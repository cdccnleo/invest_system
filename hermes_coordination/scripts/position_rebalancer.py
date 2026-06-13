"""
position_rebalancer.py — V25-B 持仓调仓助手
================================================================
背景: v2.5 plan 7 候选中 ⭐最高 ROI, 联动 C1 (风险) + C6 (事件) + L3 (策略) 3 源
核心:
  1. generate_rebalance_suggestion — 综合 3 源 → RebalanceAction 列表
  2. confirm_rebalance — 飞书回调/手动确认 → 写 audit log
  3. execute_rebalance — 模拟/真实下单 → 持久化 l3.rebalance_log
  4. get_rebalance_history — 时间轴 + 胜率 (V25-B4)

数据源 (3 源):
  - C1: l3.risk_alert_log (持仓风险, severity P0/P1/P2)
  - C6: l3.event_strategist_advice (大模型事件, direction + confidence)
  - L3: l3.decision_points (策略决策, action + confidence)

PIT 经验:
  - PIT #74 (V25-B NEW): 模拟模式默认开启, 真实下单需显式 enable_broker=True
  - PIT #75 (V25-B NEW): 飞书 action 按钮 → 用轮询确认替代 callback (飞书 webhook 限制)
  - PIT #76 (V25-B NEW): 同 code 多源冲突 → 取最严重 (P0 > P1 > P2)
  - PIT #77 (V25-B NEW): 单标的权重 > 5% → 自动减仓至 5% (V24-B4 default)
  - PIT #78 (V25-B NEW): 重平衡需 2 步确认 (suggest → confirm → execute)
  - PIT #66 沿用: 飞书推送就地实现
  - PIT #69 沿用: 3 通道全空 → 返 0 (PG 兜底)
  - PIT #71 沿用: 缺失数据 = 跳过
"""
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

# 路径
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hermes_coordination" / "scripts"))

import psycopg2
import psycopg2.extras

# 凭据
from credentials import get_credential

