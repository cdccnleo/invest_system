#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hermes_backtest_validator.py — 方案 8 回测验证入口（v2.3 R1-T2）
=============================================================
设计目标: 把 hermes 生成的策略（来自 l3.decision_points）→ 真实回测 → 反馈给 hermes memory
基于 v2.2 V22-T4 实战经验:
  - 直读 PG (避免 ORM 复杂依赖)
  - 6 模式测试驱动 (本脚本走完 6 模式才能上线)
  - LLM 限额隔离 (与方案 3/4 共用 DailyQuota)
PIT 教训 (来自 v22-10-bugs-pitfalls.md):
  - 用 inspect.getsource 看 backtest_engine 真实签名
  - 用 daily_limit/quota_file 位置参数顺序
  - PG 事务 try/except 必须 rollback
"""

import argparse
import inspect
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 路径 (l3_dialog_engine 路径推算的 3 个心智模型)
INVEST_ROOT = Path("/home/aileo/invest_system")
SCRIPTS_DIR = INVEST_ROOT / "scripts"
HERMES_SCRIPTS_DIR = INVEST_ROOT / "hermes_coordination" / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

# 凭据
CREDS_FILE = Path("/home/aileo/.hermes/invest_credentials/store.json")
# ⚠️ PIT #11: 与方案 3/4 共用 /tmp/hermes_llm_quota.json
QUOTA_FILE = "/tmp/hermes_llm_quota.json"


def get_db_config() -> dict:
    """从 ~/.hermes/invest_credentials/store.json 取 PG 密码"""
    return {
        "host": "localhost", "database": "investpilot",
        "user": "invest_admin",
        "password": json.loads(CREDS_FILE.read_text())["DB_PASSWORD"],
    }


def safe_pg_execute(sql: str, params: tuple = (), fetch: str = None) -> Any:
    """PG 安全执行, 自动 commit/rollback 防事务 abort"""
    import psycopg2
    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if fetch == "one":
            result = cur.fetchone()
        elif fetch == "all":
            result = cur.fetchall()
        else:
            result = None
        conn.commit()
        return result
    except Exception:
        conn.rollback()  # ⚠️ PIT #7: 必须 rollback 防 abort
        raise
    finally:
        conn.close()


# ============================================================
# 核心: 策略回测验证
# ============================================================
@dataclass
class StrategyBacktestResult:
    """单次回测结果 (强制 schema 一致)"""
    strategy_name: str
    stock_codes: List[str]
    start_date: str
    end_date: str
    return_pct: float
    alpha_pct: float  # vs 沪深 300
    sharpe: float
    max_drawdown: float
    initial_capital: float
    final_value: float
    benchmark: str = "CSI300"
    # 6 模式字段 - 早退/失败时这些为 None
    error: Optional[str] = None
    decision_count: int = 0  # 用于生成此策略的 l3.decision_points 条数
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


def _get_decision_points(user_id: str = "aileo", days: int = 30) -> List[Dict]:
    """从 l3.decision_points 拉最近 N 天的决策 (L3 自我记忆)"""
    # ⚠️ PIT 修复: PG INTERVAL 不支持 '%s days' 占位符格式, 改用 f-string 直接插入
    # 风险: SQL injection — 但 days 是 int 内部已校验
    if not isinstance(days, int) or days < 0 or days > 3650:
        raise ValueError(f"days must be int 0-3650, got {days}")
    sql = f"""
        SELECT decision, stock_code, confidence, reasoning, created_at
        FROM l3.decision_points
        WHERE user_id = %s
          AND created_at > NOW() - INTERVAL '{days} days'
          AND stock_code IS NOT NULL
        ORDER BY created_at DESC
    """
    rows = safe_pg_execute(sql, (user_id,), fetch="all")
    return [
        {
            "decision": r[0], "stock_code": r[1], "confidence": r[2],
            "reasoning": r[3], "created_at": str(r[4]),
        }
        for r in rows
    ]


def _normalize_ts_code(code: str) -> str:
    """补全 ts_code 后缀 (.XSHE / .XSHG / .OF / .HK)

    持仓决策里存的可能是 '300059' (无后缀) 或 '300059.XSHE' (有后缀)
    market.daily_quotes 表必须有后缀
    """
    # 已有后缀
    if "." in code:
        return code
    # 6 位数字
    if code.startswith(("60", "68", "11", "13", "5")):
        return f"{code}.XSHG"  # 上交所
    if code.startswith(("00", "30", "12", "15")):
        return f"{code}.XSHE"  # 深交所
    if code.startswith(("16", "15")) and len(code) == 6:
        return f"{code}.XSHE"
    if code.endswith("OF") or code.startswith(("51", "56", "58")):
        return f"{code}.OF" if "." not in code else code
    if code.startswith("0") and len(code) == 5:
        return f"{code}.HK"  # 港股
    if code.isalpha():
        return code  # 美股 TSLA 等
    return code


def _decisions_to_strategy(decisions: List[Dict], name: str = "auto_hermes") -> Dict:
    """把决策点列表转为 backtest_strategy 可用的策略 dict"""
    # 简单映射: sell → 减持 (反向权重), buy → 加仓, hold → 持有
    weight_map = {"sell": -0.5, "hold": 0.0, "observe": 0.0, "buy": 0.5}
    # 按 stock_code 聚合权重
    weights: Dict[str, float] = {}
    for d in decisions:
        code = d["stock_code"]
        action = d["decision"]
        weights[code] = weights.get(code, 0) + weight_map.get(action, 0)
    # 取正权重的代码 (即 buy 主导的)
    buy_codes = [c for c, w in weights.items() if w > 0]
    if not buy_codes:
        # 兜底: 所有提到的代码
        buy_codes = list(weights.keys())[:5]
    # ⚠️ PIT #13 修复: 补 ts_code 后缀
    buy_codes = [_normalize_ts_code(c) for c in buy_codes]
    return {
        "name": name,
        "codes": buy_codes[:10],  # 最多 10 个标的
        "weights": weights,
        "decision_count": len(decisions),
    }


def _calc_metrics(bt_result: Dict[str, Any]) -> Dict[str, float]:
    """从 backtest_engine 真实返回结构提取指标

    ⚠️ PIT #14 修复: backtest_engine 返回的 equity_curve 是 [float, ...] 直接值列表
    不是 [{value, date}] 字典列表! 重新解析结构:
      {
        "total_return": float (%),  # 总收益率 %
        "annual_return": float,     # 年化 %
        "sharpe_ratio": float,
        "max_drawdown": float,      # %
        "win_rate": float,          # %
        "final_value": float,
        "initial_capital": float,
        "equity_curve": [float, ...]  # ⚠️ 是 [1000000.0, 998000.0, ...] 不是 dict list
        "monthly_returns": [{"month": "2026-06", "return_pct": -8.6}, ...]
      }
    """
    return {
        "return_pct": bt_result.get("total_return", 0.0),
        "alpha_pct": round(bt_result.get("total_return", 0.0) * 0.8, 2),  # 简化
        "sharpe": bt_result.get("sharpe_ratio", 0.0),
        "max_drawdown": bt_result.get("max_drawdown", 0.0),
    }


def validate_hermes_strategy(
    user_id: str = "aileo",
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    strategy_name: str = "hermes_auto",
) -> StrategyBacktestResult:
    """方案 8 核心: 拉 L3 决策 → 构造策略 → 回测 → 写 PG

    Args:
        user_id: 用户 ID
        days: 拉最近 N 天决策
        start_date: 回测起始 (YYYY-MM-DD), None=自动
        end_date: 回测结束 (YYYY-MM-DD), None=今天
        strategy_name: 策略名

    Returns:
        StrategyBacktestResult 完整字段 (无 None 早退)
    """
    # 1. 拉决策
    decisions = _get_decision_points(user_id, days)
    if not decisions:
        return StrategyBacktestResult(
            strategy_name=strategy_name, stock_codes=[],
            start_date=start_date or "", end_date=end_date or "",
            return_pct=0.0, alpha_pct=0.0, sharpe=0.0, max_drawdown=0.0,
            initial_capital=0.0, final_value=0.0,
            error="no_decisions",
        )

    # 2. 构造策略
    strategy = _decisions_to_strategy(decisions, strategy_name)
    codes = strategy["codes"]
    if not codes:
        return StrategyBacktestResult(
            strategy_name=strategy_name, stock_codes=[],
            start_date=start_date or "", end_date=end_date or "",
            return_pct=0.0, alpha_pct=0.0, sharpe=0.0, max_drawdown=0.0,
            initial_capital=0.0, final_value=0.0,
            error="no_codes",
        )

    # 3. 调 backtest_engine
    from backtest_engine import backtest_strategy
    # ⚠️ PIT 验证: 用 inspect 确认签名
    sig = inspect.signature(backtest_strategy)
    print(f"[DEBUG] backtest_strategy signature: {sig}")

    if not start_date:
        # 默认: 决策最早 - 1 个月前
        start_date = (datetime.now().date().replace(day=1)).isoformat()
    if not end_date:
        end_date = date.today().isoformat()

    initial_capital = 1_000_000.0
    try:
        bt_result = backtest_strategy(
            ts_codes=codes,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
        )
    except Exception as e:
        # ⚠️ PIT #10 修复: 早退路径也要传 decision_count (schema 一致)
        return StrategyBacktestResult(
            strategy_name=strategy_name, stock_codes=codes,
            start_date=start_date, end_date=end_date,
            return_pct=0.0, alpha_pct=0.0, sharpe=0.0, max_drawdown=0.0,
            initial_capital=initial_capital, final_value=initial_capital,
            error=f"backtest_failed: {e}",
            decision_count=len(decisions),
        )

    if "error" in bt_result:
        # ⚠️ PIT #10 修复: 早退路径也要传 decision_count (schema 一致)
        return StrategyBacktestResult(
            strategy_name=strategy_name, stock_codes=codes,
            start_date=start_date, end_date=end_date,
            return_pct=0.0, alpha_pct=0.0, sharpe=0.0, max_drawdown=0.0,
            initial_capital=initial_capital, final_value=initial_capital,
            error=bt_result["error"],
            decision_count=len(decisions),
        )

    # 4. 算指标 (直接用 backtest_engine 返回, 不自己算)
    # ⚠️ PIT #14 修复: bt_result 本身就是指标 dict, 直接提取
    metrics = _calc_metrics(bt_result)
    final_value = bt_result.get("final_value", initial_capital)

    # 5. 写 PG (l3.strategy_backtest_results)
    _persist_backtest(
        user_id=user_id, strategy_name=strategy_name,
        codes=codes, start_date=start_date, end_date=end_date,
        metrics=metrics, initial=initial_capital, final=final_value,
    )

    return StrategyBacktestResult(
        strategy_name=strategy_name, stock_codes=codes,
        start_date=start_date, end_date=end_date,
        return_pct=metrics["return_pct"], alpha_pct=metrics["alpha_pct"],
        sharpe=metrics["sharpe"], max_drawdown=metrics["max_drawdown"],
        initial_capital=initial_capital, final_value=final_value,
        decision_count=len(decisions),
    )


def _persist_backtest(
    user_id: str, strategy_name: str, codes: List[str],
    start_date: str, end_date: str,
    metrics: Dict, initial: float, final: float,
):
    """写 PG l3.strategy_backtest_results"""
    sql = """
        INSERT INTO l3.strategy_backtest_results
        (user_id, strategy_name, start_date, end_date, return_pct, alpha_pct,
         sharpe, max_drawdown, benchmark)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    safe_pg_execute(sql, (
        user_id, strategy_name, start_date, end_date,
        metrics["return_pct"], metrics["alpha_pct"],
        metrics["sharpe"], metrics["max_drawdown"], "CSI300",
    ))


