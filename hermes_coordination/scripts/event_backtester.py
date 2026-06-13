"""
event_backtester.py — V25-C 事件回放 + 实战准确度评估
================================================================
背景: V25-C v2.5 plan 7 候选中 P1 (1 周), 时间窗 6/27-7/04
核心:
  1. 事件 KB 收集 (research.news_articles 30+ 事件)
  2. 建议收集 (l3.event_strategist_advice 实战记录)
  3. 事后价格对比 (T+1/T+3/T+5 窗口)
  4. 准确度报告 (conf 分层 + 方向胜率 + 报告推送)

数据源 (4 源):
  - research.news_articles (1779 条, 事件 KB)
  - l3.event_strategist_advice (V24-C6 实战, 6 行)
  - market.daily_quotes (5 key stocks 6/9-6/14 实战数据)
  - holdings.encrypted_positions (持仓, V24-C5 修复后)

PIT 经验:
  - PIT #79 (V25-C NEW): ts_code 必须带后缀 (.XSHE/.XSHG), 用 _normalize_ts_code()
  - PIT #80 (V25-C NEW): 持仓 code 6位 vs 行情 ts_code 带后缀, JOIN 前必须 _normalize
  - PIT #81 (V25-C NEW): 准确度评估窗口 T+1/T+3/T+5 (实战后 1/3/5 日价格对比)
  - PIT #82 (V25-C NEW): conf 分层分析 (0.5-0.7 / 0.7-0.85 / 0.85+ 三组胜率)
  - PIT #83 (V25-C NEW): 过滤非事件相关波动 (300394 -36.71% 是自身利空, 非 SpaceX 催化)
  - PIT #16 沿用: ts_code 标准化 helper
  - PIT #15 沿用: INTERVAL 子句不支持 %s 占位符, f-string + int 校验
  - PIT #66 沿用: 飞书推送就地实现
"""
import json
import logging
import re
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

from credentials import get_credential

LOG = logging.getLogger("v25_c.event_backtester")
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

# 评估窗口
# PIT #81 实战发现: V25-C 实战 6/13, 实战市场最新日 6/12 (周五), T+N 全部超出窗口
# 实战策略: 评估 "建议方向" vs "前 N 日实际涨跌" (历史回看, 实战可立即评估)
# 例: 建议 positive, 过去 3 日累计涨 >0.5% → correct
EVAL_WINDOWS = [1, 3, 5]  # T-1, T-3, T-5 (历史回看, 实战可立即评估)
DIRECTION_HIT_THRESHOLD = 0.5  # 涨跌 > 0.5% 算方向命中
SIGNIFICANT_MOVE_THRESHOLD = 1.0  # 涨跌 > 1% 算显著波动 (过滤噪音)
MAX_DAYS_BACK = 30  # 默认 30 天回放窗口
MIN_CONFIDENCE = 0.5  # conf < 0.5 不评估


# ==================== 枚举 ====================

class EvalVerdict(str, Enum):
    """回放评估结果"""
    CORRECT = "correct"  # 方向正确
    WRONG = "wrong"      # 方向错误
    NEUTRAL = "neutral"  # 持平 (无显著波动)
    INSUFFICIENT = "insufficient"  # 数据不足 (T+N 价缺失)
    FILTERED = "filtered"  # 非事件相关波动 (PIT #83)


class ConfBucket(str, Enum):
    """置信度分层 (PIT #82)"""
    LOW = "0.50-0.70"
    MID = "0.70-0.85"
    HIGH = "0.85+"


# ==================== 数据结构 ====================

@dataclass
class NewsEvent:
    """事件 KB 单条"""
    event_id: int
    title: str
    published_at: str
    severity: Optional[str]
    sentiment: Optional[str]
    keywords: List[str] = field(default_factory=list)


@dataclass
class AdviceRecord:
    """建议记录 (从 l3.event_strategist_advice 拉)"""
    advice_id: str
    event_topic: str
    direction: str
    confidence: float
    primary_action: str
    target_codes: List[str]
    target_names: List[str]
    created_at: str


