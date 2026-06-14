"""
7d_report_generator.py — V25-G 7 天报告自动出
================================================================
背景: v2.5 plan 7 候选中 P0 (自动), 时间窗 6/20 自动出 7d 报告
核心:
  1. generate_7d_report — 综合持仓/盈亏/事件/建议 → 7d 报告
  2. push_report_to_feishu — 飞书推送 (V25-A1 PIT #66 沿用)
  3. persist_snapshot — l3.report_7d_snapshot 持久化 (idempotent, PIT #86)
  4. get_latest_snapshot — 7d 前快照查询 (PIT #85)

数据源 (5 源):
  - holdings.encrypted_positions (V24-C5 修复后, 45 持仓)
  - market.daily_quotes (45 只 6/9-6/13 实战)
  - l3.event_strategist_advice (V24-C6, 6 条)
  - l3.position_risk_snapshot (V24-C1, 1 行)
  - l3.event_backtest_log (V25-C, 14 评估)

PIT 经验:
  - PIT #84 (V25-G NEW): 总市值必须用 SUM(market_value), 不能 SUM(cost) (V24-C5 教训)
  - PIT #85 (V25-G NEW): 7d 报告 = 当前 vs 7d 前快照对比, 需保留 l3.report_7d_snapshot
  - PIT #86 (V25-G NEW): 报告 idempotent (ON CONFLICT date 覆盖写, 跟 V25-F 一致)
  - PIT #15 沿用: INTERVAL 子句用 f-string
  - PIT #16/#79 沿用: ts_code 标准化
  - PIT #66 沿用: 飞书推送就地实现
  - PIT #69 沿用: 3 通道全空 → 返 0
"""
import json
import logging
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

# 路径
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hermes_coordination" / "scripts"))

import psycopg2
import psycopg2.extras

from credentials import get_credential

LOG = logging.getLogger("v25_g.7d_report")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==================== 常量 ====================

DB_PARAMS = {
    "host": "localhost",
    "dbname": "investpilot",
    "user": "invest_admin",
    "password": get_credential("DB_PASSWORD"),
}

USER_ID = "aileo"
FEISHU_MAX_LEN = 1800
RETRY_TIMES = 3
WINDOW_DAYS = 7  # 7 天报告窗口


# ==================== 枚举 ====================

class ReportSection(str):
    """报告章节"""
    SUMMARY = "summary"           # 总览
    POSITION_CHANGE = "position_change"  # 持仓变化
    TOP_MOVERS = "top_movers"     # 涨跌幅排行
    EVENTS = "events"             # 事件回顾
    ACCURACY = "accuracy"         # 准确度评估
    RISK_ALERTS = "risk_alerts"   # 风险告警


# ==================== 数据结构 ====================

@dataclass
class PositionSummary:
    """持仓总览"""
    total_market_value: float  # 总市值
    total_pnl: float           # 总浮动盈亏
    avg_profit_pct: float      # 平均 pp
    win_count: int             # 盈利数
    lose_count: int            # 亏损数
    flat_count: int            # 持平数
    position_count: int        # 总持仓数
    stock_count: int           # stock 数
    fund_count: int            # fund/ETF 数


@dataclass
class PositionChange:
    """持仓变化 (vs 7d 前快照)"""
    code: str
    name: str
    current_weight: float
    prev_weight: float
    weight_delta: float
    current_pp: float
    prev_pp: float
    pp_delta: float


@dataclass
class TopMover:
    """涨跌幅排行"""
    code: str
    name: str
    pct_change: float
    current_price: float
    prev_price: float


@dataclass
class EventRecord:
    """事件记录"""
    event_id: int
    title: str
    published_at: str
    severity: Optional[str]
    source: str  # news / advice


@dataclass
class SnapshotReport:
    """7d 报告快照"""
    report_id: int
    report_date: str
    period_start: str
    period_end: str
    position_summary: PositionSummary
    position_changes: List[PositionChange]
    top_gainers: List[TopMover]
    top_losers: List[TopMover]
    events: List[EventRecord]
    # 准确度 (从 l3.event_backtest_log 拉)
    t3_accuracy: float
    total_evaluations: int
    # 风险告警 (从 l3.risk_alert_log 拉)
    p0_count: int
    p1_count: int
    p2_count: int
    # 推送状态
    feishu_pushed: bool
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ==================== PG 表 DDL ====================

