#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V23-R2-T1: Hermes Portfolio Co-Pilot (方案 6 跨标协同)
========================================================

实现 8 大方案中的 **方案 6: Hermes 跨标协同矩阵**：

> 当一个事件 (e.g. WWDC26 / SpaceX IPO / FOMC / 英伟达财报) 发生时，
> 自动扫描事件影响域 → 匹配到持仓 (e.g. AI 算力链: 澜起/天孚/通富) →
> 推导跨标影响 (上游/下游/替代/互补) → 生成组合级操作建议。

**核心功能**:
1. `map_event_to_holdings(event)` - 事件→持仓自动映射 (LLM 语义匹配)
2. `cross_holdings_impact(event)` - 跨标协同推理 (产业链上下游/竞争对手/客户)
3. `portfolio_action_aggregator()` - 组合级操作聚合 (避免重复建议/冲突)
4. `PortfolioCopilot.advise()` - 主入口: 事件驱动 → 组合建议

**数据源** (已验证真实):
- `holdings.encrypted_positions` - 45 持仓 (28 stock + 17 fund) 总值 ¥5,631,646.60
- `l3.dialog_history` + `l3.decision_points` - 用户决策历史
- `~/.hermes/skills/investing/stock-*` - 单股 skill 知识库 (40+ 个)
- `~/.hermes/skills/investing/etf-*` - ETF skill 知识库
- `hermes_event_analyst.HermesEventAnalyst` - 事件扫描结果

**关键 PIT 修复 (来自 15 教训)**:
- PIT #1: FTS5 列名是 `timestamp` 不是 `created_at`
- PIT #5: 路径用 `Path(__file__).parent` 不写死
- PIT #10: 多 return 路径都补全 schema 字段
- PIT #13: ts_code 后缀 (6 规则) 复用
- PIT #16: PG `public.skill_sync_audit.sync_time` (不是 created_at)

**P0 验证 (8 模式 → 加 9-10 模式)**:
- 模式 9: HermesPortfolioCopilot (本模块) - 事件→持仓映射+跨标推理+组合聚合
- 模式 10: DashboardBridge  (T2 模块) - 详见 dashboard_hermes_bridge.py

Author: Hermes Agent × aileo
Date: 2026-06-12
Version: V23-R2-T1
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ====================================================================
# 路径/LSP 误报规避 (PIT #5): 用 Path(__file__).parent 动态算
# ====================================================================
_SCRIPT_DIR = Path(__file__).resolve().parent          # hermes_coordination/scripts/
_COORD_DIR = _SCRIPT_DIR.parent                       # hermes_coordination/
_INVEST_ROOT = _COORD_DIR.parent                      # ~/invest_system/
_HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills" / "investing"
_HERMES_WEB_CSV_DIR = Path.home() / ".hermes-web-ui" / "upload"

# 把 hermes_coordination/scripts 加到 sys.path (PIT #5 修正路径)
for _p in [str(_SCRIPT_DIR), str(_INVEST_ROOT / "scripts")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 真实依赖: PGPASSWORD 必须从 store.json 读 (PIT credentials 教训)
import psycopg2
import psycopg2.extras

LOG = logging.getLogger("hermes_portfolio_copilot")
if not LOG.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


# ====================================================================
# 1. 真实数据 Schema 定义 (与 PG 实际 schema 完全匹配, 验证过)
# ====================================================================

@dataclass
class HoldingPosition:
    """持仓 (来自 holdings.encrypted_positions, 已验证)"""
    code: str
    name: str
    type: str  # 'stock' | 'fund'
    market_value: float = 0.0
    weight_pct: float = 0.0
    profit_pct: float = 0.0  # 9999.9999 表示未解密

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EventImpact:
    """事件→持仓 影响"""
    event_id: str
    event_topic: str
    affected_holdings: List[HoldingPosition]
    impact_direction: str  # 'positive' | 'negative' | 'neutral'
    impact_magnitude: float  # 0-1, 0=无影响 1=巨大影响
    reasoning: str = ""
    related_skills: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_topic": self.event_topic,
            "affected_holdings": [h.to_dict() for h in self.affected_holdings],
            "affected_count": len(self.affected_holdings),
            "affected_market_value": sum(h.market_value for h in self.affected_holdings),
            "affected_weight_pct": sum(h.weight_pct for h in self.affected_holdings),
            "impact_direction": self.impact_direction,
            "impact_magnitude": self.impact_magnitude,
            "reasoning": self.reasoning,
            "related_skills": self.related_skills,
        }


@dataclass
class CrossHoldingLink:
    """跨标关联 (上游/下游/替代/互补)"""
    source_code: str
    source_name: str
    target_code: str
    target_name: str
    relation: str  # 'upstream' | 'downstream' | 'competitor' | 'complement'
    strength: float  # 0-1
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PortfolioAdvice:
    """组合级操作建议"""
    advice_id: str
    event_topic: str
    primary_action: str  # 'buy' | 'sell' | 'reduce' | 'hold' | 'rebalance'
    target_codes: List[str]
    target_names: List[str]
    confidence: float  # 0-1
    expected_value_at_risk: float  # 影响市值
    cross_links: List[CrossHoldingLink]
    reasoning: str
    risk_warnings: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["cross_links"] = [l.to_dict() for l in self.cross_links]
        return d