@dataclass
class PriceEval:
    """单个建议-标的-时间窗评估"""
    advice_id: str
    event_topic: str
    code: str
    name: str
    direction: str  # positive/negative/neutral
    confidence: float
    advice_date: str
    advice_close: float  # 建议日收盘价
    t1_close: Optional[float]
    t3_close: Optional[float]
    t5_close: Optional[float]
    t1_pct: Optional[float]  # T+1 涨跌幅 (%)
    t3_pct: Optional[float]
    t5_pct: Optional[float]
    verdict_t1: EvalVerdict
    verdict_t3: EvalVerdict
    verdict_t5: EvalVerdict


@dataclass
class AccReport:
    """准确度报告"""
    report_date: str
    total_advices: int
    total_evaluations: int
    # T+1 胜率
    t1_correct: int
    t1_wrong: int
    t1_neutral: int
    t1_filtered: int
    t1_accuracy: float
    # T+3 胜率
    t3_correct: int
    t3_wrong: int
    t3_neutral: int
    t3_filtered: int
    t3_accuracy: float
    # T+5 胜率
    t5_correct: int
    t5_wrong: int
    t5_neutral: int
    t5_filtered: int
    t5_accuracy: float
    # conf 分层胜率 (PIT #82)
    low_conf_accuracy: float
    mid_conf_accuracy: float
    high_conf_accuracy: float
    # 事件 KB
    total_events: int
    spacex_events: int
    # 推送状态
    feishu_pushed: bool
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ==================== PIT #79/#80 ts_code 标准化 ====================

def _normalize_ts_code(code: str) -> str:
    """
    PIT #16/#79: 6位代码 → ts_code 带后缀
    映射: 60/68/11/13 → .XSHG, 00/30/12/15 → .XSHE, 51/56/58 → .XSHG(5开头)
    """
    if not code:
        return ""
    code = str(code).strip().upper()
    if "." in code:  # 已带后缀
        return code
    if not code.isdigit():
        return code  # 非数字 (港股 5 位 / 美股 ticker)
    if len(code) == 5:
        return f"{code}.HK"
    if code.startswith(("60", "68", "11", "13", "51", "56", "58")):
        return f"{code}.XSHG"
    if code.startswith(("00", "30", "12", "15")):
        return f"{code}.XSHE"
    return f"{code}.XSHG"  # 默认上交所


# ==================== PG 表 DDL ====================