EARLY_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS l3.report_7d_snapshot (
        id BIGSERIAL PRIMARY KEY,
        report_date DATE NOT NULL,
        period_start DATE NOT NULL,
        period_end DATE NOT NULL,
        total_market_value NUMERIC(15,2),
        total_pnl NUMERIC(15,2),
        avg_profit_pct NUMERIC(5,2),
        position_count INTEGER,
        win_count INTEGER,
        lose_count INTEGER,
        flat_count INTEGER,
        t3_accuracy NUMERIC(5,4),
        total_evaluations INTEGER,
        p0_count INTEGER,
        p1_count INTEGER,
        p2_count INTEGER,
        payload JSONB DEFAULT '{}',
        feishu_pushed BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (report_date)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_7d_date ON l3.report_7d_snapshot(report_date);",
    "CREATE INDEX IF NOT EXISTS idx_7d_created ON l3.report_7d_snapshot(created_at);",
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
    LOG.info("✅ l3.report_7d_snapshot 表 + 2 索引已就绪")


# ==================== T1: 持仓总览 (PIT #84) ====================

def get_position_summary() -> PositionSummary:
    """
    T1: 持仓总览
    PIT #84: 总市值必须用 SUM(market_value), 不能 SUM(cost) (V24-C5 教训)
    """
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    # PIT #84: 用 market_value 加密源派生 (V24-C5 修复后)
    cur.execute("""
    SELECT
        COALESCE(SUM(market_value), 0) AS total_mv,
        COALESCE(SUM(market_value * profit_pct / 100), 0) AS total_pnl,
        COALESCE(AVG(profit_pct), 0) AS avg_pp,
        COUNT(*) AS total_cnt,
        COUNT(*) FILTER (WHERE type = 'stock') AS stock_cnt,
        COUNT(*) FILTER (WHERE type IN ('fund', 'etf')) AS fund_cnt,
        COUNT(*) FILTER (WHERE profit_pct > 0) AS win_cnt,
        COUNT(*) FILTER (WHERE profit_pct < 0) AS lose_cnt,
        COUNT(*) FILTER (WHERE profit_pct = 0) AS flat_cnt
    FROM holdings.encrypted_positions
    WHERE is_current = TRUE;
    """)
    r = cur.fetchone()
    cur.close()
    conn.close()
    return PositionSummary(
        total_market_value=float(r[0]),
        total_pnl=float(r[1]),
        avg_profit_pct=float(r[2]),
        win_count=int(r[6]),
        lose_count=int(r[7]),
        flat_count=int(r[8]),
        position_count=int(r[3]),
        stock_count=int(r[4]),
        fund_count=int(r[5]),
    )


# ==================== T2: 持仓变化 (vs 7d 前快照, PIT #85) ====================

def get_latest_snapshot(days_back: int = 7) -> Optional[Dict[str, Any]]:
    """
    T2: 拉 7d 前快照 (PIT #85)
    返回: dict {code: {weight, pp}} 或 None (无快照)
    """
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
    SELECT id, report_date, payload
    FROM l3.report_7d_snapshot
    WHERE report_date < CURRENT_DATE
      AND report_date >= CURRENT_DATE - INTERVAL '{days_back} days'
    ORDER BY report_date DESC LIMIT 1;
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    payload = row["payload"] or {}
    return {
        "snapshot_id": row["id"],
        "snapshot_date": str(row["report_date"]),
        "positions": payload.get("positions", {}),  # {code: {weight, pp}}
    }


def get_position_changes() -> List[PositionChange]:
    """
    T2: 持仓变化 (当前 vs 7d 前)
    PIT #85: 无快照时返回空 list (不报错)
    """
    snap = get_latest_snapshot(days_back=WINDOW_DAYS)
    if not snap:
        LOG.info(f"[V25-G] 无 {WINDOW_DAYS}d 前快照, 返回空变化")
        return []
    snap_positions = snap.get("positions", {})

    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
    SELECT code, name, weight_pct, profit_pct
    FROM holdings.encrypted_positions
    WHERE is_current = TRUE;
    """)
    current = {r["code"]: dict(r) for r in cur.fetchall()}
    cur.close()
    conn.close()

    changes = []
    for code, cur_pos in current.items():
        cur_w = float(cur_pos.get("weight_pct") or 0)
        cur_pp = float(cur_pos.get("profit_pct") or 0)
        prev = snap_positions.get(code, {})
        prev_w = float(prev.get("weight_pct", 0))
        prev_pp = float(prev.get("profit_pct", 0))
        # 权重变化 >0.01% 或 pp 变化 >1% 才记
        if abs(cur_w - prev_w) > 0.01 or abs(cur_pp - prev_pp) > 1.0:
            changes.append(PositionChange(
                code=code,
                name=cur_pos.get("name", ""),
                current_weight=cur_w,
                prev_weight=prev_w,
                weight_delta=cur_w - prev_w,
                current_pp=cur_pp,
                prev_pp=prev_pp,
                pp_delta=cur_pp - prev_pp,
            ))
    # 排序: pp 变化绝对值降序
    changes.sort(key=lambda c: -abs(c.pp_delta))
    return changes


# ==================== T3: 涨跌幅排行 (近 7d) ====================

def get_top_movers(days_back: int = WINDOW_DAYS, top_n: int = 5) -> Tuple[List[TopMover], List[TopMover]]:
    """
    T3: 涨跌幅排行
    PIT #16: ts_code 标准化
    PIT #15: f-string 插入 days_back
    """
    if not isinstance(days_back, int) or not (0 < days_back <= 3650):
        days_back = WINDOW_DAYS
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # 7d 前后价对比
    cur.execute(f"""
    WITH period AS (
        SELECT MAX(trade_date) AS end_date,
               MIN(trade_date) FILTER (WHERE trade_date >= CURRENT_DATE - INTERVAL '{days_back} days') AS start_date
        FROM market.daily_quotes
        WHERE trade_date >= CURRENT_DATE - INTERVAL '{days_back} days'
    )
    SELECT q.ts_code, p.code, p.name,
           (SELECT close_price FROM market.daily_quotes WHERE ts_code = q.ts_code ORDER BY trade_date ASC LIMIT 1) AS start_price,
           (SELECT close_price FROM market.daily_quotes WHERE ts_code = q.ts_code ORDER BY trade_date DESC LIMIT 1) AS end_price
    FROM market.daily_quotes q
    JOIN holdings.encrypted_positions p
      ON (p.code || CASE WHEN p.code LIKE '6%' OR p.code LIKE '5%' OR p.code LIKE '9%' THEN '.XSHG' ELSE '.XSHE' END) = q.ts_code
    WHERE p.is_current = TRUE
      AND q.trade_date >= CURRENT_DATE - INTERVAL '{days_back} days'
    GROUP BY q.ts_code, p.code, p.name;
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    movers = []
    for r in rows:
        start_p = float(r["start_price"] or 0)
        end_p = float(r["end_price"] or 0)
        if start_p <= 0 or end_p <= 0:
            continue
        pct = (end_p - start_p) / start_p * 100
        movers.append(TopMover(
            code=r["code"],
            name=r["name"] or "",
            pct_change=pct,
            current_price=end_p,
            prev_price=start_p,
        ))
    movers.sort(key=lambda m: m.pct_change, reverse=True)
    return movers[:top_n], movers[-top_n:][::-1]  # top gainers, top losers (倒序)


# ==================== T4: 事件回顾 (近 7d) ====================

def get_events(days_back: int = WINDOW_DAYS, top_n: int = 10) -> List[EventRecord]:
    """
    T4: 事件回顾 (news + advice)
    PIT #15: f-string 插入 days_back
    """
    if not isinstance(days_back, int) or not (0 < days_back <= 3650):
        days_back = WINDOW_DAYS
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    events = []
    # news
    cur.execute(f"""
    SELECT id, title, published_at, severity
    FROM research.news_articles
    WHERE published_at > NOW() - INTERVAL '{days_back} days'
      AND severity IN ('HIGH', 'MEDIUM')
    ORDER BY published_at DESC LIMIT {top_n};
    """)
    for r in cur.fetchall():
        events.append(EventRecord(
            event_id=r["id"],
            title=r["title"][:100],
            published_at=str(r["published_at"]),
            severity=r["severity"],
            source="news",
        ))
    # advice
    cur.execute(f"""
    SELECT id, event_topic, created_at
    FROM l3.event_strategist_advice
    WHERE created_at > NOW() - INTERVAL '{days_back} days'
      AND target_codes IS NOT NULL
      AND array_length(target_codes, 1) > 0
    ORDER BY created_at DESC LIMIT {top_n};
    """)
    for r in cur.fetchall():
        events.append(EventRecord(
            event_id=r["id"],
            title=r["event_topic"][:100],
            published_at=str(r["created_at"]),
            severity="ADVICE",
            source="advice",
        ))
    cur.close()
    conn.close()
    # 按时间倒序
    events.sort(key=lambda e: e.published_at, reverse=True)
    return events[:top_n]


# ==================== T5: 准确度评估 (从 l3.event_backtest_log 拉) ====================

def get_accuracy_summary() -> Tuple[float, int]:
    """T5: 拉最新一次 V25-C 报告的 T-3 胜率 + 总评估数"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute("""
    SELECT t3_accuracy, total_evaluations
    FROM l3.event_backtest_log
    ORDER BY report_date DESC LIMIT 1;
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return 0.0, 0
    return float(row[0] or 0), int(row[1] or 0)


# ==================== T6: 风险告警统计 ====================

def get_risk_alert_counts(days_back: int = WINDOW_DAYS) -> Tuple[int, int, int]:
    """T6: 拉近 7d 风险告警 P0/P1/P2 计数"""
    if not isinstance(days_back, int) or not (0 < days_back <= 3650):
        days_back = WINDOW_DAYS
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute(f"""
    SELECT
        COUNT(*) FILTER (WHERE severity = 'P0') AS p0,
        COUNT(*) FILTER (WHERE severity = 'P1') AS p1,
        COUNT(*) FILTER (WHERE severity = 'P2') AS p2
    FROM l3.risk_alert_log
    WHERE created_at > NOW() - INTERVAL '{days_back} days';
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


# ==================== 报告持久化 (PIT #86) ====================

def persist_report(report: SnapshotReport) -> bool:
    """
    T: 写 l3.report_7d_snapshot (PIT #86 idempotent)
    """
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    # 收集完整 payload (持仓快照, PIT #85)
    conn_payload = psycopg2.connect(**DB_PARAMS)
    cur_payload = conn_payload.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur_payload.execute("""
    SELECT code, name, type, weight_pct, profit_pct, market_value
    FROM holdings.encrypted_positions
    WHERE is_current = TRUE;
    """)
    positions_snapshot = {
        r["code"]: {
            "name": r["name"],
            "type": r["type"],
            "weight_pct": float(r["weight_pct"] or 0),
            "profit_pct": float(r["profit_pct"] or 0),
            "market_value": float(r["market_value"] or 0),
        }
        for r in cur_payload.fetchall()
    }
    cur_payload.close()
    conn_payload.close()

    payload = {
        "positions": positions_snapshot,
        "position_changes": [asdict(c) for c in report.position_changes[:20]],
        "top_gainers": [asdict(m) for m in report.top_gainers],
        "top_losers": [asdict(m) for m in report.top_losers],
        "events": [{"id": e.event_id, "title": e.title, "source": e.source, "severity": e.severity, "at": e.published_at} for e in report.events[:20]],
    }

    cur.execute("""
    INSERT INTO l3.report_7d_snapshot
    (report_date, period_start, period_end, total_market_value, total_pnl, avg_profit_pct,
     position_count, win_count, lose_count, flat_count,
     t3_accuracy, total_evaluations, p0_count, p1_count, p2_count,
     payload, feishu_pushed)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (report_date) DO UPDATE SET
        total_market_value = EXCLUDED.total_market_value,
        total_pnl = EXCLUDED.total_pnl,
        avg_profit_pct = EXCLUDED.avg_profit_pct,
        position_count = EXCLUDED.position_count,
        win_count = EXCLUDED.win_count,
        lose_count = EXCLUDED.lose_count,
        flat_count = EXCLUDED.flat_count,
        t3_accuracy = EXCLUDED.t3_accuracy,
        total_evaluations = EXCLUDED.total_evaluations,
        p0_count = EXCLUDED.p0_count,
        p1_count = EXCLUDED.p1_count,
        p2_count = EXCLUDED.p2_count,
        payload = EXCLUDED.payload,
        feishu_pushed = EXCLUDED.feishu_pushed,
        created_at = NOW()
    RETURNING id;
    """, (
        report.report_date, report.period_start, report.period_end,
        report.position_summary.total_market_value,
        report.position_summary.total_pnl,
        report.position_summary.avg_profit_pct,
        report.position_summary.position_count,
        report.position_summary.win_count,
        report.position_summary.lose_count,
        report.position_summary.flat_count,
        report.t3_accuracy, report.total_evaluations,
        report.p0_count, report.p1_count, report.p2_count,
        json.dumps(payload, ensure_ascii=False, default=str),
        report.feishu_pushed,
    ))
    report_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    report.report_id = report_id
    LOG.info(f"✅ 报告持久化: {report.report_date} (id={report_id})")
    return True


# ==================== 飞书推送 (PIT #66 沿用) ====================

def _send_via_feishu_inplace(webhook_url: str, title: str, content: str, level: str = "INFO") -> bool:
    """PIT #66 沿用: 飞书推送就地实现"""
    import urllib.request
    color_map = {"INFO": "#4CAF50", "WARNING": "#FF9800", "ERROR": "#F44336"}
    template = color_map.get(level, "#4CAF50")
    if len(content) > FEISHU_MAX_LEN:
        content = content[:FEISHU_MAX_LEN - 50] + "\n\n... (内容过长, 已截断)"
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": [
                {"tag": "markdown", "content": content},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"InvestPilot · V25-G 7d 报告 · {datetime.now().strftime('%H:%M:%S')}"}]},
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


