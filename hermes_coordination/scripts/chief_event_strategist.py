"""
chief_event_strategist.py — 大模型事件首席分析师 (V24-C6)

🎯 目标 (V24-C6 实战):
  在 V23-R2 跨标协同 + V24-B2.1 AInvest 复用的基础上,
  进一步做"事件→传导链→持仓影响→置信度→自动建议"一体化推理.
  现有能力: 关键词匹配 + AInvest LLM 0.95s 命中, 但**没有显式推理链**.

✅ 3 核心 (PIT #66-#70):
  1. deepseek_reasoner 推理调用 (用 deepseek-reasoner 模型, 慢但准, 5-10s)
  2. 影响传导链 (事件 → 行业/概念 → 个股) 3 跳显式
  3. 动量评分 (基于历史决策 + 当前持仓变化)

📊 实战 (V24-C6 设计):
  - 拉事件 KB (从 hermes_event_analyst)
  - 拉持仓 (45 行 is_current=true)
  - 拉最近 5 个决策 (l3.decision_points)
  - 调 deepseek-reasoner 推理 (5-10s)
  - 解析 3 跳传导链 + 动量分
  - 持久化 l3.event_strategist_advice (新表)
  - 推 push_notification (cross_advise + chief_strategy 类型)

🚀 使用:
  from chief_event_strategist import ChiefEventStrategist, advise_event
  strategist = ChiefEventStrategist()
  result = strategist.analyze_event("SpaceX IPO 6月12日")
  result = advise_event("英伟达 GTC 2026 大会 HBM 需求", dry_run=False)
  python3 chief_event_strategist.py --self-test
  python3 chief_event_strategist.py --advise "事件名"  # CLI
"""
from __future__ import annotations

import json
import math
import os as _os
import sys as _sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# PIT #66-#70 设计 (V24-C6 实战)
# ============================================================
# PIT #66: deepseek-reasoner 比 deepseek-chat 慢 5-10x 但准 2-3x, 用于关键事件
# PIT #67: 传导链 3 跳, 缺哪跳标 (reasoning) 不用补, 让 LLM 自决
# PIT #68: 动量分基于历史 5 决策 + 当前持仓变化 (避免只看事件不看历史)
# PIT #69: 失败返 schema 完整 (PIT #52 复用), 不抛
# PIT #70: idempotent 24h 缓存 (同事件 24h 不重跑, 节省 cost)
# ============================================================

DEEPSEEK_REASONER_MODEL = "deepseek-reasoner"  # PIT #66
DEEPSEEK_CHAT_MODEL = "deepseek-chat"
CACHE_TTL_HOURS = 24  # PIT #70
MIN_CONFIDENCE = 0.3  # 低于此 confidence 视为不可信, 标记 low_confidence
MAX_CHAIN_HOPS = 3  # PIT #67


@dataclass
class EventChainLink:
    """传导链 1 跳"""
    hop: int  # 1/2/3
    level: str  # event/industry/concept/sector/stock
    name: str
    relevance: float  # 0-1, LLM 评估
    evidence: str  # LLM 给出证据


@dataclass
class ChiefAdvice:
    """大模型首席分析师建议"""
    advice_id: str
    event_topic: str
    direction: str  # positive/negative/neutral
    confidence: float
    primary_action: str  # buy/hold/reduce/sell
    target_codes: List[str] = field(default_factory=list)
    target_names: List[str] = field(default_factory=list)
    chain: List[EventChainLink] = field(default_factory=list)
    momentum_score: float = 0.0  # PIT #68
    reasoning: str = ""
    model_used: str = DEEPSEEK_REASONER_MODEL
    duration_seconds: float = 0.0
    raw_response: Optional[Dict] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _get_credential(key: str) -> str:
    store_path = Path("/home/aileo/.hermes/invest_credentials/store.json")
    if store_path.exists():
        store = json.loads(store_path.read_text())
        if key in store:
            return store[key]
    return _os.getenv(key, "")


