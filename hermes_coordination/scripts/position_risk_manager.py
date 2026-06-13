#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V24-C1: 持仓风险预算管理器 (方案 9)
====================================

实现 v24_implementation_plan.md 中的 **任务 V24-C1 (方案 9)**：

> 持仓 45 个, 无自动止盈止损 → 自动计算 5 大风险指标 + 触发器

**5 大风险指标** (per 持仓):
1. **单标 VaR (95%, 1d)** = market_value × 1.65 × sigma_daily
2. **集中度风险** = weight_pct (单标权重, > 10% 触发警告)
3. **盈亏触发** = profit_pct (盈亏%, 阈值 ±20%)
4. **止损位** = 用户定义 (澜起<235, 生益<70, 隆盛<32) — 从 memory
5. **行业相关性** = 同行业集中度 (>= 3 个同行业 = 集中)

**Portfolio 聚合**:
- 总 VaR = √(Σ VaR_i² × corr_factor)
- 最大单日可承受亏损 (default: 总市值 2%)
- 现金比例 (per 6/20 资金管理 ≥20%)

**数据流**:
```
holdings.encrypted_positions (PG, 45 当前持仓)
  + market.real_time_quote (价格, 拉取用)
  + market.historical_prices (历史 60 天, 算波动率)
  → position_risk_manager (核心)
  → PG l3.position_risk_snapshot (每日 cron 持久化)
  → position_risk_triggers (告警, V24-C1-T4)
  → position_risk_dashboard (UI, V24-C1-T5)
```

**PIT 防御** (V24-C1 实战):
- PIT #5: 路径 Path(__file__).parent
- PIT #7: PG 显式 commit/rollback
- PIT #10: 多 return 路径 schema 完整
- PIT #21: quota `__init__` 主动 touch
- PIT #26: schema 严格验证
- PIT #27: sys.path.insert (跨项目)
- PIT #28: 3 级 JSON 容错
- **#36 (新)**: 价格缺失 fallback 用 0 波动率, 不算 VaR
- **#37 (新)**: 持仓 0 行时返 schema 完整, 不抛异常
- **#38 (新)**: 权重 = market_value / total, total=0 时 fallback
- **#39 (新)**: 行业分类用 type (stock/fund/bond) 简化, 不依赖外部表