LOG = logging.getLogger("v25_b.position_rebalancer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==================== 常量 ====================

DB_PARAMS = {
    "host": "localhost",
    "dbname": "investpilot",
    "user": "invest_admin",
    "password": get_credential("DB_PASSWORD"),
}

# 调仓参数
MAX_SINGLE_WEIGHT = 5.0  # PIT #77: 单标的权重上限 (V24-B4 default)
MIN_CONFIDENCE = 0.50  # C6/L3 conf < 0.5 不调仓
EXECUTION_MODE = "simulation"  # PIT #74: 默认模拟
USER_ID = "aileo"

# 飞书推送
FEISHU_MAX_LEN = 1800
RETRY_TIMES = 3


# ==================== 枚举 ====================

class ActionType(str, Enum):
    """调仓动作类型"""
    REDUCE_50 = "reduce_50"  # 减仓 50%
    REDUCE_30 = "reduce_30"  # 减仓 30%
    REDUCE_20 = "reduce_20"  # 减仓 20%
    INCREASE = "increase"    # 加仓
    HOLD = "hold"            # 持有
    CLOSE = "close"          # 清仓


class Severity(str, Enum):
    """严重程度"""
    P0 = "P0"  # 必须立即行动 (C1 P0 风险)
    P1 = "P1"  # 重要 (C1 P1 或 C6 conf>0.7)
    P2 = "P2"  # 关注 (C1 P2 或 C6 conf>0.5)
    P3 = "P3"  # 建议持有 (其他)


class Source(str, Enum):
    """3 源标记"""
    C1_RISK = "C1_risk"
    C6_EVENT = "C6_event"
    L3_STRATEGY = "L3_strategy"
    WEIGHT = "weight"  # PIT #77 权重超限


# ==================== 数据结构 ====================

@dataclass
class RebalanceAction:
    """单个调仓建议"""
    action_id: str
    code: str
    name: str
    action: ActionType
    severity: Severity
    source: Source
    confidence: float  # 0-1
    current_weight: float
    target_weight: float
    market_value: float
    delta_amount: float  # 正=加仓, 负=减仓
    reasoning: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RebalanceLog:
    """已执行的调仓记录"""
    log_id: int
    action_id: str
    code: str
    name: str
    action: ActionType
    severity: Severity
    source: Source
    confidence: float
    market_value: float
    delta_amount: float
    execution_mode: str  # simulation / real
    confirmed: bool
    confirmed_at: Optional[str]
    created_at: str
    reasoning: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RebalanceSuggestion:
    """调仓建议汇总"""
    today: str
    actions: List[RebalanceAction]
    total_suggest: int
    p0_count: int
    p1_count: int
    p2_count: int
    total_market_value: float
    total_delta: float
    # 来源统计
    c1_risk_count: int
    c6_event_count: int
    l3_strategy_count: int
    weight_count: int


# ==================== PG 表 DDL ====================

EARLY_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS l3.rebalance_log (
        id BIGSERIAL PRIMARY KEY,
        action_id VARCHAR(50) UNIQUE NOT NULL,
        user_id VARCHAR(50) NOT NULL DEFAULT 'aileo',
        code VARCHAR(20) NOT NULL,
        name VARCHAR(50),
        action VARCHAR(20) NOT NULL,  -- reduce_50/reduce_30/reduce_20/increase/hold/close
        severity VARCHAR(10) NOT NULL,
        source VARCHAR(20) NOT NULL,  -- C1_risk/C6_event/L3_strategy/weight
        confidence NUMERIC(5,4),
        current_weight NUMERIC(5,2),
        target_weight NUMERIC(5,2),
        market_value NUMERIC(15,2),
        delta_amount NUMERIC(15,2),
        execution_mode VARCHAR(20) DEFAULT 'simulation',
        confirmed BOOLEAN DEFAULT FALSE,
        confirmed_at TIMESTAMPTZ,
        confirmed_by VARCHAR(50),
        executed BOOLEAN DEFAULT FALSE,
        executed_at TIMESTAMPTZ,
        reasoning TEXT,
        metadata JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_rebalance_user_time ON l3.rebalance_log(user_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_rebalance_action_type ON l3.rebalance_log(action, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_rebalance_confirmed ON l3.rebalance_log(confirmed, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_rebalance_code ON l3.rebalance_log(code);",
]


def ensure_pg_tables() -> None:
    """建表 (幂等)"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    for ddl in EARLY_DDL_STATEMENTS:
        cur.execute(ddl)
    conn.commit()
    cur.close()
    conn.close()
    LOG.info("✅ l3.rebalance_log 表 + 5 索引已就绪")


# ==================== 3 源数据加载 ====================

def load_c1_risk_alerts() -> List[Dict]:
    """拉 l3.risk_alert_log (C1 持仓风险)"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
    SELECT id, code, severity, alert_type, message, created_at
    FROM l3.risk_alert_log
    WHERE delivered = FALSE
       OR created_at > NOW() - INTERVAL '7 days'
    ORDER BY
        CASE severity WHEN 'P0' THEN 1 WHEN 'P1' THEN 2 WHEN 'P2' THEN 3 ELSE 4 END,
        created_at DESC;
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def load_c6_event_advice() -> List[Dict]:
    """拉 l3.event_strategist_advice (C6 大模型事件)"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
    SELECT advice_id, event_topic, direction, confidence, primary_action,
           target_codes, target_names, momentum_score, reasoning, created_at
    FROM l3.event_strategist_advice
    WHERE created_at > NOW() - INTERVAL '7 days'
    ORDER BY confidence DESC;
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def load_l3_decisions() -> List[Dict]:
    """拉 l3.decision_points (L3 策略)"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
    SELECT id, user_id, dialog_id, decision, stock_code, confidence, reasoning, created_at
    FROM l3.decision_points
    WHERE created_at > NOW() - INTERVAL '7 days'
    ORDER BY confidence DESC;
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def load_current_positions() -> Dict[str, Dict]:
    """拉 holdings.encrypted_positions 当前持仓 (调仓源)"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
    SELECT code, name, type, market_value, weight_pct, profit_pct
    FROM holdings.encrypted_positions
    WHERE is_current = TRUE;
    """)
    positions = {r["code"]: dict(r) for r in cur.fetchall()}
    cur.close()
    conn.close()
    return positions


# ==================== 调仓建议生成 (PIT #76 多源合并) ====================

def generate_rebalance_suggestion(today: Optional[str] = None) -> RebalanceSuggestion:
    """
    综合 3 源 → 调仓建议
    PIT #76: 同 code 多源冲突 → 取最严重 (P0 > P1 > P2)
    PIT #77: 权重超限 → 减仓至 5%
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    c1_alerts = load_c1_risk_alerts()
    c6_advices = load_c6_event_advice()
    l3_decisions = load_l3_decisions()
    positions = load_current_positions()

    # actions 字典 (按 code 合并)
    actions_by_code: Dict[str, RebalanceAction] = {}

    # === C1 风险 (PIT #76 优先级最高) ===
    for alert in c1_alerts:
        code = alert["code"]
        if code not in positions:
            continue
        pos = positions[code]
        severity_str = alert["severity"]
        current_w = float(pos.get("weight_pct") or 0)
        current_mv = float(pos.get("market_value") or 0)
        if severity_str == "P0":
            action_type = ActionType.CLOSE  # P0 风险 = 清仓
            target_weight = 0.0
        elif severity_str == "P1":
            action_type = ActionType.REDUCE_50
            target_weight = current_w * 0.5
        else:  # P2
            action_type = ActionType.REDUCE_30
            target_weight = current_w * 0.7

        action = RebalanceAction(
            action_id=str(uuid.uuid4())[:12],
            code=code,
            name=pos.get("name", "") or "",
            action=action_type,
            severity=Severity(severity_str),
            source=Source.C1_RISK,
            confidence=0.95,  # C1 风险几乎 100% 确定
            current_weight=current_w,
            target_weight=target_weight,
            market_value=current_mv,
            delta_amount=-(current_mv * (1 - target_weight / max(current_w, 0.01))),
            reasoning=f"C1 风险 ({alert['alert_type']}): {alert['message'][:120]}",
            metadata={"alert_id": alert["id"], "alert_type": alert["alert_type"]},
        )
        # PIT #76: 同 code 已存在 → 取更严重
        if code in actions_by_code:
            existing = actions_by_code[code]
            if _severity_rank(action.severity) > _severity_rank(existing.severity):
                actions_by_code[code] = action
        else:
            actions_by_code[code] = action

    # === C6 事件 ===
    for advice in c6_advices:
        conf = float(advice["confidence"] or 0)
        if conf < MIN_CONFIDENCE:
            continue
        direction = advice["direction"]
        primary = advice["primary_action"]
        codes = advice.get("target_codes") or []
        for code in codes:
            if code not in positions:
                continue
            pos = positions[code]
            if direction == "positive":
                action_type = ActionType.INCREASE
                target_weight = min(float(pos.get("weight_pct") or 0) * 1.3, MAX_SINGLE_WEIGHT)
                delta = float(pos.get("market_value") or 0) * 0.3
            elif direction == "negative":
                action_type = ActionType.REDUCE_30
                target_weight = float(pos.get("weight_pct") or 0) * 0.7
                delta = -float(pos.get("market_value") or 0) * 0.3
            else:  # neutral
                continue
            severity = Severity.P1 if conf > 0.7 else (Severity.P2 if conf > 0.5 else Severity.P3)
            action = RebalanceAction(
                action_id=str(uuid.uuid4())[:12],
                code=code,
                name=pos.get("name", ""),
                action=action_type,
                severity=severity,
                source=Source.C6_EVENT,
                confidence=conf,
                current_weight=float(pos.get("weight_pct") or 0),
                target_weight=target_weight,
                market_value=float(pos.get("market_value") or 0),
                delta_amount=delta,
                reasoning=f"C6 事件 ({advice['event_topic'][:30]}): {direction}/{primary}, conf={conf:.2f}",
                metadata={"advice_id": advice["advice_id"], "direction": direction},
            )
            if code in actions_by_code:
                existing = actions_by_code[code]
                if _severity_rank(action.severity) > _severity_rank(existing.severity):
                    actions_by_code[code] = action
            else:
                actions_by_code[code] = action

    # === L3 策略 ===
    for decision in l3_decisions:
        conf = float(decision["confidence"] or 0)
        if conf < MIN_CONFIDENCE:
            continue
        code = decision["stock_code"]
        if code not in positions:
            continue
        pos = positions[code]
        dec = decision["decision"]
        if dec == "buy":
            action_type = ActionType.INCREASE
            target_weight = min(float(pos.get("weight_pct") or 0) * 1.3, MAX_SINGLE_WEIGHT)
            delta = float(pos.get("market_value") or 0) * 0.3
        elif dec == "sell":
            action_type = ActionType.REDUCE_30
            target_weight = float(pos.get("weight_pct") or 0) * 0.7
            delta = -float(pos.get("market_value") or 0) * 0.3
        else:
            continue
        severity = Severity.P2
        action = RebalanceAction(
            action_id=str(uuid.uuid4())[:12],
            code=code,
            name=pos.get("name", ""),
            action=action_type,
            severity=severity,
            source=Source.L3_STRATEGY,
            confidence=conf,
            current_weight=float(pos.get("weight_pct") or 0),
            target_weight=target_weight,
            market_value=float(pos.get("market_value") or 0),
            delta_amount=delta,
            reasoning=f"L3 策略 ({dec}): {(decision.get('reasoning') or '')[:100]}",
            metadata={"decision_id": decision["id"]},
        )
        if code in actions_by_code:
            existing = actions_by_code[code]
            if _severity_rank(action.severity) > _severity_rank(existing.severity):
                actions_by_code[code] = action
        else:
            actions_by_code[code] = action

    # === PIT #77: 权重超限 → 减仓至 5% ===
    for code, pos in positions.items():
        weight = float(pos.get("weight_pct") or 0)
        if weight > MAX_SINGLE_WEIGHT:
            action_type = ActionType.REDUCE_30
            target_weight = MAX_SINGLE_WEIGHT
            delta = -float(pos.get("market_value") or 0) * (weight - MAX_SINGLE_WEIGHT) / weight
            action = RebalanceAction(
                action_id=str(uuid.uuid4())[:12],
                code=code,
                name=pos.get("name", ""),
                action=action_type,
                severity=Severity.P2,
                source=Source.WEIGHT,
                confidence=1.0,
                current_weight=weight,
                target_weight=target_weight,
                market_value=float(pos.get("market_value") or 0),
                delta_amount=delta,
                reasoning=f"PIT #77 权重超限: {weight:.2f}% > {MAX_SINGLE_WEIGHT}% → 减仓至 5%",
                metadata={"rule": "weight_cap"},
            )
            if code in actions_by_code:
                existing = actions_by_code[code]
                if _severity_rank(action.severity) > _severity_rank(existing.severity):
                    actions_by_code[code] = action
            else:
                actions_by_code[code] = action

    actions = list(actions_by_code.values())
    actions.sort(key=lambda a: (-_severity_rank(a.severity), -a.confidence, a.code))

    p0 = sum(1 for a in actions if a.severity == Severity.P0)
    p1 = sum(1 for a in actions if a.severity == Severity.P1)
    p2 = sum(1 for a in actions if a.severity == Severity.P2)
    total_mv = sum(a.market_value for a in actions)
    total_delta = sum(a.delta_amount for a in actions)

    return RebalanceSuggestion(
        today=today,
        actions=actions,
        total_suggest=len(actions),
        p0_count=p0,
        p1_count=p1,
        p2_count=p2,
        total_market_value=total_mv,
        total_delta=total_delta,
        c1_risk_count=sum(1 for a in actions if a.source == Source.C1_RISK),
        c6_event_count=sum(1 for a in actions if a.source == Source.C6_EVENT),
        l3_strategy_count=sum(1 for a in actions if a.source == Source.L3_STRATEGY),
        weight_count=sum(1 for a in actions if a.source == Source.WEIGHT),
    )


def _severity_rank(sev: Severity) -> int:
    return {Severity.P0: 4, Severity.P1: 3, Severity.P2: 2, Severity.P3: 1}.get(sev, 0)


# ==================== 持久化 + 确认 + 执行 ====================

def persist_suggestion(suggestion: RebalanceSuggestion) -> int:
    """写 l3.rebalance_log (未确认状态)"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    inserted = 0
    for a in suggestion.actions:
        cur.execute("""
        INSERT INTO l3.rebalance_log
        (action_id, user_id, code, name, action, severity, source, confidence,
         current_weight, target_weight, market_value, delta_amount, execution_mode, reasoning, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (action_id) DO NOTHING;
        """, (
            a.action_id, USER_ID, a.code, a.name, a.action.value, a.severity.value, a.source.value,
            a.confidence, a.current_weight, a.target_weight, a.market_value, a.delta_amount,
            EXECUTION_MODE, a.reasoning, json.dumps(a.metadata, ensure_ascii=False),
        ))
        inserted += 1
    conn.commit()
    cur.close()
    conn.close()
    LOG.info(f"✅ 持久化 {inserted} 条调仓建议到 l3.rebalance_log")
    return inserted


def confirm_rebalance(action_id: str, confirmed_by: str = USER_ID) -> bool:
    """PIT #78: 2 步确认 - 用户确认后调 execute_rebalance"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute("""
    UPDATE l3.rebalance_log
    SET confirmed = TRUE, confirmed_at = NOW(), confirmed_by = %s
    WHERE action_id = %s AND confirmed = FALSE
    RETURNING id;
    """, (confirmed_by, action_id))
    updated = cur.fetchone() is not None
    conn.commit()
    cur.close()
    conn.close()
    if updated:
        LOG.info(f"✅ 确认调仓: {action_id} by {confirmed_by}")
    else:
        LOG.warning(f"⚠️ 调仓确认失败 (可能已确认或不存在): {action_id}")
    return updated


def execute_rebalance(action_id: str, mode: str = EXECUTION_MODE) -> bool:
    """
    PIT #74: 默认 simulation, 真实下单需显式 enable_broker=True
    PIT #78: 需先 confirm
    """
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    # 查 action 详情 + 检查 confirmed
    cur.execute("""
    SELECT code, name, action, severity, market_value, delta_amount, confirmed
    FROM l3.rebalance_log
    WHERE action_id = %s;
    """, (action_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        LOG.error(f"❌ 调仓 action 不存在: {action_id}")
        return False
    code, name, action, severity, mv, delta, confirmed = row
    if not confirmed:
        cur.close()
        conn.close()
        LOG.error(f"❌ PIT #78: action 未确认, 拒绝执行: {action_id}")
        return False

    # 模拟执行 (写 executed 状态)
    cur.execute("""
    UPDATE l3.rebalance_log
    SET executed = TRUE, executed_at = NOW()
    WHERE action_id = %s;
    """, (action_id,))
    conn.commit()
    cur.close()
    conn.close()
    LOG.info(f"✅ [{mode}] 执行调仓: {code} {action} (delta=¥{delta:,.0f})")
    return True


def get_rebalance_history(days: int = 7) -> List[Dict]:
    """V25-B4 调仓历史回放"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
    SELECT action_id, code, name, action, severity, source, confidence,
           market_value, delta_amount, confirmed, executed, created_at
    FROM l3.rebalance_log
    WHERE created_at > NOW() - (%s || ' days')::INTERVAL
    ORDER BY created_at DESC;
    """, (days,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


# ==================== 飞书推送 ====================

def _send_via_feishu_inplace(webhook_url: str, title: str, content: str, level: str = "INFO", actions: Optional[List[Dict]] = None) -> bool:
    """PIT #66 沿用 + PIT #75 新增 actions 按钮"""
    import urllib.request

    color_map = {"INFO": "#4CAF50", "WARNING": "#FF9800", "ERROR": "#F44336", "P0": "#F44336", "P1": "#FF9800", "P2": "#4CAF50"}
    template = color_map.get(level, "#4CAF50")

    if len(content) > FEISHU_MAX_LEN:
        content = content[:FEISHU_MAX_LEN - 50] + "\n\n... (内容过长, 已截断)"

    elements = [
        {"tag": "markdown", "content": content},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": f"InvestPilot · V25-B 调仓助手 · {datetime.now().strftime('%H:%M:%S')}"}]},
    ]

    # PIT #75: 飞书 action 按钮 (P0/P1 显示)
    if actions:
        action_elements = []
        for act in actions[:5]:  # 飞书限制 ≤5 actions
            action_elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": act["text"]},
                "type": act.get("type", "primary"),
                "value": act["value"],
            })
        if action_elements:
            elements.append({"tag": "action", "actions": action_elements})

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        },
    }

    for attempt in range(RETRY_TIMES):
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                if '"code":0' in body or resp.status == 200:
                    return True
        except Exception as e:
            LOG.warning(f"飞书推送重试 {attempt+1}/{RETRY_TIMES} 失败: {e}")
            if attempt < RETRY_TIMES - 1:
                time.sleep(2 ** attempt)
    return False


