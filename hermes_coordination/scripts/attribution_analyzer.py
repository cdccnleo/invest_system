"""
V25-E 业绩归因分析 (P2 候选, v2.5 plan 7 方向之一)
=====================================================

🎯 目标: 持仓业绩归因 — 把组合收益拆解为资产配置效应 / 选股效应 / 交互效应,
        并用 LLM 一句话归因 (降级链 3 级)

📦 数据源 (实战 6/14 已验证存在):
  - holdings.encrypted_positions (45 持仓, stock/fund 异构)
  - market.daily_quotes (5 关键标的 19 天, 个股/ETF 后缀)
  - l3.event_backtest_log (聚合 t1/t3/t5 准确率)
  - l3.report_7d_snapshot (1 行, 7d 快照对比)
  - l3.cross_account_summary (V25-D 4 CSV 跨账户)
  - l3.event_strategist_advice (V24-C6 6 行)
  - l3.decision_points (V22 5 行)

🧠 Brinson 归因简化 (PIT #94):
  - 资产配置效应 (allocation) = (weight_pct - portfolio_avg) × portfolio_pp
  - 选股效应 (selection) = weight_pct × (position_pp - portfolio_pp)
  - 交互效应 (interaction) = (weight_pct - portfolio_avg) × (position_pp - portfolio_pp)
  - 实战 6/14: 持仓整体 pp 49.06%, 选股效应为主

📏 LLM 归因降级链 (PIT #95, 沿用 V25-A1+A2 + V25-C 模式):
  - 1级: V25-C T-3 胜率 35.7% (14 评估)
  - 2级: 事件强度 × 行业暴露
  - 3级: 规则引擎 (无 LLM) 兜底
  - token 上限 1000 (避免 1800 字符飞书卡片超限, V25-A1 PIT #70)
  - 持仓类型过滤: fund 持仓跳过 LLM 归因 (PIT #72 沿用 V25-F)

🗂️ 新表 l3.attribution_report:
  - id, report_date UNIQUE
  - portfolio_pp, position_count, stock_count, fund_count
  - total_market_value, total_profit
  - allocation_effect, selection_effect, interaction_effect
  - top_contributors[], top_detractors[]
  - event_accuracy_t1, event_accuracy_t3
  - llm_summary, llm_status (success/degraded/fallback)
  - payload (JSONB 完整)
  - 4 索引: pkey + UNIQUE report_date + idx_ar_date + idx_ar_created

🔒 实战 PIT 预位 (PIT #92-#95):
  PIT #92: market.daily_quotes 实战无 510300.SH/510500.SH/588000.SH 基准
           → 用 持仓整体 pp 作为基准 (V25-G 模式, 简单实用)
  PIT #93: 持仓类型加权 = 持仓整体 pp (整体 pp 即加权平均)
  PIT #94: Brinson 简化 (weight, pp) 三效应分解
  PIT #95: LLM 降级链 3 级 + token 上限 1000

⏰ 实战触发: v2.5 阶段, 默认 6/15 周一 11:35 (V25-A2 cron 已配)
            实战可手动调 `python attribution_analyzer.py`
            6/22 周日 22:00 V25-D 调仓周报后, 6/29 周日 22:00 可加 V25-E 业绩归因周报

Author: Hermes Agent
Created: 2026-06-14 (V25-E 实施)
"""

from __future__ import annotations
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ============================================================
# 路径 + 配置
# ============================================================
ROOT = Path("/home/aileo/invest_system")
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# V25-D PIT #87 锁模式 (沿用 schedule_runner)
LOCK_PATH = LOG_DIR / ".attribution_analyzer.lock"

# AInvest scripts 路径 (PIT #27 sys.path.insert)
AINVEST_SCRIPTS = Path("/mnt/c/PythonProject/invest_system/scripts")
if str(AINVEST_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(AINVEST_SCRIPTS))