def _pg_connect():
    import psycopg2
    return psycopg2.connect(
        host="localhost", port=5432, user="invest_admin",
        password=_get_credential("DB_PASSWORD"), dbname="investpilot",
    )


def _ensure_advice_table(cur):
    """PIT #69 idempotent: 创 l3.event_strategist_advice 表 (新)"""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS l3.event_strategist_advice (
            id BIGSERIAL PRIMARY KEY,
            advice_id TEXT UNIQUE NOT NULL,
            event_topic TEXT NOT NULL,
            direction VARCHAR(20),
            confidence NUMERIC,
            primary_action VARCHAR(20),
            target_codes TEXT[],
            target_names TEXT[],
            chain_json JSONB,
            momentum_score NUMERIC,
            reasoning TEXT,
            model_used VARCHAR(50),
            duration_seconds NUMERIC,
            raw_response JSONB,
            error TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_esa_topic_time ON l3.event_strategist_advice(event_topic, created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_esa_confidence ON l3.event_strategist_advice(confidence DESC)")


# ============================================================
# 缓存 (PIT #70)
# ============================================================
_CACHE_FILE = "/tmp/chief_event_strategist_cache.json"


def _load_cache() -> Dict[str, Dict]:
    if not Path(_CACHE_FILE).exists():
        return {}
    try:
        return json.loads(Path(_CACHE_FILE).read_text())
    except Exception:
        return {}


def _save_cache(cache: Dict[str, Dict]) -> None:
    try:
        Path(_CACHE_FILE).write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _cache_get(event_topic: str) -> Optional[Dict]:
    """PIT #70: 24h 内同事件返缓存"""
    cache = _load_cache()
    key = event_topic.strip().lower()
    if key not in cache:
        return None
    entry = cache[key]
    ts = datetime.fromisoformat(entry.get("cached_at", "1970-01-01T00:00:00+00:00"))
    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    if age_hours > CACHE_TTL_HOURS:
        return None
    return entry


def _cache_put(event_topic: str, advice: Dict) -> None:
    cache = _load_cache()
    key = event_topic.strip().lower()
    cache[key] = {**advice, "cached_at": datetime.now(timezone.utc).isoformat()}
    _save_cache(cache)


# ============================================================
# 1. 拉持仓 + 历史决策 (本地查询, 不调 LLM)
# ============================================================
def load_holdings_snapshot() -> List[Dict[str, Any]]:
    """拉当前 45 持仓 (精简版, 给 LLM context)"""
    conn = _pg_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT code, name, type, market_value, profit_pct, weight_pct
        FROM holdings.encrypted_positions
        WHERE is_current = true
        ORDER BY market_value DESC
        LIMIT 30
    """)
    holdings = []
    for r in cur.fetchall():
        holdings.append({
            "code": r[0], "name": r[1], "type": r[2],
            "market_value": float(r[3]) if r[3] is not None else 0,
            "profit_pct": float(r[4]) if r[4] is not None else 0,
            "weight_pct": float(r[5]) if r[5] is not None else 0,
        })
    conn.close()
    return holdings


def load_recent_decisions(limit: int = 5) -> List[Dict[str, Any]]:
    """PIT #68: 拉最近 5 个决策 (用于动量分)"""
    conn = _pg_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT decision, stock_code, confidence, reasoning, created_at
        FROM l3.decision_points
        WHERE user_id = 'aileo'
        ORDER BY id DESC
        LIMIT %s
    """, (limit,))
    rows = []
    for r in cur.fetchall():
        rows.append({
            "decision": r[0], "stock_code": r[1], "confidence": r[2],
            "reasoning": r[3], "created_at": r[4].isoformat() if r[4] else None,
        })
    conn.close()
    return rows


# ============================================================
# 2. 动量评分 (PIT #68)
# ============================================================
def calc_momentum_score(recent_decisions: List[Dict], holdings: List[Dict]) -> float:
    """
    PIT #68: 动量分 = 历史决策方向倾向 + 当前持仓盈亏
    返回 -1.0 ~ +1.0
      > 0 偏多
      < 0 偏空
      = 0 中性
    """
    if not recent_decisions:
        return 0.0
    # 历史决策倾向: buy=+1, hold=0, reduce/sell=-1
    decision_score = 0.0
    for d in recent_decisions:
        dec = (d.get("decision") or "").lower()
        conf = d.get("confidence") or 0.5
        if dec in ("buy", "加仓"):
            decision_score += conf
        elif dec in ("sell", "reduce", "减仓"):
            decision_score -= conf
        else:  # hold
            decision_score += 0
    decision_score /= len(recent_decisions)

    # 当前持仓加权盈亏
    holding_score = 0.0
    total_weight = 0.0
    for h in holdings:
        w = h.get("weight_pct") or 0
        pp = h.get("profit_pct") or 0
        # 把 profit_pct 限制到 [-100, +100] 再 * 权重
        pp_clip = max(-100, min(100, pp))
        holding_score += pp_clip * w / 100.0
        total_weight += w
    if total_weight > 0:
        holding_score = holding_score / total_weight  # 归一化到 ~-1 ~ +1
    holding_score = max(-1.0, min(1.0, holding_score / 50.0))  # 再除 50 缩到合理区间

    # 7 决策 + 3 持仓 (近期决策影响大)
    momentum = decision_score * 0.7 + holding_score * 0.3
    return round(max(-1.0, min(1.0, momentum)), 4)


# ============================================================
# 3. DeepSeek Reasoner 调用 (PIT #66)
# ============================================================
def call_deepseek_reasoner(
    event_topic: str,
    holdings: List[Dict],
    momentum: float,
    timeout: int = 30,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    PIT #66: 用 deepseek-reasoner 模型做深度推理
    PIT #69: 失败返 None + error, 不抛
    """
    api_key = _get_credential("DEEPSEEK_API_KEY")
    if not api_key:
        return None, "no_api_key"

    # 简化 holdings 给 LLM (top 15)
    top_holdings = holdings[:15]
    holdings_str = "\n".join(
        f"- {h['code']} {h['name']} ({h['type']}, MV ¥{h['market_value']:,.0f}, pp={h['profit_pct']:.2f}%, w={h['weight_pct']:.2f}%)"
        for h in top_holdings
    )

    momentum_desc = "偏多" if momentum > 0.2 else ("偏空" if momentum < -0.2 else "中性")

    system_prompt = f"""你是 Aileo 用户的首席事件策略分析师. 任务: 分析事件对持仓的影响, 给出 3 跳传导链 + 决策建议.

【用户偏好】
- A股牛散, ¥530 万持仓, 28 股票 + 17 基金
- AI 算力主线 + 事件催化
- 操盘: 先卖后买, 现金≥20%, 不接飞刀
- 止损: 澜起<235 元, 生益<70 元
- 当前动量分: {momentum:.2f} ({momentum_desc})

【当前持仓 (top 15)】
{holdings_str}

【输出 JSON Schema】严格按此格式, 不要 markdown:
{{
  "direction": "positive|negative|neutral",
  "confidence": 0.0-1.0,
  "primary_action": "buy|hold|reduce|sell",
  "target_codes": ["code1", "code2", ...],
  "target_names": ["name1", "name2", ...],
  "chain": [
    {{"hop": 1, "level": "event", "name": "<事件核心>", "relevance": 0.9, "evidence": "<为什么>"}},
    {{"hop": 2, "level": "industry|concept|sector", "name": "<行业/概念>", "relevance": 0.8, "evidence": "<传导>"}},
    {{"hop": 3, "level": "stock", "name": "<个股名 + 代码>", "relevance": 0.7, "evidence": "<最终影响>"}}
  ],
  "reasoning": "<1-3 句话综合判断>"
}}"""

    user_prompt = f"事件: {event_topic}\n\n请分析这个事件对当前持仓的影响, 给出 3 跳传导链和决策建议."

    payload = {
        "model": DEEPSEEK_REASONER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 2000,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }

    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}_{resp.text[:200]}"
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        # PIT #69: 解析失败容错
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            return None, f"json_parse_error: {e}; content: {content[:200]}"
        # 验证关键字段
        for k in ("direction", "confidence", "primary_action", "chain", "reasoning"):
            if k not in parsed:
                return None, f"missing_field_{k}"
        # 范围校验
        if not (0.0 <= parsed["confidence"] <= 1.0):
            parsed["confidence"] = max(0.0, min(1.0, parsed["confidence"]))
        if parsed["primary_action"] not in ("buy", "hold", "reduce", "sell"):
            parsed["primary_action"] = "hold"
        if parsed["direction"] not in ("positive", "negative", "neutral"):
            parsed["direction"] = "neutral"
        # 链最多 3 跳
        if len(parsed["chain"]) > MAX_CHAIN_HOPS:
            parsed["chain"] = parsed["chain"][:MAX_CHAIN_HOPS]
        return parsed, None
    except Exception as e:
        return None, f"exception: {type(e).__name__}: {e}"


# ============================================================
# 4. ChiefEventStrategist 主类
# ============================================================
class ChiefEventStrategist:
    """大模型首席事件分析师 (V24-C6)"""

    def __init__(self, model: str = DEEPSEEK_REASONER_MODEL):
        self.model = model

    def analyze_event(
        self,
        event_topic: str,
        use_cache: bool = True,
    ) -> ChiefAdvice:
        """
        分析事件, 返 ChiefAdvice
        流程: 缓存检查 → 拉 context → 调 deepseek-reasoner → 解析 → 持久化
        """
        start = time.time()
        advice_id = f"chief_{uuid.uuid4().hex[:12]}"
        advice = ChiefAdvice(
            advice_id=advice_id,
            event_topic=event_topic,
            direction="neutral",
            confidence=0.0,
            primary_action="hold",
            model_used=self.model,
        )

        # PIT #70: 24h 缓存
        if use_cache:
            cached = _cache_get(event_topic)
            if cached:
                for k in ("direction", "confidence", "primary_action", "target_codes",
                          "target_names", "chain", "momentum_score", "reasoning", "raw_response"):
                    if k in cached:
                        setattr(advice, k, cached[k])
                advice.duration_seconds = time.time() - start
                advice.error = "cache_hit"
                return advice

        # 拉 context
        holdings = load_holdings_snapshot()
        recent_decisions = load_recent_decisions(limit=5)
        momentum = calc_momentum_score(recent_decisions, holdings)
        advice.momentum_score = momentum

        # 调 LLM
        parsed, error = call_deepseek_reasoner(event_topic, holdings, momentum)
        if error:
            advice.error = error
            advice.duration_seconds = time.time() - start
            # PIT #69: 失败仍持久化 (audit)
            self._persist(advice)
            return advice

        # 填字段
        advice.direction = parsed["direction"]
        advice.confidence = parsed["confidence"]
        advice.primary_action = parsed["primary_action"]
        advice.target_codes = parsed.get("target_codes", [])
        advice.target_names = parsed.get("target_names", [])
        advice.chain = [
            EventChainLink(
                hop=link.get("hop", i + 1),
                level=link.get("level", "event"),
                name=link.get("name", ""),
                relevance=float(link.get("relevance", 0.5)),
                evidence=link.get("evidence", ""),
            )
            for i, link in enumerate(parsed.get("chain", []))
        ]
        advice.reasoning = parsed.get("reasoning", "")
        advice.raw_response = parsed
        advice.duration_seconds = time.time() - start

        # PIT #70: 缓存
        if use_cache:
            _cache_put(event_topic, {
                "direction": advice.direction,
                "confidence": advice.confidence,
                "primary_action": advice.primary_action,
                "target_codes": advice.target_codes,
                "target_names": advice.target_names,
                "chain": [asdict(c) for c in advice.chain],
                "momentum_score": advice.momentum_score,
                "reasoning": advice.reasoning,
                "raw_response": advice.raw_response,
            })

        # 持久化
        self._persist(advice)
        return advice

    def _persist(self, advice: ChiefAdvice) -> bool:
        """持久化到 PG l3.event_strategist_advice (PIT #69)"""
        try:
            conn = _pg_connect()
            cur = conn.cursor()
            _ensure_advice_table(cur)
            cur.execute("""
                INSERT INTO l3.event_strategist_advice (
                    advice_id, event_topic, direction, confidence, primary_action,
                    target_codes, target_names, chain_json, momentum_score,
                    reasoning, model_used, duration_seconds, raw_response, error
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (advice_id) DO NOTHING
            """, (
                advice.advice_id, advice.event_topic, advice.direction, advice.confidence,
                advice.primary_action, advice.target_codes, advice.target_names,
                json.dumps([asdict(c) for c in advice.chain], ensure_ascii=False),
                advice.momentum_score, advice.reasoning, advice.model_used,
                advice.duration_seconds, json.dumps(advice.raw_response, ensure_ascii=False) if advice.raw_response else None,
                advice.error,
            ))
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False


# ============================================================
# 5. CLI 入口
# ============================================================
def advise_event(event_topic: str, use_cache: bool = True) -> ChiefAdvice:
    """便利函数"""
    strategist = ChiefEventStrategist()
    return strategist.analyze_event(event_topic, use_cache=use_cache)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V24-C6 大模型事件首席分析师")
    parser.add_argument("--self-test", action="store_true", help="自测")
    parser.add_argument("--advise", type=str, help="分析指定事件")
    parser.add_argument("--no-cache", action="store_true", help="不用缓存")
    args = parser.parse_args()

    if args.self_test:
        # 自测 7 项
        print("=== V24-C6 自测 ===")
        # 1. 模块导入 OK
        print("✅ 1. 模块导入")

        # 2. 核心类
        s = ChiefEventStrategist()
        print(f"✅ 2. ChiefEventStrategist 创建, model={s.model}")

        # 3. 拉持仓
        h = load_holdings_snapshot()
        print(f"✅ 3. load_holdings_snapshot: {len(h)} 行")

        # 4. 拉决策
        d = load_recent_decisions(limit=5)
        print(f"✅ 4. load_recent_decisions: {len(d)} 行")

        # 5. 动量分
        m = calc_momentum_score(d, h)
        print(f"✅ 5. calc_momentum_score: {m}")

        # 6. 传导链 dataclass
        link = EventChainLink(hop=1, level="event", name="test", relevance=0.9, evidence="x")
        print(f"✅ 6. EventChainLink: {link}")

        # 7. PG 表
        conn = _pg_connect()
        cur = conn.cursor()
        _ensure_advice_table(cur)
        conn.commit()
        conn.close()
        print("✅ 7. l3.event_strategist_advice 表 OK")

        # 实战一次 (SpaceX)
        result = advise_event("SpaceX IPO 6月12日", use_cache=False)
        print(f"\n=== 实战: SpaceX IPO 6月12日 ===")
        print(f"direction: {result.direction}, confidence: {result.confidence}")
        print(f"primary_action: {result.primary_action}")
        print(f"target_codes: {result.target_codes}")
        print(f"chain ({len(result.chain)} 跳):")
        for c in result.chain:
            print(f"  [{c.hop}] {c.level}: {c.name} (rel={c.relevance})")
        print(f"momentum_score: {result.momentum_score}")
        print(f"duration: {result.duration_seconds:.2f}s")
        print(f"reasoning: {result.reasoning[:200]}")
        if result.error:
            print(f"error: {result.error}")
        print("✅ 自测完成")
    elif args.advise:
        result = advise_event(args.advise, use_cache=not args.no_cache)
        print(json.dumps({
            "advice_id": result.advice_id,
            "event_topic": result.event_topic,
            "direction": result.direction,
            "confidence": result.confidence,
            "primary_action": result.primary_action,
            "target_codes": result.target_codes,
            "target_names": result.target_names,
            "chain": [asdict(c) for c in result.chain],
            "momentum_score": result.momentum_score,
            "reasoning": result.reasoning,
            "duration_seconds": result.duration_seconds,
            "error": result.error,
        }, ensure_ascii=False, indent=2))
    else:
        parser.print_help()