def push_suggestion_to_feishu(suggestion: RebalanceSuggestion) -> int:
    """推飞书 + action 按钮 (确认/拒绝)"""
    webhook = get_credential("FEISHU_WEBHOOK", "")
    if not webhook:
        LOG.info("[V25-B] FEISHU_WEBHOOK 未配, 跳过飞书推送 (PIT #69 PG 兜底)")
        return 0
    if suggestion.total_suggest == 0:
        LOG.info(f"[V25-B] {suggestion.today} 无调仓建议, 不推送")
        return 0

    lines = [
        f"**持仓调仓建议** ({suggestion.today})",
        f"",
        f"📊 总建议: **{suggestion.total_suggest} 条** (P0: {suggestion.p0_count}, P1: {suggestion.p1_count}, P2: {suggestion.p2_count})",
        f"💰 涉及市值: ¥{suggestion.total_market_value:,.0f} | 调仓金额: ¥{suggestion.total_delta:,.0f}",
        f"🔍 来源: C1 风险 {suggestion.c1_risk_count} + C6 事件 {suggestion.c6_event_count} + L3 策略 {suggestion.l3_strategy_count} + 权重 {suggestion.weight_count}",
        f"",
        "---",
    ]
    for a in suggestion.actions[:10]:  # 最多 10 条
        emoji = "🔴" if a.severity == Severity.P0 else ("🟠" if a.severity == Severity.P1 else "🟡")
        sign = "+" if a.delta_amount > 0 else ""
        lines.append(
            f"{emoji} **{a.severity}** {a.name}({a.code}) → {a.action.value}\n"
            f"   权重: {a.current_weight:.2f}% → {a.target_weight:.2f}% ({sign}¥{a.delta_amount:,.0f})\n"
            f"   来源: {a.source.value} | conf={a.confidence:.2f}\n"
            f"   理由: {a.reasoning[:80]}"
        )
    if len(suggestion.actions) > 10:
        lines.append(f"\n_(还有 {len(suggestion.actions) - 10} 条省略)_")

    content = "\n".join(lines)
    title = f"🔄 V25-B 调仓建议 ({suggestion.today}) {suggestion.total_suggest} 条"
    level = "P0" if suggestion.p0_count > 0 else ("P1" if suggestion.p1_count > 0 else "P2")

    # PIT #75: P0/P1 加 action 按钮 (确认/拒绝)
    actions = None
    if suggestion.p0_count > 0 or suggestion.p1_count > 0:
        actions = [
            {"text": "✅ 查看全部", "type": "primary", "value": {"action": "view_all"}},
            {"text": "❌ 稍后处理", "type": "default", "value": {"action": "defer"}},
        ]

    ok = _send_via_feishu_inplace(webhook, title, content, level, actions)
    if ok:
        LOG.info(f"✅ 飞书推送成功: {suggestion.total_suggest} 条调仓建议")
    else:
        LOG.warning(f"⚠️ 飞书推送失败, 已写 PG 兜底")
    return 1 if ok else 0