def push_report_to_feishu(report: SnapshotReport) -> int:
    """推飞书"""
    webhook = get_credential("FEISHU_WEBHOOK", "")
    if not webhook:
        LOG.info("[V25-G] FEISHU_WEBHOOK 未配, 跳过飞书推送 (PIT #69 PG 兜底)")
        return 0

    # 拼接 markdown
    s = report.position_summary
    content = f"""**7d 周报** ({report.period_start} → {report.period_end})

📊 **持仓总览**
- 总市值: **¥{s.total_market_value:,.0f}** ({s.position_count} 持仓)
- 浮动盈亏: **¥{s.total_pnl:+,.0f}** (平均 pp={s.avg_profit_pct:+.2f}%)
- 盈/亏/平: **{s.win_count}/{s.lose_count}/{s.flat_count}** (stock {s.stock_count} + fund {s.fund_count})

📈 **涨跌幅 Top 5**
- 🟢 涨幅: {', '.join(f'{m.name[:8]}({m.code}){m.pct_change:+.1f}%' for m in report.top_gainers) or '无数据'}
- 🔴 跌幅: {', '.join(f'{m.name[:8]}({m.code}){m.pct_change:+.1f}%' for m in report.top_losers) or '无数据'}

📰 **事件回顾** (top 5)
{chr(10).join(f"- [{e.severity or '?'}] {e.title[:50]} ({e.source})" for e in report.events[:5]) or '无事件'}

🎯 **准确度评估** (V25-C)
- T-3 胜率: **{report.t3_accuracy * 100:.1f}%** ({report.total_evaluations} 评估)

⚠️ **风险告警** (近 7d)
- P0: {report.p0_count} | P1: {report.p1_count} | P2: {report.p2_count}

📅 持仓变化: {len(report.position_changes)} 条 (vs 7d 前快照)
"""
    title = f"📅 V25-G 7d 报告 ({report.period_end})"
    level = "WARNING" if report.p0_count > 0 else "INFO"
    ok = _send_via_feishu_inplace(webhook, title, content, level)
    if ok:
        report.feishu_pushed = True
        LOG.info(f"✅ 飞书推送成功: 7d 报告")
    return 1 if ok else 0