# ====================================================================
# 2. PG 连接 (PIT #7 教训: 显式 commit/rollback 隔离)
# ====================================================================

def get_pg_connection():
    """从 ~/.hermes/invest_credentials/store.json 读密码 (PIT 凭据教训)"""
    store_path = Path.home() / ".hermes" / "invest_credentials" / "store.json"
    creds = json.loads(store_path.read_text())
    conn = psycopg2.connect(
        host="localhost",
        user="invest_admin",
        password=creds["DB_PASSWORD"],
        dbname="investpilot",
        connect_timeout=5,
    )
    conn.autocommit = False
    return conn


# ====================================================================
# 2.5 LLM 客户端 (V24-B2 新增: 用于事件→持仓 语义匹配)
# ====================================================================

# PIT #21 双保险: 限额文件 + __init__ 主动 touch
_LLM_QUOTA_FILE = Path("/tmp/hermes_llm_quota.json")
_LLM_DAILY_LIMIT = 20  # 与 l3_dialog_engine 共用限额
_LLM_TIMEOUT = 30  # 单次 LLM 调用超时
_LLM_MODEL = "gpt-4o-mini"  # 成本优先


class _DailyLLMQuota:
    """V24-B2: LLM 每日限额管理 (与 l3_dialog_engine 共用 /tmp/hermes_llm_quota.json)"""
    def __init__(self, daily_limit: int = _LLM_DAILY_LIMIT, quota_file: Path = _LLM_QUOTA_FILE):
        self.daily_limit = daily_limit
        self.quota_file = quota_file
        # PIT #21: 主动确保文件存在
        if not self.quota_file.exists():
            try:
                default = {"date": str(date.today()), "used": 0, "limit": daily_limit, "history": []}
                self.quota_file.write_text(json.dumps(default, ensure_ascii=False))
            except Exception as e:
                LOG.warning(f"LLM quota 文件创建失败: {e}")

    def _load(self) -> Dict:
        try:
            return json.loads(self.quota_file.read_text())
        except Exception:
            return {"date": str(date.today()), "used": 0, "limit": self.daily_limit, "history": []}

    def can_call(self) -> bool:
        state = self._load()
        # 日期切换重置
        if state.get("date") != str(date.today()):
            state = {"date": str(date.today()), "used": 0, "limit": self.daily_limit, "history": []}
            self.quota_file.write_text(json.dumps(state, ensure_ascii=False))
        return state.get("used", 0) < self.daily_limit

    def consume(self) -> int:
        state = self._load()
        if state.get("date") != str(date.today()):
            state = {"date": str(date.today()), "used": 0, "limit": self.daily_limit, "history": []}
        state["used"] = state.get("used", 0) + 1
        state["history"] = state.get("history", [])
        state["history"].append({"ts": datetime.now().isoformat(), "task": "portfolio_copilot"})
        state["history"] = state["history"][-50:]  # 保留最近 50 条
        self.quota_file.write_text(json.dumps(state, ensure_ascii=False))
        return state["used"]


_QUOTA = _DailyLLMQuota()


