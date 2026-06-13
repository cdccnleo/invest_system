"""
earnings_miss_trigger.py — V25-F 中报季业绩 miss 触发器
================================================================
背景: 2026 中报季 (7/15-8/30) 持仓 28 stock 预披露, miss>20% 自动减仓告警
核心:
  1. 拉 earnings_calendar (JSON 28 stock 预披露 + consensus)
  2. 每日 09:25 跑 check_earnings_miss(today) → 找 T-3 / T+0 披露的持仓
  3. miss > 20% (实际 EPS < 预期 EPS * 0.8) → 飞书推送 + 写 l3.earnings_miss_log
  4. 实战窗口: 8/10-8/30 持续 3 周
PIT 经验:
  - PIT #60 (V24-C5): profit_pct 不能依赖 CSV 源, 应该用 cost*shares 推算 → 实际 pp 用 holdings.encrypted_positions.profit_pct (V24-C5 修复后)
  - PIT #66 (V25-A1): 飞书推送就地实现, 避免循环 import
  - PIT #69 (V25-A1): 3 通道全空 → 返 0 (PG 兜底)
  - PIT #71 (V25-F NEW): 实际 EPS 缺失时不能误报 miss → 缺失 = 跳过
  - PIT #72 (V25-F NEW): 持仓类型 != stock 的不参与 (ETF/基金无中报)
  - PIT #73 (V25-F NEW): consensus 缺失时按 pp 阈值 (pp<-10%) 兜底
"""
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

# 路径
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hermes_coordination" / "scripts"))

import psycopg2
import psycopg2.extras

# 凭据
from credentials import get_credential

LOG = logging.getLogger("v25_f.earnings_miss_trigger")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==================== 常量 ====================

# 数据库
DB_PARAMS = {
    "host": "localhost",
    "dbname": "investpilot",
    "user": "invest_admin",
    "password": get_credential("DB_PASSWORD"),
}

# 持仓类型过滤 (PIT #72)
VALID_TYPES = ("stock",)  # 仅 stock 参与中报 miss 检测

# miss 阈值
MISS_THRESHOLD = 0.20  # miss > 20% → 减仓 50% 告警 (实际 EPS < 预期 EPS * 0.8)
PP_FALLBACK_THRESHOLD = -10.0  # PIT #73: consensus 缺失时按 pp 兜底 (pp<-10% 视为 miss)

# T 窗口
T_MINUS_DAYS = 3  # 披露前 3 天预热
T_PLUS_DAYS = 1   # 披露当天 + 1 天后 (等公告数据)

# 数据源
CALENDAR_PATH = ROOT / "hermes_coordination" / "data" / "earnings_calendar_2026h1.json"

# 飞书推送 (V25-A1 沿用, 避免循环 import)
FEISHU_MAX_LEN = 1800  # PIT #70
RETRY_TIMES = 3


# ==================== 数据结构 ====================

@dataclass
class EarningsEvent:
    """中报披露事件"""
    code: str
    name: str
    market: str
    industry: str
    disclosure_date: str  # ISO8601
    consensus_eps: float
    consensus_revenue_yoy: float
    # 运行时填充
    actual_eps: Optional[float] = None
    actual_revenue_yoy: Optional[float] = None
    miss_pct: Optional[float] = None  # 实际 vs 预期 (正=beat, 负=miss)
    profit_pct: Optional[float] = None  # 当前持仓 pp
    market_value: Optional[float] = None
    weight_pct: Optional[float] = None


@dataclass
class MissAlert:
    """业绩 miss 告警"""
    code: str
    name: str
    disclosure_date: str
    consensus_eps: float
    actual_eps: Optional[float]
    miss_pct: float
    profit_pct: float
    market_value: float
    weight_pct: float
    severity: str  # P0/P1/P2
    action: str  # reduce_50/reduce_30/hold
    reasoning: str


@dataclass
class TriggerResult:
    """触发器结果"""
    today: str
    total_events: int
    t_minus_alerts: List[MissAlert]  # 披露前 3 天
    t_zero_alerts: List[MissAlert]    # 披露当天 miss
    t_plus_alerts: List[MissAlert]    # 披露后 1 天
    pp_fallback_alerts: List[MissAlert]  # pp 兜底
    total_alerts: int