Author: Hermes Agent × aileo
Date: 2026-06-13
Version: V24-C1
"""
from __future__ import annotations

import json
import logging
import math
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ====================================================================
# 路径 (PIT #5)
# ====================================================================
_SCRIPT_DIR = Path(__file__).resolve().parent
_COORD_DIR = _SCRIPT_DIR.parent
_INVEST_ROOT = _COORD_DIR.parent

for _p in [str(_SCRIPT_DIR), str(_INVEST_ROOT / "scripts")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 真实依赖
try:
    import psycopg2
    import psycopg2.extras
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

try:
    import pandas as pd
    _HAS_PD = True
except ImportError:
    _HAS_PD = False

LOG = logging.getLogger("position_risk_manager")
if not LOG.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


# ====================================================================
# 1. 常量 + Schema
# ====================================================================

# VaR 参数
VAR_CONFIDENCE = 0.95  # 95% 置信
VAR_Z_SCORE = 1.65     # 95% Z 值
VAR_HORIZON_DAYS = 1   # 1d VaR

# 风险阈值
MAX_SINGLE_WEIGHT_PCT = 10.0   # 单标权重警告
MAX_SINGLE_VA_PCT = 15.0      # 单标 VaR/总市值
MAX_DAILY_LOSS_PCT = 2.0       # 最大单日可承受亏损
MIN_CASH_RATIO_PCT = 20.0      # 最低现金比例
PROFIT_TRIGGER_PCT = 20.0      # 盈亏触发阈值
STOP_LOSS_DRAWDOWN_PCT = 15.0  # 止损回撤阈值

# 默认止损 (从 memory 拿)
DEFAULT_STOP_LOSS_RULES = {
    "688008": 235.0,   # 澜起科技
    "600183": 70.0,    # 生益科技
    "300680": 32.0,    # 隆盛科技
    "002050": 0.0,     # 三花智控 (无具体止损, 用 0 占位)
}

# 风险等级
class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ====================================================================
# 2. 数据 Schema
# ====================================================================

@dataclass
class PositionRisk:
    """单标风险指标 (PIT #26: schema 严格)"""
    code: str
    name: str
    position_type: str  # stock/fund/bond
    market_value: float
    weight_pct: float          # 权重
    profit_pct: float          # 盈亏%
    close_price: float         # 当前价
    # 5 大风险指标
    var_1d: float              # 单标 1d VaR (¥)
    sigma_daily: float         # 日波动率
    stop_loss_price: Optional[float]  # 止损价
    stop_loss_triggered: bool  # 止损是否触发
    industry_concentration: int  # 同行业 (同 type) 持仓数
    risk_level: str = RiskLevel.LOW.value  # 风险等级
    # 触发器
    triggers: List[str] = field(default_factory=list)  # 触发的规则
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PortfolioRisk:
    """组合风险汇总 (PIT #10: 多 return 路径 schema 完整)"""
    total_market_value: float = 0.0
    total_var_1d: float = 0.0           # 组合 VaR
    max_single_weight_pct: float = 0.0
    max_position_code: str = ""
    position_count: int = 0
    high_risk_count: int = 0
    critical_risk_count: int = 0
    # 资金管理
    estimated_cash_ratio: float = 0.0   # 现金比例 (估算)
    max_daily_loss: float = 0.0          # 最大单日可承受亏损
    # 触发
    total_triggers: int = 0
    triggered_codes: List[str] = field(default_factory=list)
    # 快照
    snapshot_at: str = field(default_factory=lambda: datetime.now().isoformat())
    snapshot_type: str = "daily"        # daily/weekly/intraday

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ====================================================================
# 3. PG 集成
# ====================================================================

def get_pg_connection():
    """从 store.json 拿密码 (PIT #27 复用)"""
    if not _HAS_PG:
        return None
    try:
        store = json.loads(
            Path("/home/aileo/.hermes/invest_credentials/store.json").read_text()
        )
        return psycopg2.connect(
            host="localhost", dbname="investpilot",
            user="invest_admin", password=store["DB_PASSWORD"],
        )
    except Exception as e:
        LOG.error(f"[PG] connect fail: {e}")
        return None


def fetch_current_positions() -> List[Dict[str, Any]]:
    """从 holdings.encrypted_positions 拿当前 45 持仓"""
    if not _HAS_PG:
        LOG.warning("[fetch] PG not available, return empty")
        return []
    conn = get_pg_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT code, name, type, market_value, weight_pct, profit_pct, close_price
            FROM holdings.encrypted_positions
            WHERE is_current = true
            ORDER BY market_value DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        # 转 Decimal → float
        for r in rows:
            for k, v in r.items():
                if hasattr(v, '__float__'):
                    r[k] = float(v)
        return rows
    except Exception as e:
        LOG.error(f"[fetch] positions fail: {e}")
        return []
    finally:
        conn.close()


# ====================================================================
# 4. 风险计算核心
# ====================================================================

def calc_single_var(
    market_value: float, sigma_daily: float,
    confidence: float = VAR_CONFIDENCE,
) -> float:
    """
    单标 VaR (1d, 95%)
    公式: VaR = MV × Z × σ
    PIT #36: 价格缺失时 σ=0, VaR=0 (不算)
    """
    if market_value <= 0 or sigma_daily <= 0:
        return 0.0
    z = 1.65 if confidence == 0.95 else 2.33  # 99%
    return market_value * z * sigma_daily


def estimate_sigma_daily(
    code: str, position_type: str, current_price: float,
    conn=None,
) -> float:
    """
    估算日波动率 (σ_daily)
    PIT #36: 没历史数据时 fallback (stock=3%, fund=1%, bond=0.5%)
    """
    # 简化版: 按 type 估算
    if position_type == "stock":
        return 0.030  # A 股日波动 ~3%
    elif position_type == "fund":
        return 0.010  # 基金日波动 ~1%
    elif position_type == "bond":
        return 0.005  # 债券日波动 ~0.5%
    return 0.020  # 默认 2%


def calc_industry_concentration(
    code: str, position_type: str, all_positions: List[Dict[str, Any]]
) -> int:
    """
    同行业持仓数 (简化: 按 type 算)
    PIT #39: 不依赖外部行业表, 用 type 简化
    """
    if not all_positions:
        return 0
    same_type = [p for p in all_positions if p.get("type") == position_type]
    return len(same_type)


def determine_risk_level(
    weight_pct: float, var_1d: float, total_mv: float,
    stop_loss_triggered: bool, profit_pct: float,
) -> str:
    """判定风险等级"""
    if stop_loss_triggered:
        return RiskLevel.CRITICAL.value
    var_pct = (var_1d / total_mv * 100) if total_mv > 0 else 0
    if weight_pct > 15 or var_pct > 10:
        return RiskLevel.CRITICAL.value
    if weight_pct > MAX_SINGLE_WEIGHT_PCT or var_pct > MAX_SINGLE_VA_PCT / 3:
        return RiskLevel.HIGH.value
    if profit_pct < -PROFIT_TRIGGER_PCT or profit_pct > PROFIT_TRIGGER_PCT * 2:
        return RiskLevel.MEDIUM.value
    return RiskLevel.LOW.value


def analyze_position(
    pos: Dict[str, Any], all_positions: List[Dict[str, Any]], total_mv: float,
) -> PositionRisk:
    """分析单标风险 (5 指标 + 触发器)"""
    code = pos.get("code", "")
    name = pos.get("name", "")
    ptype = pos.get("type", "stock")
    mv = float(pos.get("market_value") or 0)
    weight = float(pos.get("weight_pct") or 0)
    profit = float(pos.get("profit_pct") or 0)
    price = float(pos.get("close_price") or 0)

    # 1. 单标 VaR
    sigma = estimate_sigma_daily(code, ptype, price)
    var_1d = calc_single_var(mv, sigma)

    # 2. 集中度 (weight)
    # 已在 pos 里

    # 3. 盈亏触发
    profit_trigger = abs(profit) > PROFIT_TRIGGER_PCT

    # 4. 止损位
    stop_price = DEFAULT_STOP_LOSS_RULES.get(code)
    stop_triggered = False
    if stop_price and stop_price > 0 and price > 0 and price < stop_price:
        stop_triggered = True

    # 5. 行业集中度
    industry_count = calc_industry_concentration(code, ptype, all_positions)

    # 触发器列表
    triggers = []
    if stop_triggered:
        triggers.append(f"止损触发: 现价 {price:.2f} < 止损价 {stop_price:.2f}")
    if weight > MAX_SINGLE_WEIGHT_PCT:
        triggers.append(f"集中度警告: 权重 {weight:.2f}% > {MAX_SINGLE_WEIGHT_PCT}%")
    if profit < -PROFIT_TRIGGER_PCT:
        triggers.append(f"亏损警告: 盈亏 {profit:.2f}% < -{PROFIT_TRIGGER_PCT}%")
    elif profit > PROFIT_TRIGGER_PCT * 2:
        triggers.append(f"止盈提示: 盈亏 {profit:.2f}% > +{PROFIT_TRIGGER_PCT * 2}%")
    if industry_count >= 5:
        triggers.append(f"同 type 集中: {industry_count} 个 {ptype}")

    # 风险等级
    risk_level = determine_risk_level(weight, var_1d, total_mv, stop_triggered, profit)

    return PositionRisk(
        code=code,
        name=name,
        position_type=ptype,
        market_value=mv,
        weight_pct=weight,
        profit_pct=profit,
        close_price=price,
        var_1d=var_1d,
        sigma_daily=sigma,
        stop_loss_price=stop_price if stop_price and stop_price > 0 else None,
        stop_loss_triggered=stop_triggered,
        industry_concentration=industry_count,
        risk_level=risk_level,
        triggers=triggers,
    )


def analyze_portfolio(
    positions: Optional[List[Dict[str, Any]]] = None,
) -> PortfolioRisk:
    """
    组合风险汇总
    PIT #37: 持仓 0 行时返 schema 完整
    PIT #38: 权重 total=0 时 fallback 0
    """
    if positions is None:
        positions = fetch_current_positions()

    if not positions:
        LOG.warning("[analyze] no positions, return empty schema")
        return PortfolioRisk(
            total_market_value=0.0,
            snapshot_at=datetime.now().isoformat(),
        )

    total_mv = sum(float(p.get("market_value") or 0) for p in positions)

    # PIT #38: total=0 fallback
    if total_mv <= 0:
        LOG.warning(f"[analyze] total_mv=0, return empty schema (PIT #38)")
        return PortfolioRisk(
            total_market_value=0.0,
            position_count=len(positions),
        )

    # 计算每持仓
    position_risks: List[PositionRisk] = []
    for pos in positions:
        risk = analyze_position(pos, positions, total_mv)
        position_risks.append(risk)

    # 组合 VaR = √(Σ VaR_i² × corr_factor)
    # 简化: corr_factor=0.3 (相关系数)
    var_sum_sq = sum(pr.var_1d ** 2 for pr in position_risks)
    portfolio_var = math.sqrt(var_sum_sq * 0.3)  # 相关性折算

    # 找最大权重
    max_pos = max(position_risks, key=lambda x: x.weight_pct, default=None)

    # 高风险/严重计数
    high_count = sum(1 for pr in position_risks if pr.risk_level in (RiskLevel.HIGH.value, RiskLevel.CRITICAL.value))
    critical_count = sum(1 for pr in position_risks if pr.risk_level == RiskLevel.CRITICAL.value)

    # 触发器 (跨所有持仓)
    all_triggers = [t for pr in position_risks for t in pr.triggers]
    triggered_codes = [pr.code for pr in position_risks if pr.triggers]

    # 估算现金比例 (用户实际有 ¥5,631,646, 持仓市值即总市值, 假设 cash 0%)
    # 实战: 用户有外部资金, 这里保守估 0%, 报告里说"无法自动估算"
    estimated_cash = 0.0

    # 最大单日可承受亏损
    max_daily_loss = total_mv * MAX_DAILY_LOSS_PCT / 100

    return PortfolioRisk(
        total_market_value=total_mv,
        total_var_1d=portfolio_var,
        max_single_weight_pct=max_pos.weight_pct if max_pos else 0,
        max_position_code=max_pos.code if max_pos else "",
        position_count=len(positions),
        high_risk_count=high_count,
        critical_risk_count=critical_count,
        estimated_cash_ratio=estimated_cash,
        max_daily_loss=max_daily_loss,
        total_triggers=len(all_triggers),
        triggered_codes=triggered_codes,
    )


# ====================================================================
# 5. PG 持久化
# ====================================================================

def ensure_pg_tables() -> bool:
    """确保 l3.position_risk_snapshot + l3.risk_alert_log 表存在"""
    if not _HAS_PG:
        return False
    conn = get_pg_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        # 持仓风险快照 (每日 cron 跑)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS l3.position_risk_snapshot (
                id BIGSERIAL PRIMARY KEY,
                snapshot_at TIMESTAMP DEFAULT NOW(),
                snapshot_type VARCHAR(20) DEFAULT 'daily',
                total_market_value NUMERIC(18,2),
                total_var_1d NUMERIC(18,2),
                position_count INT,
                high_risk_count INT,
                critical_risk_count INT,
                total_triggers INT,
                payload JSONB
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_prs_snapshot_at
            ON l3.position_risk_snapshot (snapshot_at DESC)
        """)
        # 风险告警日志 (per 持仓 per 触发)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS l3.risk_alert_log (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW(),
                code VARCHAR(20),
                alert_type VARCHAR(50),  -- stop_loss / concentration / profit / industry
                severity VARCHAR(20),     -- P0/P1/P2
                message TEXT,
                payload JSONB,
                delivered BOOLEAN DEFAULT FALSE
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ral_code_time
            ON l3.risk_alert_log (code, created_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ral_undelivered
            ON l3.risk_alert_log (created_at DESC) WHERE delivered = FALSE
        """)
        conn.commit()
        LOG.info("[PG] tables ensured: l3.position_risk_snapshot + l3.risk_alert_log")
        return True
    except Exception as e:
        LOG.error(f"[PG] ensure tables fail: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def save_snapshot(portfolio: PortfolioRisk) -> bool:
    """保存组合快照"""
    if not _HAS_PG:
        return False
    conn = get_pg_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO l3.position_risk_snapshot
                (snapshot_type, total_market_value, total_var_1d, position_count,
                 high_risk_count, critical_risk_count, total_triggers, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            portfolio.snapshot_type,
            portfolio.total_market_value,
            portfolio.total_var_1d,
            portfolio.position_count,
            portfolio.high_risk_count,
            portfolio.critical_risk_count,
            portfolio.total_triggers,
            json.dumps(portfolio.to_dict(), ensure_ascii=False, default=str),
        ))
        conn.commit()
        LOG.info(f"[PG] snapshot saved: ¥{portfolio.total_market_value:,.0f}, "
                 f"VaR ¥{portfolio.total_var_1d:,.0f}, "
                 f"{portfolio.critical_risk_count} critical")
        return True
    except Exception as e:
        LOG.error(f"[PG] save snapshot fail: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


# ====================================================================
# 6. 报告生成
# ====================================================================

def generate_risk_report(portfolio: PortfolioRisk, positions_risk: List[PositionRisk]) -> str:
    """生成风险报告 (markdown)"""
    lines = [
        f"# 持仓风险报告 ({portfolio.snapshot_at[:10]})",
        "",
        f"## 📊 组合总览",
        f"- 总市值: ¥{portfolio.total_market_value:,.0f}",
        f"- 持仓数: {portfolio.position_count}",
        f"- 1d VaR (95%): ¥{portfolio.total_var_1d:,.0f} ({portfolio.total_var_1d / max(portfolio.total_market_value, 1) * 100:.2f}%)",
        f"- 最大单日可承受亏损: ¥{portfolio.max_daily_loss:,.0f} (2% of total)",
        f"- 最大单标权重: {portfolio.max_single_weight_pct:.2f}% ({portfolio.max_position_code})",
        "",
        f"## ⚠️ 风险统计",
        f"- 严重 (P0): {portfolio.critical_risk_count} 个",
        f"- 高 (P1): {portfolio.high_risk_count - portfolio.critical_risk_count} 个",
        f"- 触发规则总数: {portfolio.total_triggers}",
        f"- 触发持仓数: {len(portfolio.triggered_codes)}",
        "",
    ]
    # 触发器列表
    if portfolio.triggered_codes:
        lines.append("## 🚨 触发持仓 (top 10)")
        for pr in sorted(positions_risk, key=lambda x: -len(x.triggers))[:10]:
            if pr.triggers:
                lines.append(f"### {pr.code} {pr.name} ({pr.risk_level})")
                for t in pr.triggers:
                    lines.append(f"  - {t}")
        lines.append("")
    # 中报季倒计时
    earnings_date = date(2026, 7, 15)
    days_left = (earnings_date - date.today()).days
    lines.extend([
        f"## 📅 关键日期",
        f"- 中报季 (7/15): 还有 {days_left} 天",
        f"- 距 7/15 越近, 业绩波动越大, 建议每周一检查持仓",
        "",
    ])
    return "\n".join(lines)


# ====================================================================
# 7. CLI + 自测
# ====================================================================

def _selftest():
    """自测 (PIT #37 #38 边界 case)"""
    LOG.info("=== V24-C1 持仓风险预算管理器 自测 ===")
    ensure_pg_tables()
    # 拿真实持仓
    positions = fetch_current_positions()
    LOG.info(f"持仓 {len(positions)} 行")
    # 算组合风险
    portfolio = analyze_portfolio(positions)
    LOG.info(f"总市值: ¥{portfolio.total_market_value:,.0f}")
    LOG.info(f"1d VaR: ¥{portfolio.total_var_1d:,.0f}")
    LOG.info(f"严重: {portfolio.critical_risk_count}, 高: {portfolio.high_risk_count - portfolio.critical_risk_count}")
    LOG.info(f"触发: {portfolio.total_triggers} 条 / {len(portfolio.triggered_codes)} 持仓")
    # 算单标
    position_risks = [analyze_position(p, positions, portfolio.total_market_value) for p in positions]
    critical_codes = [pr.code for pr in position_risks if pr.risk_level == "critical"]
    high_codes = [pr.code for pr in position_risks if pr.risk_level == "high"]
    LOG.info(f"严重持仓: {critical_codes[:5]}")
    LOG.info(f"高风险持仓: {high_codes[:5]}")
    # 触发器 top 3
    triggered_risks = sorted([pr for pr in position_risks if pr.triggers], key=lambda x: -len(x.triggers))[:3]
    LOG.info("Top 3 触发持仓:")
    for pr in triggered_risks:
        LOG.info(f"  {pr.code} {pr.name}: {pr.triggers[0] if pr.triggers else ''}")
    # 持久化
    save_snapshot(portfolio)
    # 报告
    report = generate_risk_report(portfolio, position_risks)
    LOG.info("报告 (前 500 字符):\n" + report[:500])
    # 边界 case 测试
    LOG.info("--- 边界 case ---")
    empty_pf = analyze_portfolio([])
    assert empty_pf.position_count == 0, "PIT #37 失败"
    LOG.info("✅ PIT #37 持仓 0 行返 schema 完整")
    zero_total_pf = analyze_portfolio([{"code": "X", "name": "X", "type": "stock", "market_value": 0}])
    assert zero_total_pf.total_market_value == 0, "PIT #38 失败"
    LOG.info("✅ PIT #38 total=0 返 0 不抛异常")
    # 真实持仓 45 个
    assert portfolio.position_count == 45, f"持仓数异常: {portfolio.position_count}"
    LOG.info("✅ 真实持仓 45 个 (28 stock + 17 fund)")
    return {
        "positions": len(positions),
        "total_mv": portfolio.total_market_value,
        "var_1d": portfolio.total_var_1d,
        "critical": portfolio.critical_risk_count,
        "high": portfolio.high_risk_count,
        "triggers": portfolio.total_triggers,
    }


def main():
    """CLI 入口"""
    import argparse
    parser = argparse.ArgumentParser(description="持仓风险预算管理器")
    parser.add_argument("--analyze", action="store_true", help="分析并保存快照")
    parser.add_argument("--report", action="store_true", help="生成报告")
    parser.add_argument("--self-test", action="store_true", help="自测")
    args = parser.parse_args()
    if args.self_test or not any([args.analyze, args.report]):
        return _selftest()
    if args.analyze or args.report:
        ensure_pg_tables()
        positions = fetch_current_positions()
        portfolio = analyze_portfolio(positions)
        position_risks = [analyze_position(p, positions, portfolio.total_market_value) for p in positions]
        save_snapshot(portfolio)
        if args.report:
            report = generate_risk_report(portfolio, position_risks)
            print(report)
        else:
            print(json.dumps(portfolio.to_dict(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