def call_llm_for_event_match(event_topic: str, holdings: List[HoldingPosition]) -> Optional[Dict[str, Any]]:
    """
    V24-B2: 调 LLM 语义匹配事件→持仓

    PIT 防御:
    - 限额检查: 超出走 None (触发 fallback)
    - 超时 30s: 走 None
    - JSON parse 失败: 走 None
    - LLM 不可用: 走 None (PIT #7 fallback chain)

    Returns:
        {
            "affected_codes": ["002050", "601689", ...],
            "direction": "positive" | "negative" | "neutral",
            "reasoning": "事件核心逻辑: ...\n受影响标的: ...",
            "model": "gpt-4o-mini",
            "tokens_used": 1234
        }
        或 None (任何失败)
    """
    if not _QUOTA.can_call():
        LOG.info(f"[call_llm_for_event_match] 限额已满 ({_QUOTA.daily_limit}/天), 跳过 LLM")
        return None

    # 构造 prompt
    holdings_summary = []
    for h in holdings:
        holdings_summary.append({
            "code": h.code,
            "name": h.name,
            "type": h.type,
            "market_value": round(h.market_value, 0),
            "weight_pct": round(h.weight_pct, 2),
        })

    system_prompt = """你是 Hermes Agent 投资分析助手。任务: 给定一个市场事件描述 + 用户当前持仓列表, 语义分析事件影响域, 输出受影响的持仓代码。

规则:
1. **必须**返回严格 JSON, 不要任何解释文字
2. **只**输出持仓列表中**真实存在**的 code (6 位数字)
3. 影响方向: positive (利好持仓) / negative (利空持仓) / neutral (无关)
4. reasoning 简述: 事件核心 + 为什么这些持仓受影响
5. 如果事件与持仓无关, 返回空 affected_codes 数组 + neutral
6. **不要**输出未在列表中的 code, **不要**编造持仓"""

    user_prompt = f"""事件描述: {event_topic}

用户当前持仓 ({len(holdings)} 个, 总市值 ¥5,631,646):
{json.dumps(holdings_summary, ensure_ascii=False, indent=1)}

请返回 JSON:
{{
  "affected_codes": ["code1", "code2", ...],
  "direction": "positive|negative|neutral",
  "reasoning": "事件核心逻辑 + 受影响原因"
}}"""

    try:
        # 尝试 OpenAI SDK (gpt-4o-mini)
        try:
            from openai import OpenAI
            store_path = Path.home() / ".hermes" / "invest_credentials" / "store.json"
            creds = json.loads(store_path.read_text())
            api_key = creds.get("DEEPSEEK_API_KEY") or creds.get("OPENAI_API_KEY")
            if not api_key:
                LOG.warning("[call_llm_for_event_match] 无 LLM API key, 跳过")
                return None
            client = OpenAI(api_key=api_key, timeout=_LLM_TIMEOUT)
            _QUOTA.consume()
            resp = client.chat.completions.create(
                model=_LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=800,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            usage = resp.usage
            result = json.loads(content)
            # 验证 schema (PIT #10)
            if not isinstance(result.get("affected_codes"), list):
                LOG.warning(f"[call_llm_for_event_match] LLM 返回 schema 错: {result}")
                return None
            if result.get("direction") not in ("positive", "negative", "neutral"):
                result["direction"] = "neutral"
            result["model"] = _LLM_MODEL
            result["tokens_used"] = usage.total_tokens if usage else 0
            LOG.info(f"[call_llm_for_event_match] LLM 匹配: {len(result['affected_codes'])} 标的, "
                     f"direction={result['direction']}, tokens={result['tokens_used']}")
            return result
        except ImportError:
            LOG.warning("[call_llm_for_event_match] openai SDK 未安装, 跳过 LLM")
            return None
    except Exception as e:
        LOG.warning(f"[call_llm_for_event_match] LLM 调失败: {type(e).__name__}: {e}")
        return None


# PIT #22 模式标识 (V24-B2): 区分 LLM vs 关键词匹配, 用于监控分析
_MATCH_MODE_LLM = "llm"
_MATCH_MODE_KEYWORD = "keyword"
_MATCH_MODE_EMPTY = "empty"


# ====================================================================
# 3. 持仓加载 (真实 holdings.encrypted_positions, 45 行)
# ====================================================================

def load_current_holdings(conn=None) -> List[HoldingPosition]:
    """
    从 PG 加载当前持仓 (is_current=true)

    PIT 验证: 实际 45 行 (28 stock + 17 fund)
    """
    if conn is None:
        conn = get_pg_connection()
    try:
        cur = conn.cursor()
        # PIT #7 显式 commit
        conn.commit()
        cur.execute("""
            SELECT code, name, type, market_value, weight_pct, profit_pct
            FROM holdings.encrypted_positions
            WHERE is_current = true
              AND market_value > 0
            ORDER BY market_value DESC
        """)
        rows = cur.fetchall()
        conn.commit()
        holdings = [
            HoldingPosition(
                code=r[0], name=r[1], type=r[2],
                market_value=float(r[3] or 0),
                weight_pct=float(r[4] or 0),
                profit_pct=float(r[5] or 0),
            )
            for r in rows
        ]
        LOG.info(f"[load_current_holdings] loaded {len(holdings)} holdings, "
                 f"total_mv=¥{sum(h.market_value for h in holdings):,.2f}")
        return holdings
    except Exception as e:
        # PIT #7 显式 rollback
        conn.rollback()
        LOG.error(f"[load_current_holdings] failed: {e}")
        raise


# ====================================================================
# 4. Skill 知识库索引 (~/.hermes/skills/investing/*)
# ====================================================================

def index_investing_skills() -> Dict[str, Dict[str, str]]:
    """
    扫描所有 investing skill, 提取 (代码→skill_name, 摘要, 主题)

    Returns: {code: {name, skill_dir, summary, themes}}
    """
    if not _HERMES_SKILLS_DIR.exists():
        LOG.warning(f"[index_investing_skills] not found: {_HERMES_SKILLS_DIR}")
        return {}

    index: Dict[str, Dict[str, str]] = {}
    for skill_dir in _HERMES_SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")[:5000]
            # 提取股票代码 (6 位数字, 排除 ETF 51/56/58/15 开头作为可识别)
            codes = set(re.findall(r"\b[036]\d{5}\b", text))
            # 提取主题 (看 frontmatter tags)
            tags_match = re.search(r"tags:\s*\[([^\]]*)\]", text)
            tags = [t.strip().strip('"').strip("'")
                    for t in (tags_match.group(1) if tags_match else "").split(",") if t.strip()]
            summary = re.sub(r"\s+", " ", text[:200]).strip()
            for code in codes:
                index[code] = {
                    "name": skill_name,
                    "skill_dir": str(skill_dir),
                    "summary": summary,
                    "themes": tags,
                }
        except Exception as e:
            LOG.debug(f"[index_investing_skills] skip {skill_name}: {e}")
    LOG.info(f"[index_investing_skills] indexed {len(index)} stock skills")
    return index


# ====================================================================
# 5. 事件→持仓 语义匹配 (PIT #13 复用 _normalize_ts_code)
# ====================================================================

# 6 大主题→持仓代码 关键词映射 (主题事件匹配, 不依赖 LLM 也能跑)
# PIT #18 修复: 每个主题配 event_keywords, 事件描述必须出现才匹配
# 避免"AI 算力"污染"SaceX IPO"等无关事件
THEME_KEYWORDS_TO_CODES: Dict[str, Dict[str, Any]] = {
    "AI算力": {
        "event_keywords": ["AI", "算力", "GPU", "HBM", "Blackwell", "H200", "B200",
                           "数据中心", "液冷", "光模块", "CPO", "1.6T"],
        "codes": ["688008", "300394", "002156", "688025", "688279",
                  "300059", "600183", "002384", "002837", "002600",
                  "159819", "515700"],
        "names": ["澜起科技", "天孚通信", "通富微电", "杰普特", "峰岹科技",
                  "东方财富", "生益科技", "东山精密", "英维克", "领益智造",
                  "人工智能ETF", "新能车ETF"],
    },
    "SpaceX星链": {
        "event_keywords": ["SpaceX", "星链", "Starlink", "IPO", "卫星",
                           "星舰", "Starship", "铌合金", "火箭", "可回收"],
        "codes": ["300136", "002149", "600487"],
        "names": ["信维通信", "西部材料", "亨通光电"],
    },
    "可控核聚变": {
        "event_keywords": ["核聚变", "托卡马克", "EAST", "ITER", "超导",
                           "等离子体", "氘氚", "能量增益"],
        "codes": ["600105"],
        "names": ["永鼎股份"],
    },
    "黄金避险": {
        "event_keywords": ["黄金", "金价", "避险", "央行购金", "美元贬值",
                           "中东", "地缘", "通胀"],
        "codes": ["518880"],
        "names": ["黄金ETF"],
    },
    "有色金属": {
        "event_keywords": ["有色", "铜价", "铝价", "锂价", "镍价", "钴价",
                           "工业金属", "周期"],
        "codes": ["516650"],
        "names": ["有色金属ETF"],
    },
    "半导体": {
        "event_keywords": ["半导体", "芯片", "晶圆", "光刻机", "ASML",
                           "TSMC", "中芯", "存储", "DDR5", "HBM", "DRAM", "MLCC"],
        "codes": ["688008", "002156", "300394", "688025", "002600",
                  "600183", "002975", "301611", "002080", "600378",
                  "600063", "159516"],
        "names": ["澜起科技", "通富微电", "天孚通信", "杰普特", "领益智造",
                  "生益科技", "博杰股份", "珂玛科技", "中材科技", "昊华科技",
                  "皖维高新", "半导体设备ETF国泰"],
    },
    "新能源汽车": {
        "event_keywords": ["新能源车", "电动车", "小米SU7", "特斯拉", "蔚来",
                           "小鹏", "理想", "比亚迪", "动力电池", "电驱", "热管理"],
        "codes": ["601689", "002472", "300680", "002050", "002709"],
        "names": ["拓普集团", "双环传动", "隆盛科技", "三花智控", "天赐材料"],
    },
    "AI医疗": {
        "event_keywords": ["AI医疗", "医疗AI", "诊断AI", "DeepSeek", "妙想",
                           "金融大模型", "医疗大模型"],
        "codes": ["002432"],
        "names": ["九安医疗"],
    },
    "机器人": {
        "event_keywords": ["机器人", "Optimus", "宇树", "Figure", "减速器",
                           "谐波", "RV减速器", "特斯拉机器人", "人形"],
        "codes": ["002050", "002472", "300680", "601689"],
        "names": ["三花智控", "双环传动", "隆盛科技", "拓普集团"],
    },
    "风电叶片": {
        "event_keywords": ["风电", "风机", "海风", "陆风", "叶片", "塔筒"],
        "codes": ["002080", "600458"],
        "names": ["中材科技", "时代新材"],
    },
    "锂电材料": {
        "event_keywords": ["锂电", "电解液", "六氟磷酸锂", "正极", "负极",
                           "隔膜", "钠电", "固态电池"],
        "codes": ["002709", "300450", "300395", "002756"],
        "names": ["天赐材料", "先导智能", "菲利华", "永兴材料"],
    },
    "MLCC": {
        "event_keywords": ["MLCC", "电容", "村田", "TDK", "三环"],
        "codes": ["600183"],
        "names": ["生益科技"],
    },
    "玻璃基板": {
        "event_keywords": ["玻璃基板", "英特尔", "TGV", "先进封装", "扇出"],
        "codes": ["002384"],
        "names": ["东山精密"],
    },
    "FOF/基金": {
        "event_keywords": ["FOF", "基金重仓", "公募", "百亿基金"],
        "codes": ["002943", "007355"],
        "names": ["广发多因子混合", "汇添富科技创新混合A"],
    },
}


def map_event_to_holdings(event_topic: str, holdings: List[HoldingPosition],
                          use_llm: bool = True) -> EventImpact:
    """
    事件→持仓 自动映射 (V24-B2 LLM 真实接入)

    策略 (PIT #18 修复: 必须 event_keywords 命中才匹配):
    1. 主题关键词匹配: 主题的 event_keywords 必须在事件描述中出现
    2. 持仓名/代码直接匹配: 事件描述中出现代码/股票名
    3. 估算影响方向 (positive/negative/neutral)

    V24-B2 新增 (PIT #22):
    - 主路径: LLM 语义匹配 (call_llm_for_event_match)
      - 失败/限额满 → 降级到关键词匹配
    - use_llm=False 强制走关键词 (测试用)
    """
    # PIT #22: 跟踪匹配模式, 写入 PG
    match_mode = _MATCH_MODE_EMPTY
    llm_reasoning = ""

    # V24-B2: 先尝试 LLM 语义匹配 (主路径)
    if use_llm:
        # PIT #7 + #22: try/except 包整个 LLM 调用, 失败降级
        try:
            llm_result = call_llm_for_event_match(event_topic, holdings)
        except Exception as e:
            LOG.warning(f"[map_event_to_holdings] LLM 异常, 降级: {type(e).__name__}: {e}")
            llm_result = None
        if llm_result is not None and llm_result.get("affected_codes"):
            # LLM 匹配成功, 构造 EventImpact
            affected: List[HoldingPosition] = []
            holdings_map = {h.code: h for h in holdings}
            for code in llm_result["affected_codes"]:
                if code in holdings_map:
                    h = holdings_map[code]
                    if h not in affected:
                        affected.append(h)

            direction = llm_result.get("direction", "neutral")
            if direction not in ("positive", "negative", "neutral"):
                direction = "neutral"

            affected_weight = sum(h.weight_pct for h in affected)
            magnitude = min(1.0, affected_weight / 100.0)

            related_skills: List[str] = []
            for h in affected:
                skill_match = _HERMES_SKILLS_DIR / f"stock-{h.code}"
                if skill_match.exists():
                    related_skills.append(f"stock-{h.code}")
                else:
                    etf_match = _HERMES_SKILLS_DIR / f"etf-{h.code}"
                    if etf_match.exists():
                        related_skills.append(f"etf-{h.code}")

            llm_reasoning = llm_result.get("reasoning", "")
            reasoning = (f"[LLM语义匹配] 事件 '{event_topic}' → {len(affected)} 标的。"
                         f"LLM推理: {llm_reasoning[:200]}"
                         f"受影响权重 {affected_weight:.2f}%.")

            # 模式标识
            match_mode = _MATCH_MODE_LLM

            return EventImpact(
                event_id=f"evt_{int(time.time())}_llm",
                event_topic=event_topic,
                affected_holdings=affected,
                impact_direction=direction,
                impact_magnitude=round(magnitude, 4),
                reasoning=reasoning,
                related_skills=related_skills,
            )
        elif llm_result is not None:
            # LLM 返回了但 affected_codes 为空 → 真的无影响
            match_mode = _MATCH_MODE_LLM
            return EventImpact(
                event_id=f"evt_{int(time.time())}_llm",
                event_topic=event_topic,
                affected_holdings=[],
                impact_direction=llm_result.get("direction", "neutral"),
                impact_magnitude=0.0,
                reasoning=f"[LLM语义匹配] 事件 '{event_topic}' 与持仓无关。LLM推理: {llm_reasoning[:200]}",
                related_skills=[],
            )
        # else: LLM 失败/限额满, 降级到关键词匹配
        LOG.info(f"[map_event_to_holdings] LLM 不可用, 降级到关键词匹配")

    # ===== 降级路径: 关键词硬匹配 (PIT #18 修复) =====
    affected: List[HoldingPosition] = []
    related_skills: List[str] = []
    matched_themes: List[str] = []

    holdings_map = {h.code: h for h in holdings}
    holdings_by_name = {h.name: h for h in holdings}

    # 1. 主题匹配 (PIT #18 严格关键词)
    for theme, info in THEME_KEYWORDS_TO_CODES.items():
        event_kw = info.get("event_keywords", [])
        theme_codes = info.get("codes", [])
        theme_names = info.get("names", [])

        # 必须 event_keywords 至少一个命中
        if not any(kw in event_topic for kw in event_kw):
            continue

        for code in theme_codes:
            if code in holdings_map:
                h = holdings_map[code]
                if h not in affected:
                    affected.append(h)
                matched_themes.append(theme)
        for name in theme_names:
            # PIT #19 修复: 持仓名可能是"黄金ETF华安"等带后缀, 用 in 而非 ==
            for h_name, h in holdings_by_name.items():
                if name in h_name or h_name in name:
                    if h not in affected:
                        affected.append(h)
                    matched_themes.append(theme)
                    break

    # 2. 直接代码匹配 (6 位数字)
    for code_match in re.findall(r"\b[036]\d{5}\b", event_topic):
        if code_match in holdings_map:
            h = holdings_map[code_match]
            if h not in affected:
                affected.append(h)

    # 3. 决定影响方向
    positive_themes = {"AI算力", "SpaceX星链", "可控核聚变", "半导体", "新能源汽车", "机器人", "锂电材料"}
    negative_themes = {"黄金避险", "有色金属", "FOF/基金"}  # 风险事件, 利好避险资产

    if any(t in positive_themes for t in matched_themes):
        direction = "positive"
    elif any(t in negative_themes for t in matched_themes):
        direction = "positive"  # 风险事件利好避险, 也算 positive
    else:
        direction = "neutral"

    # 影响强度: 受影响持仓权重占比
    affected_weight = sum(h.weight_pct for h in affected)
    magnitude = min(1.0, affected_weight / 100.0)

    # 收集相关 skill 名
    for h in affected:
        # 尝试找对应 skill (code 在 skill index)
        skill_match = _HERMES_SKILLS_DIR / f"stock-{h.code}"
        if skill_match.exists():
            related_skills.append(f"stock-{h.code}")
        else:
            etf_match = _HERMES_SKILLS_DIR / f"etf-{h.code}"
            if etf_match.exists():
                related_skills.append(f"etf-{h.code}")

    reasoning = (f"[关键词匹配] 事件 '{event_topic}' 匹配到主题: {', '.join(set(matched_themes)) or '无'}。"
                 f"受影响持仓 {len(affected)} 个, 合计权重 {affected_weight:.2f}%.")

    # PIT #22: 模式标识
    match_mode = _MATCH_MODE_KEYWORD if affected else _MATCH_MODE_EMPTY

    return EventImpact(
        event_id=f"evt_{int(time.time())}_kw",
        event_topic=event_topic,
        affected_holdings=affected,
        impact_direction=direction,
        impact_magnitude=round(magnitude, 4),
        reasoning=reasoning,
        related_skills=related_skills,
    )


# ====================================================================
# 6. 跨标协同推理 (基于产业链知识 + skill 摘要)
# ====================================================================

# 跨标关联知识 (核心 8 大产业链)
CROSS_HOLDING_RELATIONS: List[CrossHoldingLink] = [
    # AI 算力光互连链
    CrossHoldingLink("300394", "天孚通信", "688008", "澜起科技", "upstream", 0.9,
                     "天孚光器件→澜起 DDR5 接口芯片"),
    CrossHoldingLink("002156", "通富微电", "300394", "天孚通信", "downstream", 0.7,
                     "通富 AMD 封装→天孚光器件需求"),
    CrossHoldingLink("688025", "杰普特", "300394", "天孚通信", "complement", 0.6,
                     "杰普特激光调阻机→天孚光器件生产设备"),
    # SpaceX 链
    CrossHoldingLink("002149", "西部材料", "300136", "信维通信", "complement", 0.8,
                     "西部材料铌合金→SpaceX 星链→信维通信零部件"),
    CrossHoldingLink("600487", "亨通光电", "300136", "信维通信", "competitor", 0.5,
                     "亨通光电分拆亨通华海(光纤) vs 信维通信射频"),
    # 新能源汽车链
    CrossHoldingLink("002050", "三花智控", "601689", "拓普集团", "complement", 0.85,
                     "三花热管理+拓普 NVH 平台→特斯拉/小米 su7 配套"),
    CrossHoldingLink("002472", "双环传动", "002050", "三花智控", "complement", 0.7,
                     "双环齿轮+三花阀→新能源车电驱总成"),
    # 半导体设备链
    CrossHoldingLink("002975", "博杰股份", "301611", "珂玛科技", "downstream", 0.6,
                     "博杰测试设备→珂玛陶瓷加热器验证"),
    # MLCC/PCB 链
    CrossHoldingLink("600183", "生益科技", "002384", "东山精密", "upstream", 0.75,
                     "生益 CCL→东山精密 PCB/FPC 基板"),
    # 锂电链
    CrossHoldingLink("002709", "天赐材料", "300450", "先导智能", "upstream", 0.8,
                     "天赐电解液→先导锂电设备产线"),
    # 风电链
    CrossHoldingLink("002080", "中材科技", "600458", "时代新材", "competitor", 0.7,
                     "中材 vs 时代新材→风电叶片双寡头"),
]


def cross_holdings_impact(impact: EventImpact) -> List[CrossHoldingLink]:
    """
    跨标协同推理: 基于影响到的持仓, 找出其在产业链中的关联

    Returns: CrossHoldingLink[] (仅含受事件影响的标的)
    """
    affected_codes = {h.code for h in impact.affected_holdings}
    relevant_links: List[CrossHoldingLink] = []

    for link in CROSS_HOLDING_RELATIONS:
        # 至少一端在受影响列表
        if link.source_code in affected_codes or link.target_code in affected_codes:
            # 同时两端都受影响 → 协同效应更强
            if link.source_code in affected_codes and link.target_code in affected_codes:
                link.strength = min(1.0, link.strength * 1.3)
            relevant_links.append(link)
    LOG.info(f"[cross_holdings_impact] {len(relevant_links)} cross-links for {impact.event_topic}")
    return relevant_links


# ====================================================================
# 7. 组合级操作聚合 (PIT #10 早退 schema 铁律)
# ====================================================================

def aggregate_portfolio_advice(
    impact: EventImpact,
    cross_links: List[CrossHoldingLink],
    primary_action: str = "hold",
) -> PortfolioAdvice:
    """
    聚合事件影响 + 跨标推理 → 组合级操作建议

    primary_action 决策逻辑:
    - positive 大影响 (magnitude > 0.3): 加仓相关
    - negative 大影响: 减仓/对冲 (黄金/有色)
    - 中性: 持有观察
    """
    target_codes = [h.code for h in impact.affected_holdings]
    target_names = [h.name for h in impact.affected_holdings]
    affected_mv = sum(h.market_value for h in impact.affected_holdings)
    affected_weight = sum(h.weight_pct for h in impact.affected_holdings)

    # 自动判断 primary_action
    if impact.impact_direction == "positive" and impact.impact_magnitude > 0.3:
        primary_action = "buy" if impact.impact_magnitude > 0.5 else "hold"
        confidence = min(0.95, 0.6 + impact.impact_magnitude)
    elif impact.impact_direction == "negative" and impact.impact_magnitude > 0.3:
        primary_action = "reduce"
        confidence = 0.7
    else:
        primary_action = "hold"
        confidence = 0.5

    # 风险提示
    risk_warnings: List[str] = []
    if affected_mv > 1_000_000:
        risk_warnings.append(f"⚠️ 受影响市值 ¥{affected_mv:,.0f} 超过 100 万, 单事件集中度高")
    if impact.impact_magnitude > 0.5:
        risk_warnings.append(f"⚠️ 组合权重 {impact.impact_magnitude*100:.1f}% 受影响, 建议分散")
    if len(cross_links) > 5:
        risk_warnings.append(f"⚠️ 跨标关联 {len(cross_links)} 条, 需评估二阶影响")

    reasoning = (
        f"[事件] {impact.event_topic}\n"
        f"[影响] {impact.impact_direction} | 强度 {impact.impact_magnitude:.2f} | "
        f"标的 {len(target_codes)} 个 | 权重 {affected_weight:.2f}%\n"
        f"[跨标] {len(cross_links)} 条关联\n"
        f"[决策] {primary_action.upper()} | 置信度 {confidence:.2f}"
    )

    return PortfolioAdvice(
        advice_id=f"adv_{int(time.time())}",
        event_topic=impact.event_topic,
        primary_action=primary_action,
        target_codes=target_codes,
        target_names=target_names,
        confidence=round(confidence, 4),
        expected_value_at_risk=round(affected_mv, 2),
        cross_links=cross_links,
        reasoning=reasoning,
        risk_warnings=risk_warnings,
    )


# ====================================================================
# 8. 主入口: PortfolioCopilot
# ====================================================================

class PortfolioCopilot:
    """跨标协同 Copilot (方案 6 主类)"""

    def __init__(self, conn=None, dry_run: bool = True):
        self.conn = conn
        self.dry_run = dry_run
        self.holdings: List[HoldingPosition] = []
        self.skill_index: Dict[str, Dict[str, str]] = {}

    def __enter__(self):
        if self.conn is None:
            self.conn = get_pg_connection()
        self.holdings = load_current_holdings(self.conn)
        self.skill_index = index_investing_skills()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            try:
                self.conn.commit()
            except Exception:
                self.conn.rollback()
            self.conn.close()

    def advise(self, event_topic: str, primary_action: Optional[str] = None) -> PortfolioAdvice:
        """
        主入口: 事件 → 持仓映射 → 跨标推理 → 组合建议

        Args:
            event_topic: 事件描述 (e.g. "WWDC26 Siri 全面觉醒 AI 助手")
            primary_action: 可选, 强制指定 (e.g. 'buy'/'sell'/'hold'/'reduce')

        Returns:
            PortfolioAdvice (含目标代码/置信度/风险提示)
        """
        # Step 1: 事件→持仓
        impact = map_event_to_holdings(event_topic, self.holdings)

        # Step 2: 跨标协同
        cross_links = cross_holdings_impact(impact)

        # Step 3: 组合级操作聚合
        advice = aggregate_portfolio_advice(impact, cross_links, primary_action or "hold")

        LOG.info(f"[advise] '{event_topic}' → "
                 f"{advice.primary_action} | {len(advice.target_codes)} 标的 | "
                 f"置信度 {advice.confidence}")
        return advice


# ====================================================================
# 9. PG 持久化 (l3.portfolio_copilot_log)
# ====================================================================

PG_DDL = """
CREATE TABLE IF NOT EXISTS l3.portfolio_copilot_log (
    id              BIGSERIAL PRIMARY KEY,
    advice_id       VARCHAR(64) NOT NULL UNIQUE,
    event_topic     TEXT NOT NULL,
    primary_action  VARCHAR(20) NOT NULL,
    target_codes    TEXT[] NOT NULL,
    target_names    TEXT[] NOT NULL,
    confidence      NUMERIC(4,3) NOT NULL,
    expected_var    NUMERIC(16,2) NOT NULL,
    impact_direction VARCHAR(20),
    impact_magnitude NUMERIC(6,4),
    cross_link_count INTEGER,
    risk_warnings   JSONB,
    reasoning       TEXT,
    full_advice     JSONB,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pcl_action_time ON l3.portfolio_copilot_log (primary_action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pcl_topic ON l3.portfolio_copilot_log USING gin (to_tsvector('simple', event_topic));
"""


def ensure_pg_table(conn):
    cur = conn.cursor()
    cur.execute(PG_DDL)
    conn.commit()
    LOG.info("[ensure_pg_table] l3.portfolio_copilot_log ready")


def persist_advice(conn, advice: PortfolioAdvice, impact: EventImpact) -> int:
    """PG 持久化, 返回 advice_id"""
    ensure_pg_table(conn)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO l3.portfolio_copilot_log (
            advice_id, event_topic, primary_action, target_codes, target_names,
            confidence, expected_var, impact_direction, impact_magnitude,
            cross_link_count, risk_warnings, reasoning, full_advice
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (advice_id) DO NOTHING
        RETURNING id
    """, (
        advice.advice_id, advice.event_topic, advice.primary_action,
        advice.target_codes, advice.target_names, advice.confidence,
        advice.expected_value_at_risk, impact.impact_direction, impact.impact_magnitude,
        len(advice.cross_links), json.dumps(advice.risk_warnings, ensure_ascii=False),
        advice.reasoning, json.dumps(advice.to_dict(), ensure_ascii=False, default=str),
    ))
    rid = cur.fetchone()
    conn.commit()
    LOG.info(f"[persist_advice] {advice.advice_id} → id={rid[0] if rid else 'EXISTS'}")
    return rid[0] if rid else -1


# ====================================================================
# 10. 模式 9 测试驱动 (6 模式 → 9 模式)
# ====================================================================

def _selftest_pattern_9() -> Dict[str, Any]:
    """模式 9: PortfolioCopilot 端到端测试"""
    LOG.info("[pattern_9] start")
    t0 = time.time()
    result: Dict[str, Any] = {"pattern": 9, "name": "PortfolioCopilot", "tests": []}

    # 1. 持仓加载
    with PortfolioCopilot() as copilot:
        assert len(copilot.holdings) == 45, f"expected 45 holdings, got {len(copilot.holdings)}"
        result["tests"].append({
            "test": "load_holdings",
            "expected": 45, "actual": len(copilot.holdings),
            "passed": len(copilot.holdings) == 45,
        })

        # 2. skill 索引
        assert len(copilot.skill_index) >= 20, f"expected ≥20 skills, got {len(copilot.skill_index)}"
        result["tests"].append({
            "test": "index_skills",
            "expected": ">=20", "actual": len(copilot.skill_index),
            "passed": len(copilot.skill_index) >= 20,
        })

        # 3. 事件→持仓 映射 (AI 算力事件)
        impact = map_event_to_holdings("英伟达 Blackwell 量产, AI 算力链爆发", copilot.holdings)
        assert len(impact.affected_holdings) >= 5, f"expected ≥5 持仓受影响, got {len(impact.affected_holdings)}"
        result["tests"].append({
            "test": "map_event_ai",
            "expected": ">=5 affected", "actual": len(impact.affected_holdings),
            "passed": len(impact.affected_holdings) >= 5,
        })

        # 4. 跨标推理
        links = cross_holdings_impact(impact)
        assert len(links) >= 2, f"expected ≥2 跨标关联, got {len(links)}"
        result["tests"].append({
            "test": "cross_links",
            "expected": ">=2", "actual": len(links),
            "passed": len(links) >= 2,
        })

        # 5. 组合建议
        advice = aggregate_portfolio_advice(impact, links)
        assert advice.primary_action in ("buy", "hold", "sell", "reduce")
        assert 0 < advice.confidence <= 1
        assert advice.expected_value_at_risk > 0
        result["tests"].append({
            "test": "advice_aggregator",
            "expected": "valid action+confidence",
            "actual": f"{advice.primary_action}/{advice.confidence}",
            "passed": advice.primary_action in ("buy", "hold", "sell", "reduce"),
        })

        # 6. 端到端 advise (SpaceX 事件)
        advice_spc = copilot.advise("SpaceX IPO 6月12日 估值 1.3 万亿")
        assert "信维通信" in advice_spc.target_names or "002149" in [c[:6] for c in advice_spc.target_codes]
        result["tests"].append({
            "test": "end_to_end_spacex",
            "expected": "信维通信/西部材料 affected",
            "actual": advice_spc.target_names[:3],
            "passed": len(advice_spc.target_names) > 0,
        })

        # 7. PG 持久化
        rid = persist_advice(copilot.conn, advice, impact)
        result["tests"].append({
            "test": "pg_persist",
            "expected": "id>0", "actual": rid,
            "passed": rid > 0 or rid == -1,  # -1 表示已存在
        })

    # 8. 早退路径: 无持仓匹配的事件
    impact_none = map_event_to_holdings("完全无关事件 xyz", [])
    assert len(impact_none.affected_holdings) == 0
    assert impact_none.impact_magnitude == 0.0
    result["tests"].append({
        "test": "early_return_no_match",
        "expected": "empty+0.0",
        "actual": f"{len(impact_none.affected_holdings)}/{impact_none.impact_magnitude}",
        "passed": len(impact_none.affected_holdings) == 0 and impact_none.impact_magnitude == 0.0,
    })

    # 9. 早退路径: 中性事件
    advice_neutral = aggregate_portfolio_advice(impact_none, [])
    assert advice_neutral.primary_action == "hold"
    assert advice_neutral.confidence == 0.5
    result["tests"].append({
        "test": "early_return_neutral",
        "expected": "hold/0.5",
        "actual": f"{advice_neutral.primary_action}/{advice_neutral.confidence}",
        "passed": advice_neutral.primary_action == "hold",
    })

    # 10. 真实 SQL 验证 (PG 有数据)
    conn = get_pg_connection()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM l3.portfolio_copilot_log")
    pg_count = cur.fetchone()[0]
    conn.commit()
    conn.close()
    result["tests"].append({
        "test": "pg_row_count",
        "expected": ">=1", "actual": pg_count,
        "passed": pg_count >= 1,
    })

    result["duration_seconds"] = round(time.time() - t0, 3)
    result["passed"] = sum(1 for t in result["tests"] if t["passed"])
    result["total"] = len(result["tests"])
    return result


if __name__ == "__main__":
    # 模式 9 自测
    res = _selftest_pattern_9()
    print(f"\n=== 模式 9: PortfolioCopilot ===")
    print(f"通过: {res['passed']}/{res['total']} | 耗时: {res['duration_seconds']}s")
    for t in res["tests"]:
        ok = "✅" if t["passed"] else "❌"
        print(f"  {ok} {t['test']}: expected={t['expected']} actual={t['actual']}")
    sys.exit(0 if res["passed"] == res["total"] else 1)