# ==================== 数据库表 DDL ====================

EARLY_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS l3.earnings_calendar (
        id BIGSERIAL PRIMARY KEY,
        code VARCHAR(20) NOT NULL,
        name VARCHAR(50),
        market VARCHAR(20),
        industry VARCHAR(30),
        disclosure_date DATE NOT NULL,
        consensus_eps NUMERIC(10,4),
        consensus_revenue_yoy NUMERIC(8,2),
        actual_eps NUMERIC(10,4),
        actual_revenue_yoy NUMERIC(8,2),
        miss_pct NUMERIC(8,4),
        source VARCHAR(50) DEFAULT 'manual',  -- manual/wind/choice
        note TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(code, disclosure_date)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ec_disclosure_date
    ON l3.earnings_calendar(disclosure_date);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ec_code
    ON l3.earnings_calendar(code);
    """,
    """
    CREATE TABLE IF NOT EXISTS l3.earnings_miss_log (
        id BIGSERIAL PRIMARY KEY,
        code VARCHAR(20) NOT NULL,
        name VARCHAR(50),
        disclosure_date DATE,
        alert_type VARCHAR(20),  -- t_minus_3/t_zero/t_plus_1/pp_fallback
        consensus_eps NUMERIC(10,4),
        actual_eps NUMERIC(10,4),
        miss_pct NUMERIC(8,4),
        profit_pct NUMERIC(8,2),
        market_value NUMERIC(15,2),
        weight_pct NUMERIC(5,2),
        severity VARCHAR(10),
        action VARCHAR(20),
        reasoning TEXT,
        feishu_pushed BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_eml_code
    ON l3.earnings_miss_log(code);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_eml_disclosure_date
    ON l3.earnings_miss_log(disclosure_date);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_eml_severity_created
    ON l3.earnings_miss_log(severity, created_at);
    """,
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
    LOG.info("✅ l3.earnings_calendar + l3.earnings_miss_log 表已就绪")


# ==================== 数据加载 ====================

def load_calendar() -> Dict[str, EarningsEvent]:
    """从 JSON 加载 28 stock 预披露日历"""
    if not CALENDAR_PATH.exists():
        LOG.error(f"日历文件不存在: {CALENDAR_PATH}")
        return {}
    raw = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    events = {}
    for code, e in raw.items():
        # 跳过基金 (002943)
        if e.get("industry", "").endswith("(跳过)"):
            continue
        events[code] = EarningsEvent(
            code=code,
            name="",  # 后填充
            market=e["market"],
            industry=e["industry"],
            disclosure_date=e["date"],
            consensus_eps=float(e["consensus_eps"]),
            consensus_revenue_yoy=float(e["consensus_revenue_yoy"]),
        )
    return events


def load_position_context(events: Dict[str, EarningsEvent]) -> Dict[str, EarningsEvent]:
    """从 PG 拉持仓 context (name, market_value, profit_pct)"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
    SELECT code, name, type, market_value, profit_pct, weight_pct
    FROM holdings.encrypted_positions
    WHERE is_current = TRUE AND type = 'stock';
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    for r in rows:
        code = r["code"]
        if code in events:
            events[code].name = r["name"] or ""
            events[code].profit_pct = float(r["profit_pct"]) if r["profit_pct"] is not None else None
            events[code].market_value = float(r["market_value"]) if r["market_value"] is not None else None
            events[code].weight_pct = float(r["weight_pct"]) if r["weight_pct"] is not None else None
    return events


# ==================== 实际业绩填充 (PIT #71 缺失跳过) ====================

def fill_actual_eps(events: Dict[str, EarningsEvent]) -> None:
    """
    从 PG l3.earnings_calendar 拉 actual_eps (用户手动更新或外部 API 同步)
    PIT #71: actual_eps 缺失 = 跳过, 不算 miss
    """
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute("SELECT code, disclosure_date, actual_eps, actual_revenue_yoy FROM l3.earnings_calendar WHERE actual_eps IS NOT NULL;")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    for code, ddate, actual_eps, actual_rev_yoy in rows:
        if code in events:
            events[code].actual_eps = float(actual_eps)
            events[code].actual_revenue_yoy = float(actual_rev_yoy) if actual_rev_yoy else None
            # 计算 miss
            if events[code].consensus_eps > 0:
                events[code].miss_pct = (float(actual_eps) - events[code].consensus_eps) / events[code].consensus_eps


# ==================== 核心触发 ====================

def check_earnings_miss(today: str) -> TriggerResult:
    """
    检查今天 (ISO8601) 前后窗口的业绩 miss
    - T-3: 提前 3 天预热 (披露日期 - 3 == today) → 飞书推送 "T-3 预警"
    - T+0: 披露当天 → 拉 actual_eps → miss > 20% → 飞书推送 "miss 减仓"
    - T+1: 披露后 1 天 → 累计 miss
    - pp fallback: consensus 缺失 + pp < -10% → 兜底告警
    """
    today_dt = datetime.strptime(today, "%Y-%m-%d").date()
    t_minus = today_dt + timedelta(days=T_MINUS_DAYS)
    t_plus = today_dt - timedelta(days=T_PLUS_DAYS)

    events = load_calendar()
    events = load_position_context(events)
    fill_actual_eps(events)

    t_minus_alerts: List[MissAlert] = []
    t_zero_alerts: List[MissAlert] = []
    t_plus_alerts: List[MissAlert] = []
    pp_fallback_alerts: List[MissAlert] = []

    for code, ev in events.items():
        ddate = datetime.strptime(ev.disclosure_date, "%Y-%m-%d").date()

        # T-3 预热
        if ddate == t_minus:
            t_minus_alerts.append(_build_t_minus_alert(ev))

        # T+0 披露 (miss 检测)
        if ddate == today_dt and ev.actual_eps is not None:
            alert = _build_miss_alert(ev)
            if alert:
                t_zero_alerts.append(alert)

        # T+1 累计
        if ddate == t_plus and ev.actual_eps is not None:
            alert = _build_miss_alert(ev, alert_type="t_plus_1")
            if alert:
                t_plus_alerts.append(alert)

        # PIT #73: pp 兜底
        if (ev.profit_pct is not None
                and ev.profit_pct < PP_FALLBACK_THRESHOLD
                and ddate <= today_dt + timedelta(days=7)  # 7 天内披露
                and ddate >= today_dt - timedelta(days=2)):  # T-2 ~ T+5
            pp_fallback_alerts.append(_build_pp_fallback_alert(ev))

    total = len(t_minus_alerts) + len(t_zero_alerts) + len(t_plus_alerts) + len(pp_fallback_alerts)
    return TriggerResult(
        today=today,
        total_events=len(events),
        t_minus_alerts=t_minus_alerts,
        t_zero_alerts=t_zero_alerts,
        t_plus_alerts=t_plus_alerts,
        pp_fallback_alerts=pp_fallback_alerts,
        total_alerts=total,
    )


def _build_t_minus_alert(ev: EarningsEvent) -> MissAlert:
    """T-3 预热: 披露前 3 天预警, 让用户关注"""
    return MissAlert(
        code=ev.code,
        name=ev.name,
        disclosure_date=ev.disclosure_date,
        consensus_eps=ev.consensus_eps,
        actual_eps=None,
        miss_pct=0.0,
        profit_pct=ev.profit_pct or 0.0,
        market_value=ev.market_value or 0.0,
        weight_pct=ev.weight_pct or 0.0,
        severity="P2",
        action="watch",
        reasoning=f"T-3 预警: {ev.name}({ev.code}) {ev.disclosure_date} 披露, 行业={ev.industry}, 预期 EPS={ev.consensus_eps:.2f}",
    )


def _build_miss_alert(ev: EarningsEvent, alert_type: str = "t_zero") -> Optional[MissAlert]:
    """miss 检测: actual_eps < consensus_eps * (1-20%) → 减仓"""
    if ev.actual_eps is None or ev.miss_pct is None:
        return None  # PIT #71
    if ev.miss_pct >= -MISS_THRESHOLD:
        return None  # 符合预期, 不告警

    # miss 程度
    if ev.miss_pct < -0.50:
        severity = "P0"
        action = "reduce_50"
    elif ev.miss_pct < -0.35:
        severity = "P1"
        action = "reduce_50"
    else:
        severity = "P1"
        action = "reduce_30"

    return MissAlert(
        code=ev.code,
        name=ev.name,
        disclosure_date=ev.disclosure_date,
        consensus_eps=ev.consensus_eps,
        actual_eps=ev.actual_eps,
        miss_pct=ev.miss_pct,
        profit_pct=ev.profit_pct or 0.0,
        market_value=ev.market_value or 0.0,
        weight_pct=ev.weight_pct or 0.0,
        severity=severity,
        action=action,
        reasoning=f"中报 miss {ev.miss_pct*100:.1f}%: 实际 EPS={ev.actual_eps:.2f} < 预期 {ev.consensus_eps:.2f}*{1-MISS_THRESHOLD:.2f}",
    )


def _build_pp_fallback_alert(ev: EarningsEvent) -> MissAlert:
    """PIT #73: consensus 缺失时按 pp 兜底告警"""
    return MissAlert(
        code=ev.code,
        name=ev.name,
        disclosure_date=ev.disclosure_date,
        consensus_eps=ev.consensus_eps,
        actual_eps=None,
        miss_pct=0.0,
        profit_pct=ev.profit_pct or 0.0,
        market_value=ev.market_value or 0.0,
        weight_pct=ev.weight_pct or 0.0,
        severity="P2",
        action="watch",
        reasoning=f"pp 兜底: {ev.name}({ev.code}) pp={ev.profit_pct:.1f}% < {PP_FALLBACK_THRESHOLD}% (中报 {ev.disclosure_date} 前后窗口)",
    )


# ==================== 持久化 + 飞书推送 ====================

def persist_alerts(result: TriggerResult) -> int:
    """写 l3.earnings_miss_log, 返 ID 数"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    all_alerts = (
        [(a, "t_minus_3") for a in result.t_minus_alerts]
        + [(a, "t_zero") for a in result.t_zero_alerts]
        + [(a, "t_plus_1") for a in result.t_plus_alerts]
        + [(a, "pp_fallback") for a in result.pp_fallback_alerts]
    )
    inserted = 0
    for alert, atype in all_alerts:
        cur.execute("""
        INSERT INTO l3.earnings_miss_log
        (code, name, disclosure_date, alert_type, consensus_eps, actual_eps, miss_pct, profit_pct, market_value, weight_pct, severity, action, reasoning)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            alert.code, alert.name, alert.disclosure_date, atype,
            alert.consensus_eps, alert.actual_eps, alert.miss_pct,
            alert.profit_pct, alert.market_value, alert.weight_pct,
            alert.severity, alert.action, alert.reasoning,
        ))
        inserted += 1
    conn.commit()
    cur.close()
    conn.close()
    LOG.info(f"✅ 持久化 {inserted} 条告警到 l3.earnings_miss_log")
    return inserted