# ==================== 主入口 ====================

def generate_7d_report(today: Optional[str] = None) -> SnapshotReport:
    """V25-G 主流程: 6 步生成 7d 报告"""
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    end_date = datetime.strptime(today, "%Y-%m-%d")
    start_date = end_date - timedelta(days=WINDOW_DAYS)
    period_start = start_date.strftime("%Y-%m-%d")
    period_end = today

    ensure_pg_tables()

    # T1: 持仓总览
    pos_summary = get_position_summary()
    LOG.info(f"📊 持仓: ¥{pos_summary.total_market_value:,.0f} | pp={pos_summary.avg_profit_pct:+.2f}% | {pos_summary.position_count} 持仓")

    # T2: 持仓变化
    pos_changes = get_position_changes()
    LOG.info(f"📅 持仓变化: {len(pos_changes)} 条 (vs 7d 前)")

    # T3: 涨跌幅排行
    gainers, losers = get_top_movers(days_back=WINDOW_DAYS, top_n=5)
    LOG.info(f"📈 涨/跌: {len(gainers)} / {len(losers)}")

    # T4: 事件回顾
    events = get_events(days_back=WINDOW_DAYS, top_n=10)
    LOG.info(f"📰 事件: {len(events)} 条")

    # T5: 准确度评估
    t3_acc, total_evals = get_accuracy_summary()
    LOG.info(f"🎯 T-3 胜率: {t3_acc * 100:.1f}% ({total_evals} 评估)")

    # T6: 风险告警
    p0, p1, p2 = get_risk_alert_counts(days_back=WINDOW_DAYS)
    LOG.info(f"⚠️ 风险告警: P0={p0} P1={p1} P2={p2}")

    # 拼装报告
    report = SnapshotReport(
        report_id=0,
        report_date=today,
        period_start=period_start,
        period_end=period_end,
        position_summary=pos_summary,
        position_changes=pos_changes,
        top_gainers=gainers,
        top_losers=losers,
        events=events,
        t3_accuracy=t3_acc,
        total_evaluations=total_evals,
        p0_count=p0,
        p1_count=p1,
        p2_count=p2,
        feishu_pushed=False,
    )

    # 持久化
    persist_report(report)
    # 飞书推送
    push_report_to_feishu(report)

    # 控制台输出
    print(f"\n=== V25-G 7d 报告 ({period_start} → {period_end}) ===")
    print(f"📊 持仓: ¥{pos_summary.total_market_value:,.0f} | 浮盈 ¥{pos_summary.total_pnl:+,.0f} | pp={pos_summary.avg_profit_pct:+.2f}%")
    print(f"   盈/亏/平: {pos_summary.win_count}/{pos_summary.lose_count}/{pos_summary.flat_count}")
    print(f"\n📈 涨幅 Top 5:")
    for m in gainers:
        print(f"   🟢 {m.name[:14]:14s} {m.code} {m.pct_change:+.2f}%")
    print(f"\n📉 跌幅 Top 5:")
    for m in losers:
        print(f"   🔴 {m.name[:14]:14s} {m.code} {m.pct_change:+.2f}%")
    print(f"\n📰 事件: {len(events)} 条")
    for e in events[:5]:
        print(f"   [{e.severity or '?'}] {e.title[:60]} ({e.source})")
    print(f"\n🎯 T-3 胜率: {t3_acc * 100:.1f}% ({total_evals} 评估)")
    print(f"⚠️ 风险: P0={p0} P1={p1} P2={p2}")
    print(f"📅 持仓变化: {len(pos_changes)} 条")
    if pos_changes:
        print(f"\n--- 变化 top 5 ---")
        for c in pos_changes[:5]:
            print(f"   {c.name[:14]:14s} {c.code} 权重 {c.prev_weight:.2f}→{c.current_weight:.2f}% pp {c.prev_pp:+.2f}→{c.current_pp:+.2f}%")

    return report


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--today", type=str, default=None)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        print("=== V25-G self-test (实战 6/14 数据生成 7d 报告) ===")
        # 实战 6/14 self-test
        generate_7d_report(today=args.today or "2026-06-14")
    else:
        generate_7d_report(today=args.today)