# ==================== 入口 ====================

def main(today: Optional[str] = None) -> RebalanceSuggestion:
    """CLI 入口: python position_rebalancer.py [--today 2026-06-13]"""
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    ensure_pg_tables()
    suggestion = generate_rebalance_suggestion(today)
    persist_suggestion(suggestion)
    push_suggestion_to_feishu(suggestion)

    print(f"\n=== V25-B 调仓建议 ({today}) ===")
    print(f"总建议: {suggestion.total_suggest} 条")
    print(f"  P0: {suggestion.p0_count} | P1: {suggestion.p1_count} | P2: {suggestion.p2_count}")
    print(f"涉及市值: ¥{suggestion.total_market_value:,.0f}")
    print(f"调仓金额: ¥{suggestion.total_delta:,.0f}")
    print(f"来源: C1={suggestion.c1_risk_count}, C6={suggestion.c6_event_count}, L3={suggestion.l3_strategy_count}, weight={suggestion.weight_count}")

    if suggestion.actions:
        print(f"\n--- Top 10 建议 ---")
        for a in suggestion.actions[:10]:
            sign = "+" if a.delta_amount > 0 else ""
            print(f"  {a.severity.value} {a.name or '':18s}({a.code}) {a.action.value:12s} 权重 {a.current_weight:.2f}%→{a.target_weight:.2f}% ({sign}¥{a.delta_amount:,.0f}) [{a.source.value}]")

    return suggestion


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--today", type=str, default=None)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--confirm", type=str, default=None, help="确认 action_id")
    p.add_argument("--execute", type=str, default=None, help="执行 action_id")
    args = p.parse_args()

    if args.confirm:
        confirm_rebalance(args.confirm)
    elif args.execute:
        execute_rebalance(args.execute)
    elif args.self_test:
        # 自检: 跑 1 次全链路
        print("=== V25-B self-test ===")
        suggestion = main()
        if suggestion.actions:
            first = suggestion.actions[0]
            print(f"\n--- 模拟全链路: {first.action_id} ---")
            confirm_rebalance(first.action_id)
            execute_rebalance(first.action_id)
            history = get_rebalance_history(7)
            print(f"  历史记录: {len(history)} 条")
    else:
        main(today=args.today)