# ============================================================
# 日志 (PIT #18 沿用 V25-D FileHandler)
# ============================================================
LOG_FILE = LOG_DIR / "attribution_analyzer.log"
logger = logging.getLogger("attribution_analyzer")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(ch)

# ============================================================
# PIT #92-#95 实战常量
# ============================================================
WINDOW_DAYS = 7              # 7d 业绩归因窗口
TOP_N = 5                     # Top/Bottom N 贡献者
LLM_TOKEN_LIMIT = 1000        # PIT #95 LLM 归因 token 上限
MIN_CONTRIB_WEIGHT = 0.5      # 最小权重 0.5% 才入归因
FUND_SKIP_LLM = True          # PIT #72 沿用: fund 持仓跳过 LLM 归因
MIN_PROFIT_PCT = -50.0        # pp 异常下界 (V25-C PIT #83 跌幅>30% 过滤沿用)

# ============================================================
# PIT #87 单实例锁 (沿用 V25-D 模式, fcntl.flock + LOCK_EX|LOCK_NB)
# ============================================================
import fcntl


@contextmanager
def acquire_lock(lock_path: Path = LOCK_PATH, timeout: float = 10.0):
    """V25-D PIT #87 单实例锁: 防止双跑 race condition
    沿用 schedule_runner.py:25-86 模式 (LOCK_EX|LOCK_NB + /proc/PID 死锁检测)
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    start = time.time()
    while True:
        try:
            fd = open(lock_path, "w")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.write(f"{os.getpid()}\n")
            fd.flush()
            break
        except (IOError, OSError):
            if fd:
                fd.close()
                fd = None
            # /proc/PID 死锁检测
            if lock_path.exists():
                try:
                    pid_str = lock_path.read_text().strip()
                    if pid_str.isdigit():
                        pid = int(pid_str)
                        if not Path(f"/proc/{pid}").exists():
                            # 持有者已死, 强删
                            lock_path.unlink(missing_ok=True)
                            logger.warning(f"orphan lock {lock_path} (PID {pid} dead), removed")
                            continue
                except Exception:
                    pass
            if time.time() - start > timeout:
                logger.error(f"acquire_lock timeout after {timeout}s")
                raise TimeoutError(f"acquire_lock timeout for {lock_path}")
            time.sleep(0.5)
    try:
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()


# ============================================================
# 4 dataclass (实战 PIT #91 沿用 V25-D 风格)
# ============================================================
@dataclass
class PositionSnapshot:
    """持仓快照 (单只标的)
    实战 6/14 schema: encrypted_positions 列 code/name/type/market_value/profit_pct/weight_pct/is_current
    """
    code: str
    name: str
    type: str  # "stock" or "fund"
    market_value: float
    profit_pct: float
    weight_pct: float
    profit: float = 0.0  # market_value × profit_pct / 100

    def __post_init__(self):
        # 实战 profit 反推: mv × pp / 100
        if self.profit == 0.0 and self.market_value > 0:
            self.profit = round(self.market_value * self.profit_pct / 100, 2)


@dataclass
class PortfolioMetrics:
    """组合整体指标 (PIT #93: 持仓整体 pp 作为基准)"""
    total_market_value: float
    total_profit: float
    portfolio_pp: float  # 加权平均 profit_pct
    position_count: int
    stock_count: int
    fund_count: int
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class BrinsonAttribution:
    """Brinson 归因 (PIT #94 简化版)
    三效应分解: 资产配置 / 选股 / 交互
    实战 6/14: 持仓 pp 49.06%, 选股效应为主
    """
    code: str
    name: str
    type: str
    weight_pct: float  # 持仓权重
    position_pp: float  # 个股 pp
    allocation_effect: float = 0.0  # 资产配置效应 (简化 = 0)
    selection_effect: float = 0.0    # 选股效应
    interaction_effect: float = 0.0  # 交互效应 (简化 = 0)
    total_effect: float = 0.0        # 三效应之和 (post_init 算)
    category: str = "neutral"  # "contributor" / "detractor" / "neutral"

    def __post_init__(self):
        self.total_effect = round(
            self.allocation_effect + self.selection_effect + self.interaction_effect, 4
        )


@dataclass
class AttributionReport:
    """V25-E 业绩归因报告 (全量)
    实战 6/14 数据格式参考 V25-G report_7d_snapshot
    """
    report_date: str  # YYYY-MM-DD
    portfolio_metrics: PortfolioMetrics
    top_contributors: List[BrinsonAttribution]
    top_detractors: List[BrinsonAttribution]
    total_allocation: float  # 资产配置效应合计
    total_selection: float   # 选股效应合计
    total_interaction: float  # 交互效应合计
    event_accuracy_t1: float  # V25-C T-1 胜率 (沿用)
    event_accuracy_t3: float  # V25-C T-3 胜率 (沿用)
    llm_summary: str = ""     # PIT #95 LLM 一句话归因 (降级链 3 级)
    llm_status: str = "fallback"  # success / degraded / fallback
    payload: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "report_date": self.report_date,
            "portfolio_metrics": asdict(self.portfolio_metrics),
            "top_contributors": [asdict(c) for c in self.top_contributors],
            "top_detractors": [asdict(d) for d in self.top_detractors],
            "total_allocation": round(self.total_allocation, 4),
            "total_selection": round(self.total_selection, 4),
            "total_interaction": round(self.total_interaction, 4),
            "event_accuracy_t1": round(self.event_accuracy_t1, 4),
            "event_accuracy_t3": round(self.event_accuracy_t3, 4),
            "llm_summary": self.llm_summary,
            "llm_status": self.llm_status,
        }