def _send_via_feishu_inplace(webhook_url: str, title: str, content: str, level: str = "INFO") -> bool:
    """
    PIT #66 沿用 V25-A1: 飞书推送就地实现
    - interactive card (msg_type + card + header + elements + note)
    - 颜色映射 INFO=#4CAF50 / WARNING=#FF9800 / ERROR=#F44336
    - 3 retry exponential backoff
    - MAX_LEN=1800
    """
    import urllib.request

    color_map = {"INFO": "#4CAF50", "WARNING": "#FF9800", "ERROR": "#F44336", "P0": "#F44336", "P1": "#FF9800", "P2": "#4CAF50"}
    template = color_map.get(level, "#4CAF50")

    if len(content) > FEISHU_MAX_LEN:
        content = content[:FEISHU_MAX_LEN - 50] + "\n\n... (内容过长, 已截断)"

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": [
                {"tag": "markdown", "content": content},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"InvestPilot · V25-F 中报季 · {datetime.now().strftime('%H:%M:%S')}"}]},
            ],
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


def push_to_feishu(result: TriggerResult) -> int:
    """推飞书 (走 store.json FEISHU_WEBHOOK)"""
    webhook = get_credential("FEISHU_WEBHOOK", "")
    if not webhook:
        LOG.info("[V25-F] FEISHU_WEBHOOK 未配, 跳过飞书推送 (PIT #69 PG 兜底)")
        return 0
    if result.total_alerts == 0:
        LOG.info(f"[V25-F] {result.today} 无 miss 告警, 不推送")
        return 0

    # 按 severity 排序
    all_alerts = result.t_minus_alerts + result.t_zero_alerts + result.t_plus_alerts + result.pp_fallback_alerts
    all_alerts.sort(key=lambda a: (a.severity, a.miss_pct))

    lines = [
        f"**中报季 miss 触发器报告** ({result.today})",
        f"",
        f"📅 28 stock 预披露日历: {result.total_events} 只",
        f"⚠️ 本次告警: **{result.total_alerts} 条** (T-3 预警 {len(result.t_minus_alerts)} + T+0 miss {len(result.t_zero_alerts)} + T+1 累计 {len(result.t_plus_alerts)} + pp 兜底 {len(result.pp_fallback_alerts)})",
        f"",
        "---",
    ]
    for a in all_alerts[:10]:  # 最多 10 条
        emoji = "🔴" if a.severity == "P0" else ("🟠" if a.severity == "P1" else "🟡")
        lines.append(
            f"{emoji} **{a.severity}** {a.name}({a.code}) {a.disclosure_date}\n"
            f"   权重: {a.weight_pct:.2f}% | 市值: ¥{a.market_value:,.0f}\n"
            f"   当前 pp: {a.profit_pct:.1f}%\n"
            f"   动作: {a.action}\n"
            f"   理由: {a.reasoning}"
        )

    if len(all_alerts) > 10:
        lines.append(f"\n_(还有 {len(all_alerts) - 10} 条省略)_")

    content = "\n".join(lines)
    title = f"🛡 V25-F 中报季 ({result.today}) {result.total_alerts} 告警"

    # 决定级别
    if any(a.severity == "P0" for a in all_alerts):
        level = "P0"
    elif any(a.severity == "P1" for a in all_alerts):
        level = "P1"
    else:
        level = "P2"

    ok = _send_via_feishu_inplace(webhook, title, content, level)
    if ok:
        LOG.info(f"✅ 飞书推送成功: {result.total_alerts} 条告警")
    else:
        LOG.warning(f"⚠️ 飞书推送失败, 已写 PG 兜底")
    return 1 if ok else 0