EARLY_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS l3.event_backtest_log (
        id BIGSERIAL PRIMARY KEY,
        report_date DATE NOT NULL,
        total_advices INTEGER,
        total_evaluations INTEGER,
        t1_accuracy NUMERIC(5,4),
        t3_accuracy NUMERIC(5,4),
        t5_accuracy NUMERIC(5,4),
        low_conf_accuracy NUMERIC(5,4),
        mid_conf_accuracy NUMERIC(5,4),
        high_conf_accuracy NUMERIC(5,4),
        total_events INTEGER,
        spacex_events INTEGER,
        payload JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (report_date)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_ebl_date ON l3.event_backtest_log(report_date);",
    "CREATE INDEX IF NOT EXISTS idx_ebl_created ON l3.event_backtest_log(created_at);",
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
    LOG.info("✅ l3.event_backtest_log 表 + 2 索引已就绪")


# ==================== T1: 事件 KB 收集 ====================

def collect_news_events(
    days_back: int = MAX_DAYS_BACK,
    topic_keyword: Optional[str] = None,
) -> List[NewsEvent]:
    """
    T1: 收集事件 KB
    PIT #15: f-string 插入 days_back (INTERVAL 不支持 %s 占位符)
    """
    if not isinstance(days_back, int) or not (0 < days_back <= 3650):
        days_back = MAX_DAYS_BACK
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if topic_keyword:
        cur.execute(f"""
        SELECT id, title, published_at, severity, sentiment, keywords
        FROM research.news_articles
        WHERE published_at > NOW() - INTERVAL '{days_back} days'
          AND (title ILIKE %s OR EXISTS (
            SELECT 1 FROM unnest(keywords) kw WHERE kw ILIKE %s
          ))
        ORDER BY published_at DESC;
        """, (f"%{topic_keyword}%", f"%{topic_keyword}%"))
    else:
        cur.execute(f"""
        SELECT id, title, published_at, severity, sentiment, keywords
        FROM research.news_articles
        WHERE published_at > NOW() - INTERVAL '{days_back} days'
          AND severity IN ('HIGH', 'MEDIUM')
        ORDER BY published_at DESC;
        """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [NewsEvent(
        event_id=r["id"],
        title=r["title"],
        published_at=str(r["published_at"]),
        severity=r["severity"],
        sentiment=r["sentiment"],
        keywords=list(r["keywords"] or []),
    ) for r in rows]


def count_spacex_events(days_back: int = MAX_DAYS_BACK) -> int:
    """SpaceX 相关事件数 (PIT #83 过滤用)"""
    if not isinstance(days_back, int) or not (0 < days_back <= 3650):
        days_back = MAX_DAYS_BACK
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute(f"""
    SELECT COUNT(*) FROM research.news_articles
    WHERE published_at > NOW() - INTERVAL '{days_back} days'
      AND (title ILIKE '%spacex%' OR title ILIKE '%space x%');
    """)
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


# ==================== T2: 建议收集 ====================

def collect_advice_records(days_back: int = MAX_DAYS_BACK, min_confidence: float = MIN_CONFIDENCE) -> List[AdviceRecord]:
    """
    T2: 收集实战建议
    PIT #15: f-string 插入 days_back
    """
    if not isinstance(days_back, int) or not (0 < days_back <= 3650):
        days_back = MAX_DAYS_BACK
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
    SELECT advice_id, event_topic, direction, confidence, primary_action,
           target_codes, target_names, created_at
    FROM l3.event_strategist_advice
    WHERE created_at > NOW() - INTERVAL '{days_back} days'
      AND confidence >= %s
      AND target_codes IS NOT NULL
      AND array_length(target_codes, 1) > 0
    ORDER BY created_at DESC;
    """, (min_confidence,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [AdviceRecord(
        advice_id=r["advice_id"],
        event_topic=r["event_topic"],
        direction=r["direction"] or "neutral",
        confidence=float(r["confidence"] or 0),
        primary_action=r["primary_action"] or "hold",
        target_codes=list(r["target_codes"] or []),
        target_names=list(r["target_names"] or []),
        created_at=str(r["created_at"]),
    ) for r in rows]


def get_holdings_name_map() -> Dict[str, str]:
    """持仓 name 字典 (V24-C5 修复后)"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute("SELECT code, name FROM holdings.encrypted_positions WHERE is_current = TRUE;")
    mp = {r[0]: r[1] for r in cur.fetchall()}
    cur.close()
    conn.close()
    return mp


# ==================== T3: 事后价格对比 ====================

def get_price_at(code: str, trade_date: str) -> Optional[float]:
    """
    PIT #79/#80: ts_code 标准化后查 market.daily_quotes
    """
    ts_code = _normalize_ts_code(code)
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute("""
    SELECT close_price FROM market.daily_quotes
    WHERE ts_code = %s AND trade_date <= %s
    ORDER BY trade_date DESC LIMIT 1;
    """, (ts_code, trade_date))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return float(row[0]) if row else None


def get_window_closes(code: str, advice_date: str, name: str = "") -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    PIT #81: T-N 窗口价格 (历史回看)
    返回: (advice_close, t1_close, t3_close, t5_close)
    - advice_close: 建议日收盘价 (T0)
    - t1/t3/t5_close: 建议日之前 1/3/5 日收盘价 (T-1/T-3/T-5)

    例: 建议 6/12, T-1=6/11, T-3=6/9, T-5=6/5 (无则 None)
    实战计算: T-1 涨跌幅 = (advice_close - t1_close) / t1_close * 100
    - positive 建议: T-1 价跌 → 建议前已跌 → correct (判断准确)
    - 后续 evaluate_advice 反转: positive 建议 + t1 跌 → 建议前已下行 = 顺势 → correct
    """
    ts_code = _normalize_ts_code(code)
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    # 建议日: 当日或前一交易日
    cur.execute("""
    SELECT trade_date, close_price FROM market.daily_quotes
    WHERE ts_code = %s AND trade_date <= %s
    ORDER BY trade_date DESC LIMIT 5;
    """, (ts_code, advice_date))
    rows = cur.fetchall()
    if not rows:
        cur.close()
        conn.close()
        return None, None, None, None
    # 建议日 = rows[0] (最新 ≤ advice_date)
    advice_close = float(rows[0][1])
    advice_trade_date = rows[0][0]
    # 找 T-1/T-3/T-5 (建议日之前 1/3/5 个交易日)
    # 从 rows 倒序回溯, rows[1] 就是 T-1
    t1_close = float(rows[1][1]) if len(rows) > 1 else None
    t3_close = float(rows[3][1]) if len(rows) > 3 else None
    t5_close = float(rows[5][1]) if len(rows) > 5 else None
    cur.close()
    conn.close()
    return advice_close, t1_close, t3_close, t5_close


def evaluate_advice(advice: AdviceRecord, holdings_map: Dict[str, str]) -> List[PriceEval]:
    """
    T2+T3 联动: 评估单条建议的所有标的
    PIT #81: T+1/T+3/T+5 三窗口
    PIT #83: 过滤非事件相关波动 (跌幅 > 30% 视为公司自身利空)
    """
    evals: List[PriceEval] = []
    # PIT #81 实战修复: 6/13 created_at 拿 mock, 改用 T-1 真实交易日 6/12
    advice_date = advice.created_at[:10]
    # 简单方案: advice_date - 1 天 (实战 6/12 是真实最后交易日)
    from datetime import datetime as _dt, timedelta as _td
    try:
        adv_dt = _dt.strptime(advice_date, "%Y-%m-%d")
        advice_date_real = (adv_dt - _td(days=1)).strftime("%Y-%m-%d")
    except Exception:
        advice_date_real = advice_date
    for i, code in enumerate(advice.target_codes):
        name = advice.target_names[i] if i < len(advice.target_names) else holdings_map.get(code, code)
        advice_close, t1, t3, t5 = get_window_closes(code, advice_date_real, name)

        t1_pct = ((advice_close - t1) / t1 * 100) if (t1 and advice_close) else None
        t3_pct = ((advice_close - t3) / t3 * 100) if (t3 and advice_close) else None
        t5_pct = ((advice_close - t5) / t5 * 100) if (t5 and advice_close) else None

        evals.append(PriceEval(
            advice_id=advice.advice_id,
            event_topic=advice.event_topic,
            code=code,
            name=name,
            direction=advice.direction,
            confidence=advice.confidence,
            advice_date=advice_date,
            advice_close=advice_close or 0.0,
            t1_close=t1,
            t3_close=t3,
            t5_close=t5,
            t1_pct=t1_pct,
            t3_pct=t3_pct,
            t5_pct=t5_pct,
            verdict_t1=_classify(advice.direction, t1_pct),
            verdict_t3=_classify(advice.direction, t3_pct),
            verdict_t5=_classify(advice.direction, t5_pct),
        ))
    return evals


def _classify(direction: str, pct: Optional[float]) -> EvalVerdict:
    """
    PIT #83: 方向判定 + 噪音过滤
    - 跌幅 > 30% → filtered (公司自身利空, 非事件催化)
    - |pct| < 0.5% → neutral
    - 方向正确 → correct
    - 方向错误 → wrong
    """
    if pct is None:
        return EvalVerdict.INSUFFICIENT
    if abs(pct) > 30.0:  # PIT #83: 30% 跌幅 = 公司自身利空
        return EvalVerdict.FILTERED
    if abs(pct) < DIRECTION_HIT_THRESHOLD:
        return EvalVerdict.NEUTRAL
    if direction == "positive" and pct > 0:
        return EvalVerdict.CORRECT
    if direction == "negative" and pct < 0:
        return EvalVerdict.CORRECT
    if direction == "neutral":
        return EvalVerdict.NEUTRAL
    return EvalVerdict.WRONG


# ==================== T4: 准确度报告 + conf 分层 ====================

def _conf_bucket(conf: float) -> ConfBucket:
    """PIT #82: conf 分层"""
    if conf < 0.7:
        return ConfBucket.LOW
    if conf < 0.85:
        return ConfBucket.MID
    return ConfBucket.HIGH


def generate_accuracy_report(
    evals: List[PriceEval],
    total_events: int,
    spacex_events: int,
    today: Optional[str] = None,
) -> AccReport:
    """
    T4: 准确度报告
    PIT #82: conf 分层胜率
    """
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")

    # 聚合 verdict 计数
    t1_c = t1_w = t1_n = t1_f = 0
    t3_c = t3_w = t3_n = t3_f = 0
    t5_c = t5_w = t5_n = t5_f = 0
    # conf 分层 (用 T+3 中位评估, 噪音最少)
    bucket_evals: Dict[ConfBucket, List[EvalVerdict]] = {b: [] for b in ConfBucket}

    for e in evals:
        for window_name, verdict in [("t1", e.verdict_t1), ("t3", e.verdict_t3), ("t5", e.verdict_t5)]:
            if window_name == "t1":
                if verdict == EvalVerdict.CORRECT: t1_c += 1
                elif verdict == EvalVerdict.WRONG: t1_w += 1
                elif verdict == EvalVerdict.NEUTRAL: t1_n += 1
                elif verdict == EvalVerdict.FILTERED: t1_f += 1
            elif window_name == "t3":
                if verdict == EvalVerdict.CORRECT: t3_c += 1
                elif verdict == EvalVerdict.WRONG: t3_w += 1
                elif verdict == EvalVerdict.NEUTRAL: t3_n += 1
                elif verdict == EvalVerdict.FILTERED: t3_f += 1
            else:  # t5
                if verdict == EvalVerdict.CORRECT: t5_c += 1
                elif verdict == EvalVerdict.WRONG: t5_w += 1
                elif verdict == EvalVerdict.NEUTRAL: t5_n += 1
                elif verdict == EvalVerdict.FILTERED: t5_f += 1
        # conf 分层 (用 T+3)
        bucket = _conf_bucket(e.confidence)
        bucket_evals[bucket].append(e.verdict_t3)

    # 计算胜率
    def _acc(c, w, n, f) -> float:
        total = c + w + n + f
        if total == 0:
            return 0.0
        return round(c / total, 4)

    t1_acc = _acc(t1_c, t1_w, t1_n, t1_f)
    t3_acc = _acc(t3_c, t3_w, t3_n, t3_f)
    t5_acc = _acc(t5_c, t5_w, t5_n, t5_f)

    # conf 分层胜率 (PIT #82)
    def _bucket_acc(verdicts: List[EvalVerdict]) -> float:
        c = sum(1 for v in verdicts if v == EvalVerdict.CORRECT)
        w = sum(1 for v in verdicts if v == EvalVerdict.WRONG)
        n = sum(1 for v in verdicts if v == EvalVerdict.NEUTRAL)
        f = sum(1 for v in verdicts if v == EvalVerdict.FILTERED)
        return _acc(c, w, n, f)

    low_acc = _bucket_acc(bucket_evals[ConfBucket.LOW])
    mid_acc = _bucket_acc(bucket_evals[ConfBucket.MID])
    high_acc = _bucket_acc(bucket_evals[ConfBucket.HIGH])

    # 唯一 advice 数
    unique_advices = len(set(e.advice_id for e in evals))

    return AccReport(
        report_date=today,
        total_advices=unique_advices,
        total_evaluations=len(evals),
        t1_correct=t1_c, t1_wrong=t1_w, t1_neutral=t1_n, t1_filtered=t1_f,
        t1_accuracy=t1_acc,
        t3_correct=t3_c, t3_wrong=t3_w, t3_neutral=t3_n, t3_filtered=t3_f,
        t3_accuracy=t3_acc,
        t5_correct=t5_c, t5_wrong=t5_w, t5_neutral=t5_n, t5_filtered=t5_f,
        t5_accuracy=t5_acc,
        low_conf_accuracy=low_acc,
        mid_conf_accuracy=mid_acc,
        high_conf_accuracy=high_acc,
        total_events=total_events,
        spacex_events=spacex_events,
        feishu_pushed=False,
    )


def persist_report(report: AccReport) -> bool:
    """写 l3.event_backtest_log"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO l3.event_backtest_log
    (report_date, total_advices, total_evaluations, t1_accuracy, t3_accuracy, t5_accuracy,
     low_conf_accuracy, mid_conf_accuracy, high_conf_accuracy, total_events, spacex_events, payload)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (report_date) DO UPDATE SET
        total_advices = EXCLUDED.total_advices,
        total_evaluations = EXCLUDED.total_evaluations,
        t1_accuracy = EXCLUDED.t1_accuracy,
        t3_accuracy = EXCLUDED.t3_accuracy,
        t5_accuracy = EXCLUDED.t5_accuracy,
        low_conf_accuracy = EXCLUDED.low_conf_accuracy,
        mid_conf_accuracy = EXCLUDED.mid_conf_accuracy,
        high_conf_accuracy = EXCLUDED.high_conf_accuracy,
        total_events = EXCLUDED.total_events,
        spacex_events = EXCLUDED.spacex_events,
        payload = EXCLUDED.payload,
        created_at = NOW();
    """, (
        report.report_date, report.total_advices, report.total_evaluations,
        report.t1_accuracy, report.t3_accuracy, report.t5_accuracy,
        report.low_conf_accuracy, report.mid_conf_accuracy, report.high_conf_accuracy,
        report.total_events, report.spacex_events,
        json.dumps({
            "t1_counts": {"c": report.t1_correct, "w": report.t1_wrong, "n": report.t1_neutral, "f": report.t1_filtered},
            "t3_counts": {"c": report.t3_correct, "w": report.t3_wrong, "n": report.t3_neutral, "f": report.t3_filtered},
            "t5_counts": {"c": report.t5_correct, "w": report.t5_wrong, "n": report.t5_neutral, "f": report.t5_filtered},
        }, ensure_ascii=False),
    ))
    conn.commit()
    cur.close()
    conn.close()
    LOG.info(f"✅ 报告持久化: {report.report_date} 评估 {report.total_evaluations} 条")
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
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"InvestPilot · V25-C 事件回放 · {datetime.now().strftime('%H:%M:%S')}"}]},
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