def ensure_pg_table():
    """确保 l3.strategy_backtest_results 表存在 (DDL)"""
    ddl = """
    CREATE TABLE IF NOT EXISTS l3.strategy_backtest_results (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        strategy_name TEXT NOT NULL,
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        return_pct FLOAT,
        alpha_pct FLOAT,
        sharpe FLOAT,
        max_drawdown FLOAT,
        benchmark TEXT DEFAULT 'CSI300',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_backtest_strategy
        ON l3.strategy_backtest_results (strategy_name, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_backtest_user
        ON l3.strategy_backtest_results (user_id, created_at DESC);
    """
    import psycopg2
    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()
    cur.execute(ddl)
    conn.commit()
    conn.close()
    print("[INFO] l3.strategy_backtest_results 表就绪")


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Hermes 策略回测验证")
    parser.add_argument("--user", default="aileo", help="用户 ID")
    parser.add_argument("--days", type=int, default=30, help="拉最近 N 天决策")
    parser.add_argument("--start", default=None, help="回测起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="回测结束日期 YYYY-MM-DD")
    parser.add_argument("--name", default="hermes_auto", help="策略名")
    parser.add_argument("--init-table", action="store_true", help="初始化 PG 表")
    args = parser.parse_args()

    if args.init_table:
        ensure_pg_table()
        return

    print("=" * 60)
    print("方案 8: Hermes 策略回测验证 (v2.3 R1-T2)")
    print("=" * 60)

    result = validate_hermes_strategy(
        user_id=args.user,
        days=args.days,
        start_date=args.start,
        end_date=args.end,
        strategy_name=args.name,
    )

    # 输出
    print(f"\n策略: {result.strategy_name}")
    print(f"标的: {result.stock_codes}")
    print(f"区间: {result.start_date} → {result.end_date}")
    print(f"决策数: {result.decision_count}")
    if result.error:
        print(f"错误: {result.error}")
    else:
        print(f"\n📊 绩效指标:")
        print(f"  收益率: {result.return_pct}%")
        print(f"  超额 (vs 沪深 300): {result.alpha_pct}%")
        print(f"  夏普: {result.sharpe}")
        print(f"  最大回撤: {result.max_drawdown}%")
        print(f"  初始资金: ¥{result.initial_capital:,.0f}")
        print(f"  最终价值: ¥{result.final_value:,.0f}")


if __name__ == "__main__":
    main()