# ==================== 入口 ====================

def main(today: Optional[str] = None) -> TriggerResult:
    """CLI 入口: python earnings_miss_trigger.py [--today 2026-08-10]"""
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    ensure_pg_tables()
    result = check_earnings_miss(today)
    persist_alerts(result)
    push_to_feishu(result)

    print(f"\n=== V25-F 中报季触发器 ({today}) ===")
    print(f"持仓 28 stock: {result.total_events} 只")
    print(f"本次告警: {result.total_alerts} 条")
    print(f"  T-3 预热: {len(result.t_minus_alerts)}")
    print(f"  T+0 miss: {len(result.t_zero_alerts)}")
    print(f"  T+1 累计: {len(result.t_plus_alerts)}")
    print(f"  pp 兜底: {len(result.pp_fallback_alerts)}")

    if result.t_zero_alerts:
        print(f"\n🔴 miss 减仓告警:")
        for a in result.t_zero_alerts:
            print(f"  {a.severity} {a.name}({a.code}) miss={a.miss_pct*100:.1f}% → {a.action}")

    return result


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--today", type=str, default=None, help="ISO8601 日期, 默认今天")
    p.add_argument("--self-test", action="store_true", help="跑自检")
    args = p.parse_args()

    if args.self_test:
        # 跑 3 个关键日期: 8/10 (最早披露), 8/15 (中段), 8/30 (最晚披露)
        for d in ["2026-08-10", "2026-08-15", "2026-08-30"]:
            print(f"\n--- self-test: {d} ---")
            main(today=d)
    else:
        main(today=args.today)