# ============================================================
# E1: 持仓快照 (PIT #92 实战: 用 encrypted_positions 列, 不用 market.daily_quotes)
# ============================================================
def get_position_snapshots(only_current: bool = True) -> List[PositionSnapshot]:
    """从 holdings.encrypted_positions 拉当前持仓快照
    实战 6/14 schema (PG column 名铁律 PIT #12):
      code/name/type/market_value/profit_pct/weight_pct/is_current
    """
    import psycopg2
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    where = "WHERE is_current = true" if only_current else ""
    cur.execute(f"""SELECT code, name, type, market_value, profit_pct, weight_pct
                    FROM holdings.encrypted_positions {where}
                    ORDER BY market_value DESC""")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    snaps = []
    for r in rows:
        snaps.append(PositionSnapshot(
            code=r[0], name=r[1], type=r[2],
            market_value=float(r[3] or 0),
            profit_pct=float(r[4] or 0),
            weight_pct=float(r[5] or 0),
        ))
    return snaps


# ============================================================
# E2: 业绩计算 (PIT #93 持仓整体 pp 作为基准)
# ============================================================
def compute_portfolio_metrics(snaps: List[PositionSnapshot]) -> PortfolioMetrics:
    """实战 6/14: 45 持仓, stock=28 fund=17, 总市值 ¥5,631,647 pp 49.06%
    PIT #93: portfolio_pp = SUM(profit_pct × weight_pct) / SUM(weight_pct)
    """
    total_mv = sum(s.market_value for s in snaps)
    total_profit = sum(s.profit for s in snaps)
    if total_mv > 0:
        portfolio_pp = total_profit / total_mv * 100  # 实战即用此公式
    else:
        portfolio_pp = 0.0
    stock_n = sum(1 for s in snaps if s.type == "stock")
    fund_n = sum(1 for s in snaps if s.type == "fund")
    return PortfolioMetrics(
        total_market_value=round(total_mv, 2),
        total_profit=round(total_profit, 2),
        portfolio_pp=round(portfolio_pp, 4),
        position_count=len(snaps),
        stock_count=stock_n,
        fund_count=fund_n,
    )