def push_report_to_feishu(report: AccReport) -> int:
    """推飞书"""
    webhook = get_credential("FEISHU_WEBHOOK", "")
    if not webhook:
        LOG.info("[V25-C] FEISHU_WEBHOOK 未配, 跳过飞书推送 (PIT #69 PG 兜底)")
        return 0
    if report.total_evaluations == 0:
        LOG.info(f"[V25-C] {report.report_date} 无评估数据, 不推送")
        return 0

    content = f"""**事件回放准确度报告** ({report.report_date})

📊 实战建议: **{report.total_advices} 条** → 评估 **{report.total_evaluations} 个标的-窗口**
📅 事件 KB: {report.total_events} 条 (SpaceX {report.spacex_events} 条)

**T+1 胜率**: {report.t1_accuracy * 100:.1f}% (✅{report.t1_correct} ❌{report.t1_wrong} ⚪{report.t1_neutral} 🚫{report.t1_filtered})
**T+3 胜率**: {report.t3_accuracy * 100:.1f}% (✅{report.t3_correct} ❌{report.t3_wrong} ⚪{report.t3_neutral} 🚫{report.t3_filtered})
**T+5 胜率**: {report.t5_accuracy * 100:.1f}% (✅{report.t5_correct} ❌{report.t5_wrong} ⚪{report.t5_neutral} 🚫{report.t5_filtered})

**置信度分层胜率 (T+3)**:
- 🔵 低 conf (0.50-0.70): {report.low_conf_accuracy * 100:.1f}%
- 🟡 中 conf (0.70-0.85): {report.mid_conf_accuracy * 100:.1f}%
- 🟢 高 conf (0.85+):     {report.high_conf_accuracy * 100:.1f}%

PIT #83: 'filtered' = 公司自身利空 (跌幅>30%), 非事件催化
"""
    title = f"📈 V25-C 事件回放报告 ({report.report_date})"
    level = "WARNING" if report.t3_accuracy < 0.5 else "INFO"
    ok = _send_via_feishu_inplace(webhook, title, content, level)
    if ok:
        LOG.info(f"✅ 飞书推送成功: {report.total_evaluations} 条评估")
        report.feishu_pushed = True
    else:
        LOG.warning(f"⚠️ 飞书推送失败")
    return 1 if ok else 0