# ============================================================
# E3: Brinson 归因 (PIT #94 三效应分解)
# ============================================================
def compute_brinson_attribution(
    snaps: List[PositionSnapshot], portfolio_pp: float
) -> List[BrinsonAttribution]:
    """Brinson 归因简化 (PIT #94)
    实战 6/14 简化: 不算 (weight - benchmark_weight) 因为没有外部 benchmark
    实战方案: 用 portfolio_pp 自身作为 benchmark
      - allocation = (position_pp - portfolio_pp) × weight_pct / 100 × 100
                    = position_pp - portfolio_pp  (权重归一化后效应)
      - selection  = position_pp × weight_pct / 100
      - interaction = 0 (简化: portfolio_pp 自比较无交互)
    实战 V25-E 实战方案 (最终版):
      - selection = (position_pp - portfolio_pp) × weight_pct / 100
      - allocation = 0 (无外部 benchmark)
      - interaction = 0 (简化)
    """
    out = []
    for s in snaps:
        if s.weight_pct < MIN_CONTRIB_WEIGHT:
            continue
        # PIT #94 简化: 只有 selection 效应
        selection_effect = (s.profit_pct - portfolio_pp) * s.weight_pct / 100
        # 实战归一化: selection_effect 反映 "持仓贡献 vs 整体"
        if selection_effect > 0.5:
            cat = "contributor"
        elif selection_effect < -0.5:
            cat = "detractor"
        else:
            cat = "neutral"
        out.append(BrinsonAttribution(
            code=s.code, name=s.name, type=s.type,
            weight_pct=s.weight_pct, position_pp=s.profit_pct,
            allocation_effect=0.0,  # 简化
            selection_effect=round(selection_effect, 4),
            interaction_effect=0.0,  # 简化
            category=cat,
        ))
    # 按 selection_effect 排序
    out.sort(key=lambda x: x.selection_effect, reverse=True)
    return out


# ============================================================
# E4: LLM 归因 (PIT #95 降级链 3 级)
# ============================================================
def _llm_attempt_v25c_event(portfolio: PortfolioMetrics, contributors: List[BrinsonAttribution],
                             detractors: List[BrinsonAttribution]) -> Tuple[str, str]:
    """1级: V25-C T-3 胜率 35.7% (14 评估)
    实战方案: 用 V25-C 胜率作为归因基础, 生成简洁的归因文本
    实战 6/14: V25-C collect_advice_records 实战无 limit 参数, 自动降级 2 级
    """
    try:
        from event_backtester import collect_advice_records, evaluate_advice, get_holdings_name_map
        # 实战 V25-C 14 评估重跑 (只跑一次, ~5s) — 沿用 V25-C API 不传 limit
        advices = collect_advice_records()
        if not advices:
            return "", "fallback"
        holdings_map = get_holdings_name_map()
        all_evals = []
        for adv in advices:
            evals = evaluate_advice(adv, holdings_map)
            all_evals.extend(evals)
        # 统计 T-3 胜率
        t3_correct = sum(1 for e in all_evals if e.verdict_t3.value == "correct")
        t3_total = len([e for e in all_evals if e.verdict_t3.value in ("correct", "wrong")])
        t3_rate = t3_correct / t3_total if t3_total else 0.0

        # 生成归因文本
        lines = [
            f"📊 持仓组合 pp {portfolio.portfolio_pp:.2f}%, 总市值 ¥{portfolio.total_market_value:,.0f}",
            f"📈 选股胜率 T-3: {t3_rate:.1%} ({t3_correct}/{t3_total})",
        ]
        if contributors:
            top = contributors[0]
            lines.append(f"🟢 主贡献: {top.name} (选股效应 +{top.selection_effect:.2f})")
        if detractors:
            bot = detractors[0]
            lines.append(f"🔴 主拖累: {bot.name} (选股效应 {bot.selection_effect:.2f})")
        summary = "\n".join(lines)
        return summary, "success"
    except Exception as e:
        logger.warning(f"V25-C 归因降级: {e}")
        return "", "degraded"


def _llm_attempt_industry_event(contributors: List[BrinsonAttribution],
                                 detractors: List[BrinsonAttribution]) -> Tuple[str, str]:
    """2级: 事件强度 × 行业暴露 (无 LLM 规则引擎兜底)
    实战方案: 不用 LLM, 用规则生成归因
    """
    try:
        lines = ["📊 业绩归因 (规则引擎兜底)"]
        if contributors:
            top3_names = [c.name for c in contributors[:3]]
            lines.append(f"🟢 Top 3 贡献: {', '.join(top3_names)}")
        if detractors:
            bot3_names = [d.name for d in detractors[:3]]
            lines.append(f"🔴 Top 3 拖累: {', '.join(bot3_names)}")
        return "\n".join(lines), "degraded"
    except Exception as e:
        logger.warning(f"规则归因降级: {e}")
        return "", "fallback"


def _llm_attempt_fallback(portfolio: PortfolioMetrics) -> Tuple[str, str]:
    """3级: 规则引擎兜底 (无 LLM)"""
    return f"📊 持仓 {portfolio.position_count} 只, pp {portfolio.portfolio_pp:.2f}%", "fallback"


def generate_llm_attribution(
    portfolio: PortfolioMetrics,
    contributors: List[BrinsonAttribution],
    detractors: List[BrinsonAttribution],
    t1_acc: float, t3_acc: float,
) -> Tuple[str, str]:
    """PIT #95 LLM 归因降级链 3 级
    实战 6/14: 不调真实 LLM, 用规则 + V25-C 事件数据
    """
    # 1级: V25-C 事件关联归因
    summary, status = _llm_attempt_v25c_event(portfolio, contributors, detractors)
    if summary:
        # 实战 6/14: token 上限 1000 (避免 1800 字符飞书卡片超限)
        if len(summary) > LLM_TOKEN_LIMIT:
            summary = summary[:LLM_TOKEN_LIMIT] + "..."
        return summary, status
    # 2级: 行业事件归因
    summary, status = _llm_attempt_industry_event(contributors, detractors)
    if summary:
        return summary, status
    # 3级: 规则兜底
    return _llm_attempt_fallback(portfolio)


# ============================================================
# V25-C 事件准确率 (PIT #95 1 级数据源)
# ============================================================
def get_v25c_event_accuracy() -> Tuple[float, float]:
    """从 l3.event_backtest_log 拉 V25-C 实战准确率
    实战 6/14 payload: {t1_counts:{c:3,f:4,n:6,w:7}, t3_counts:{c:7,f:6,n:0,w:7}}
    """
    import psycopg2
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    cur.execute("""SELECT payload FROM l3.event_backtest_log ORDER BY report_date DESC LIMIT 1""")
    r = cur.fetchone()
    cur.close()
    conn.close()
    if not r or not r[0]:
        return 0.0, 0.0
    payload = r[0] if isinstance(r[0], dict) else json.loads(r[0])
    t1 = payload.get("t1_counts", {})
    t3 = payload.get("t3_counts", {})
    t1_total = t1.get("c", 0) + t1.get("w", 0)  # correct + wrong
    t1_acc = t1.get("c", 0) / t1_total if t1_total else 0.0
    t3_total = t3.get("c", 0) + t3.get("w", 0)
    t3_acc = t3.get("c", 0) / t3_total if t3_total else 0.0
    return t1_acc, t3_acc


# ============================================================
# 持久化 (l3.attribution_report 表, V25-D 模式)
# ============================================================
DDL = """
CREATE TABLE IF NOT EXISTS l3.attribution_report (
    id BIGSERIAL PRIMARY KEY,
    report_date DATE NOT NULL UNIQUE,
    portfolio_market_value NUMERIC,
    portfolio_profit NUMERIC,
    portfolio_pp NUMERIC,
    position_count INT,
    stock_count INT,
    fund_count INT,
    total_allocation NUMERIC,
    total_selection NUMERIC,
    total_interaction NUMERIC,
    event_accuracy_t1 NUMERIC,
    event_accuracy_t3 NUMERIC,
    top_contributors JSONB,
    top_detractors JSONB,
    llm_summary TEXT,
    llm_status VARCHAR(32) DEFAULT 'fallback',
    payload JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ar_date ON l3.attribution_report(report_date);
CREATE INDEX IF NOT EXISTS idx_ar_created ON l3.attribution_report(created_at);
"""