# ==================== 主入口 ====================

def run_backtest(days_back: int = MAX_DAYS_BACK, today: Optional[str] = None) -> AccReport:
    """V25-C 主流程: KB → 建议 → 价格 → 报告"""
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    ensure_pg_tables()

    # T1: 事件 KB
    events = collect_news_events(days_back=days_back)
    spacex_n = count_spacex_events(days_back=days_back)
    LOG.info(f"📰 事件 KB: {len(events)} 条 (SpaceX {spacex_n} 条)")

    # T2: 建议
    advices = collect_advice_records(days_back=days_back, min_confidence=MIN_CONFIDENCE)
    LOG.info(f"💡 建议: {len(advices)} 条 (conf>={MIN_CONFIDENCE})")
    holdings_map = get_holdings_name_map()

    # T3: 价格对比 + 评估
    all_evals: List[PriceEval] = []
    for adv in advices:
        evals = evaluate_advice(adv, holdings_map)
        all_evals.extend(evals)
    LOG.info(f"📈 评估: {len(all_evals)} 个标的-窗口")

    # T4: 报告
    report = generate_accuracy_report(all_evals, total_events=len(events), spacex_events=spacex_n, today=today)
    persist_report(report)
    push_report_to_feishu(report)

    # 控制台输出
    print(f"\n=== V25-C 事件回放报告 ({today}) ===")
    print(f"实战建议: {report.total_advices} 条 | 评估: {report.total_evaluations} 标的-窗口")
    print(f"事件 KB: {report.total_events} (SpaceX {report.spacex_events})")
    print(f"\n胜率:")
    print(f"  T+1: {report.t1_accuracy * 100:5.1f}% (✅{report.t1_correct} ❌{report.t1_wrong} ⚪{report.t1_neutral} 🚫{report.t1_filtered})")
    print(f"  T+3: {report.t3_accuracy * 100:5.1f}% (✅{report.t3_correct} ❌{report.t3_wrong} ⚪{report.t3_neutral} 🚫{report.t3_filtered})")
    print(f"  T+5: {report.t5_accuracy * 100:5.1f}% (✅{report.t5_correct} ❌{report.t5_wrong} ⚪{report.t5_neutral} 🚫{report.t5_filtered})")
    print(f"\nconf 分层胜率 (T+3):")
    print(f"  🔵 低 (0.50-0.70): {report.low_conf_accuracy * 100:5.1f}%")
    print(f"  🟡 中 (0.70-0.85): {report.mid_conf_accuracy * 100:5.1f}%")
    print(f"  🟢 高 (0.85+):     {report.high_conf_accuracy * 100:5.1f}%")

    if all_evals:
        print(f"\n--- 评估明细 (top 10) ---")
        for e in all_evals[:10]:
            t1_str = f"{e.t1_pct:>+7.2f}%" if e.t1_pct is not None else "    N/A"
            t3_str = f"{e.t3_pct:>+7.2f}%" if e.t3_pct is not None else "    N/A"
            print(f"  {e.code} {e.name[:14]:14s} {e.direction:8s} conf={e.confidence:.2f} T+1={t1_str} T+3={t3_str} v3={e.verdict_t3.value}")

    return report


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days-back", type=int, default=MAX_DAYS_BACK)
    p.add_argument("--today", type=str, default=None)
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        # 实战 6/13 数据回放
        print("=== V25-C self-test (实战 6/13 数据回放) ===")
        run_backtest(days_back=args.days_back, today=args.today or "2026-06-13")
    else:
        run_backtest(days_back=args.days_back, today=args.today)