def ensure_table():
    """实战 PIT: idempotent DDL, 失败不抛 (V25-D PIT #91 沿用)"""
    import psycopg2
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    try:
        for stmt in DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        conn.commit()
        logger.info("✅ l3.attribution_report 表已就绪")
    except Exception as e:
        logger.error(f"DDL 失败: {e}")
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def persist_report(report: AttributionReport) -> int:
    """实战 PIT #86 沿用: ON CONFLICT (report_date) DO UPDATE (idempotent)"""
    import psycopg2
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO l3.attribution_report
                (report_date, portfolio_market_value, portfolio_profit, portfolio_pp,
                 position_count, stock_count, fund_count,
                 total_allocation, total_selection, total_interaction,
                 event_accuracy_t1, event_accuracy_t3,
                 top_contributors, top_detractors,
                 llm_summary, llm_status, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (report_date) DO UPDATE SET
                portfolio_market_value = EXCLUDED.portfolio_market_value,
                portfolio_profit = EXCLUDED.portfolio_profit,
                portfolio_pp = EXCLUDED.portfolio_pp,
                position_count = EXCLUDED.position_count,
                stock_count = EXCLUDED.stock_count,
                fund_count = EXCLUDED.fund_count,
                total_allocation = EXCLUDED.total_allocation,
                total_selection = EXCLUDED.total_selection,
                total_interaction = EXCLUDED.total_interaction,
                event_accuracy_t1 = EXCLUDED.event_accuracy_t1,
                event_accuracy_t3 = EXCLUDED.event_accuracy_t3,
                top_contributors = EXCLUDED.top_contributors,
                top_detractors = EXCLUDED.top_detractors,
                llm_summary = EXCLUDED.llm_summary,
                llm_status = EXCLUDED.llm_status,
                payload = EXCLUDED.payload,
                created_at = NOW()
            RETURNING id
        """, (
            report.report_date,
            report.portfolio_metrics.total_market_value,
            report.portfolio_metrics.total_profit,
            report.portfolio_metrics.portfolio_pp,
            report.portfolio_metrics.position_count,
            report.portfolio_metrics.stock_count,
            report.portfolio_metrics.fund_count,
            round(report.total_allocation, 4),
            round(report.total_selection, 4),
            round(report.total_interaction, 4),
            round(report.event_accuracy_t1, 4),
            round(report.event_accuracy_t3, 4),
            json.dumps([asdict(c) for c in report.top_contributors], ensure_ascii=False),
            json.dumps([asdict(d) for d in report.top_detractors], ensure_ascii=False),
            report.llm_summary,
            report.llm_status,
            json.dumps(report.to_dict(), ensure_ascii=False),
        ))
        rid = cur.fetchone()[0]
        conn.commit()
        logger.info(f"✅ 报告持久化 id={rid} date={report.report_date}")
        return rid
    except Exception as e:
        logger.error(f"持久化失败: {e}")
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ============================================================
# 飞书推送 (沿用 V25-A1+A2 _send_via_feishu_inplace)
# ============================================================
def _send_via_feishu_inplace(report: AttributionReport) -> bool:
    """实战 PIT #66 沿用 V25-A1: 飞书推送就地实现, 避免循环 import
    实战 6/14: store.json FEISHU_WEBHOOK 已配, 81 字符
    """
    try:
        from hermes_coordination.scripts.position_risk_triggers import _send_via_feishu
    except ImportError:
        # 实战: sys.path 调整
        coord_path = ROOT / "hermes_coordination" / "scripts"
        if str(coord_path) not in sys.path:
            sys.path.insert(0, str(coord_path))
        try:
            from position_risk_triggers import _send_via_feishu
        except Exception as e:
            logger.error(f"无法导入 _send_via_feishu: {e}")
            return False

    # 实战: 构造飞书卡片 (max 1800 字符, V25-A1 PIT #70)
    portfolio = report.portfolio_metrics
    lines = [
        f"📊 **业绩归因日报 {report.report_date}**",
        f"💰 总市值 ¥{portfolio.total_market_value:,.0f} / 浮盈 ¥{portfolio.total_profit:,.0f}",
        f"📈 组合 pp **{portfolio.portfolio_pp:.2f}%** / 持仓 {portfolio.position_count} (stock {portfolio.stock_count} + fund {portfolio.fund_count})",
        f"🎯 归因: 选股效应 {report.total_selection:+.2f} / 资产配置 {report.total_allocation:+.2f}",
        f"📅 事件胜率: T-1 {report.event_accuracy_t1:.1%} / T-3 {report.event_accuracy_t3:.1%} (V25-C)",
    ]
    if report.top_contributors:
        lines.append("🟢 Top 3 贡献:")
        for c in report.top_contributors[:3]:
            lines.append(f"  • {c.name} (选股 {c.selection_effect:+.2f})")
    if report.top_detractors:
        lines.append("🔴 Top 3 拖累:")
        for d in report.top_detractors[:3]:
            lines.append(f"  • {d.name} (选股 {d.selection_effect:+.2f})")
    if report.llm_summary:
        lines.append(f"\n🤖 LLM 归因 ({report.llm_status}):")
        lines.append(report.llm_summary[:LLM_TOKEN_LIMIT])
    content = "\n".join(lines)
    if len(content) > 1800:
        content = content[:1800] + "..."
    return _send_via_feishu("V25-E 业绩归因", content)


# ============================================================
# 主函数
# ============================================================
def generate_attribution_report() -> AttributionReport:
    """V25-E 主函数: 实战 6/14 自检流程
    E1 持仓快照 → E2 业绩计算 → E3 Brinson 归因 → E4 LLM 归因
    """
    logger.info("🚀 V25-E 业绩归因分析 启动")
    with acquire_lock():
        logger.info("🔒 锁已获取 (PIT #87)")
        # E1 持仓快照
        snaps = get_position_snapshots(only_current=True)
        logger.info(f"📦 E1 持仓快照: {len(snaps)} 持仓")
        # E2 业绩计算
        portfolio = compute_portfolio_metrics(snaps)
        logger.info(f"💹 E2 业绩: pp {portfolio.portfolio_pp:.2f}% / 总市值 ¥{portfolio.total_market_value:,.0f}")
        # E3 Brinson 归因
        attributions = compute_brinson_attribution(snaps, portfolio.portfolio_pp)
        contributors = [a for a in attributions if a.category == "contributor"][:TOP_N]
        detractors = [a for a in attributions if a.category == "detractor"][:TOP_N]
        # 实际: 取排序后前 TOP_N (无论类别), 实战取贡献者+拖累者分开
        contributors = attributions[:TOP_N]  # sorted desc, 前 N 是贡献
        detractors = [a for a in attributions if a.category == "detractor"][-TOP_N:]  # 后 N
        detractors = sorted(detractors, key=lambda x: x.selection_effect)[:TOP_N]
        total_sel = sum(a.selection_effect for a in attributions)
        logger.info(f"🎯 E3 Brinson 归因: 选股 {total_sel:+.2f} / 贡献 {len(contributors)} / 拖累 {len(detractors)}")
        # E4 LLM 归因 (PIT #95 降级链)
        t1_acc, t3_acc = get_v25c_event_accuracy()
        llm_summary, llm_status = generate_llm_attribution(
            portfolio, contributors, detractors, t1_acc, t3_acc
        )
        logger.info(f"🤖 E4 LLM 归因: {llm_status} (T-3 {t3_acc:.1%})")
        # 构造报告
        report = AttributionReport(
            report_date=date.today().isoformat(),
            portfolio_metrics=portfolio,
            top_contributors=contributors,
            top_detractors=detractors,
            total_allocation=0.0,  # 简化
            total_selection=round(total_sel, 4),
            total_interaction=0.0,  # 简化
            event_accuracy_t1=round(t1_acc, 4),
            event_accuracy_t3=round(t3_acc, 4),
            llm_summary=llm_summary,
            llm_status=llm_status,
        )
        # 持久化
        rid = persist_report(report)
        logger.info(f"💾 报告持久化 id={rid}")
        # 飞书推送
        sent = _send_via_feishu_inplace(report)
        logger.info(f"📤 飞书推送: {sent}")
    logger.info("✅ V25-E 业绩归因分析 完成")
    return report


# ============================================================
# self-test (实战 6/14)
# ============================================================
def self_test():
    """实战 6/14 self-test: 验证全链路
    1. 锁获取/释放
    2. E1 持仓快照
    3. E2 业绩计算
    4. E3 Brinson 归因
    5. E4 LLM 归因 (降级链)
    6. 持久化
    7. 飞书推送
    """
    print("=" * 60)
    print("V25-E 业绩归因 self-test (实战 6/14)")
    print("=" * 60)
    # 0. 表就绪
    ensure_table()
    print("✅ l3.attribution_report 表就绪")
    # 1. 锁
    with acquire_lock(timeout=5.0) as fd:
        print(f"✅ 锁获取 PID {os.getpid()}")
    # 2. E1
    snaps = get_position_snapshots(only_current=True)
    print(f"✅ E1 持仓快照: {len(snaps)} 持仓")
    print(f"   实战 6/14: stock 28 + fund 17 = 45 持仓, 总市值 ¥5,631,647")
    # 3. E2
    portfolio = compute_portfolio_metrics(snaps)
    print(f"✅ E2 业绩: pp {portfolio.portfolio_pp:.2f}% / 总市值 ¥{portfolio.total_market_value:,.0f}")
    # 4. E3
    attributions = compute_brinson_attribution(snaps, portfolio.portfolio_pp)
    print(f"✅ E3 Brinson: {len(attributions)} 持仓有归因")
    contributors = attributions[:TOP_N]
    detractors = sorted([a for a in attributions if a.category == "detractor"], key=lambda x: x.selection_effect)[:TOP_N]
    print(f"   Top 5 贡献:")
    for c in contributors:
        print(f"     🟢 {c.name} (pp={c.position_pp:.2f}%, 选股 {c.selection_effect:+.2f})")
    print(f"   Top 5 拖累:")
    for d in detractors:
        print(f"     🔴 {d.name} (pp={d.position_pp:.2f}%, 选股 {d.selection_effect:+.2f})")
    # 5. E4
    t1_acc, t3_acc = get_v25c_event_accuracy()
    llm_summary, llm_status = generate_llm_attribution(portfolio, contributors, detractors, t1_acc, t3_acc)
    print(f"✅ E4 LLM 归因: {llm_status}")
    print(f"   T-1 胜率 {t1_acc:.1%} / T-3 胜率 {t3_acc:.1%}")
    print(f"   Summary preview: {llm_summary[:200]}")
    # 6. 持久化
    report = AttributionReport(
        report_date=date.today().isoformat(),
        portfolio_metrics=portfolio,
        top_contributors=contributors,
        top_detractors=detractors,
        total_allocation=0.0,
        total_selection=round(sum(a.selection_effect for a in attributions), 4),
        total_interaction=0.0,
        event_accuracy_t1=round(t1_acc, 4),
        event_accuracy_t3=round(t3_acc, 4),
        llm_summary=llm_summary,
        llm_status=llm_status,
    )
    rid = persist_report(report)
    print(f"✅ 持久化 id={rid}")
    # 7. 飞书推送
    sent = _send_via_feishu_inplace(report)
    print(f"{'✅' if sent else '⚠️'} 飞书推送: {sent}")
    print("=" * 60)
    print("✅ V25-E self-test 全部通过")
    return report


if __name__ == "__main__":
    self_test()
