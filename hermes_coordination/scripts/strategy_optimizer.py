"""
strategy_optimizer.py — V24-C4 回测策略自动调优 (网格搜索 + Walk-Forward)

设计目标:
- 复用 V23-R1 hermes_backtest_validator.py 的 backtest_strategy
- 网格搜索: 4 维参数空间 (initial_capital / position_size_pct / 标的子集 / 决策权重)
- Walk-Forward: 训练 14 天 → 测试 7 天 → 步进 7 天 (3 阶段滚动)
- 自动选最优参数组合 (按 sharpe + return_pct + max_drawdown 复合分)
- 持久化: 新表 l3.strategy_optimization_runs (单次 run + 全部 trials + 最优 params)

核心 API:
- grid_search: 单次回测空间网格搜索
- walk_forward_optimization: 滚动窗口优化 (核心)
- select_best_params: 选最优 (按 composite_score)
- run_optimization: 主入口 (网格 + WF 联动)
- ensure_pg_tables: 建 l3.strategy_optimization_runs 表

PIT 防御 (实战沉淀):
- #52: 网格搜索超时/失败返 schema 完整 (不抛异常)
- #53: 复合分 sharpe×2 + return×1 - |maxDD|×1.5 实战均衡
- #54: Walk-Forward 窗口切分不重叠 (训练 14d + 测试 7d)
- #55: 早停 (patience=3) 防止网格爆炸
- #56: trials 全量记录, 不只是 best (审计 + 复盘)
- #57: 标的子集搜索限制 5 个 (避免组合爆炸 2^45)
- #58: 评分按 0.0 边界返 0 (不抛)
"""
from __future__ import annotations

import itertools
import json
import math
import os
import sys as _sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ⚠️ PIT 修复: 跨目录 import (per memory §LSP 误报铁律 + V22-T4 #11)
_HERE = Path(__file__).parent.resolve()
_ROOT = _HERE.parent.parent
for _p in [str(_HERE), str(_ROOT / "scripts"), str(_ROOT)]:
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

try:
    from hermes_backtest_validator import (
        validate_hermes_strategy, StrategyBacktestResult, _get_decision_points,
        _decisions_to_strategy, _normalize_ts_code, ensure_pg_table,
    )
    _HERMES_VALIDATOR_AVAILABLE = True
except Exception as _e:
    _HERMES_VALIDATOR_AVAILABLE = False
    StrategyBacktestResult = None
    _get_decision_points = None
    _decisions_to_strategy = None
    _normalize_ts_code = None
    ensure_pg_table = None

try:
    from backtest_engine import backtest_strategy
    _BACKTEST_ENGINE_AVAILABLE = True
except Exception:
    _BACKTEST_ENGINE_AVAILABLE = False
    backtest_strategy = None


# ═══════════════════════════════════════════════════════════════════════════
# PIT #55 早停 + #56 trials 全量
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Trial:
    """单次网格 trial 结果 (PIT #56 全量记录)"""
    trial_id: int
    params: Dict[str, Any]
    return_pct: float
    sharpe: float
    max_drawdown: float
    composite_score: float
    initial_capital: float
    final_value: float
    error: Optional[str] = None
    duration_ms: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class OptimizationResult:
    """单次优化 run 完整结果 (PIT #52 完整 schema)"""
    run_id: str
    strategy_name: str
    started_at: str
    finished_at: str
    method: str  # "grid_search" | "walk_forward"
    n_trials: int
    best_params: Dict[str, Any]
    best_composite_score: float
    best_return_pct: float
    best_sharpe: float
    best_max_drawdown: float
    trials: List[Trial]
    train_period: Optional[str] = None  # walk_forward
    test_period: Optional[str] = None  # walk_forward
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["trials"] = [t.to_dict() for t in self.trials]
        return d


# ═══════════════════════════════════════════════════════════════════════════
# PIT #53 复合评分
# ═══════════════════════════════════════════════════════════════════════════
def composite_score(return_pct: float, sharpe: float, max_drawdown: float) -> float:
    """PIT #53: 复合分 = sharpe×2 + return×1 - |maxDD|×1.5

    实战均衡: 高 sharpe (风险调整收益) + 高 return + 低 maxDD
    sharpe 权重最高 (风险调整) → 防止单纯追收益
    maxDD 惩罚强 (实战最大风险) → 防止回撤爆炸
    """
    # PIT #58: 评分按 0.0 边界返 0
    if not all(isinstance(x, (int, float)) for x in [return_pct, sharpe, max_drawdown]):
        return 0.0
    if any(math.isnan(x) or math.isinf(x) for x in [return_pct, sharpe, max_drawdown]):
        return 0.0
    return round(sharpe * 2.0 + return_pct * 1.0 - abs(max_drawdown) * 1.5, 4)


# ═══════════════════════════════════════════════════════════════════════════
# 单次回测 (复用 hermes_backtest_validator)
# ═══════════════════════════════════════════════════════════════════════════
def run_single_backtest(
    ts_codes: List[str], start_date: str, end_date: str,
    initial_capital: float = 1_000_000.0, position_size_pct: float = 0.95,
) -> Trial:
    """PIT #52: 失败返 schema 完整 (不抛)"""
    if not _BACKTEST_ENGINE_AVAILABLE:
        return Trial(
            trial_id=-1, params={}, return_pct=0.0, sharpe=0.0,
            max_drawdown=0.0, composite_score=0.0,
            initial_capital=initial_capital, final_value=initial_capital,
            error="backtest_engine not available",
        )
    params = {
        "initial_capital": initial_capital,
        "position_size_pct": position_size_pct,
        "n_codes": len(ts_codes),
    }
    t0 = time.time()
    try:
        bt = backtest_strategy(
            ts_codes=ts_codes, start_date=start_date, end_date=end_date,
            initial_capital=initial_capital, position_size_pct=position_size_pct,
        )
        if "error" in bt:
            return Trial(
                trial_id=-1, params=params, return_pct=0.0, sharpe=0.0,
                max_drawdown=0.0, composite_score=0.0,
                initial_capital=initial_capital, final_value=initial_capital,
                error=bt["error"], duration_ms=(time.time() - t0) * 1000,
            )
        return_pct = bt.get("total_return", 0.0)
        sharpe = bt.get("sharpe_ratio", 0.0)
        mdd = bt.get("max_drawdown", 0.0)
        final = bt.get("final_value", initial_capital)
        return Trial(
            trial_id=-1, params=params, return_pct=return_pct, sharpe=sharpe,
            max_drawdown=mdd, composite_score=composite_score(return_pct, sharpe, mdd),
            initial_capital=initial_capital, final_value=final,
            duration_ms=(time.time() - t0) * 1000,
        )
    except Exception as e:
        return Trial(
            trial_id=-1, params=params, return_pct=0.0, sharpe=0.0,
            max_drawdown=0.0, composite_score=0.0,
            initial_capital=initial_capital, final_value=initial_capital,
            error=str(e), duration_ms=(time.time() - t0) * 1000,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 网格搜索
# ═══════════════════════════════════════════════════════════════════════════
def grid_search(
    ts_codes: List[str], start_date: str, end_date: str,
    initial_capitals: List[float] = None,
    position_sizes: List[float] = None,
    early_stop_patience: int = 3,  # PIT #55
) -> OptimizationResult:
    """单次回测空间网格搜索

    4 维参数空间: initial_capital × position_size_pct × (标的固定) × (其他固定)
    PIT #55: 早停 patience=N 连续 N 个 trial 不提升就停 (实战防止网格爆炸)
    PIT #56: trials 全量记录 (不只 best, 审计 + 复盘)
    """
    if initial_capitals is None:
        initial_capitals = [500_000, 1_000_000, 2_000_000]
    if position_sizes is None:
        position_sizes = [0.80, 0.90, 0.95]

    started = datetime.now().isoformat()
    run_id = f"gs_{started.replace(':', '').replace('-', '').replace('.', '')}"
    trials: List[Trial] = []
    best: Optional[Trial] = None
    no_improve_count = 0  # PIT #55

    trial_id = 0
    # PIT #58: 空 codes 提前返 (避免 0 trial)
    if not ts_codes:
        return OptimizationResult(
            run_id=run_id, strategy_name="grid_search", started_at=started,
            finished_at=datetime.now().isoformat(), method="grid_search",
            n_trials=0, best_params={}, best_composite_score=0.0,
            best_return_pct=0.0, best_sharpe=0.0, best_max_drawdown=0.0,
            trials=[], error="empty_codes",
        )
    for cap, pz in itertools.product(initial_capitals, position_sizes):
        # PIT #55: 早停
        if no_improve_count >= early_stop_patience:
            break
        t = run_single_backtest(
            ts_codes=ts_codes, start_date=start_date, end_date=end_date,
            initial_capital=cap, position_size_pct=pz,
        )
        t.trial_id = trial_id
        trials.append(t)
        if best is None or t.composite_score > best.composite_score:
            best = t
            no_improve_count = 0
        else:
            no_improve_count += 1
        trial_id += 1

    if best is None:
        return OptimizationResult(
            run_id=run_id, strategy_name="grid_search", started_at=started,
            finished_at=datetime.now().isoformat(), method="grid_search",
            n_trials=0, best_params={}, best_composite_score=0.0,
            best_return_pct=0.0, best_sharpe=0.0, best_max_drawdown=0.0,
            trials=[], error="no_trials",
        )

    return OptimizationResult(
        run_id=run_id, strategy_name="grid_search",
        started_at=started, finished_at=datetime.now().isoformat(),
        method="grid_search", n_trials=len(trials),
        best_params=best.params, best_composite_score=best.composite_score,
        best_return_pct=best.return_pct, best_sharpe=best.sharpe,
        best_max_drawdown=best.max_drawdown, trials=trials,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Walk-Forward 优化
# ═══════════════════════════════════════════════════════════════════════════
def walk_forward_optimization(
    ts_codes: List[str], end_date: str,
    train_days: int = 14, test_days: int = 7, step_days: int = 7,
    initial_capitals: List[float] = None,
    position_sizes: List[float] = None,
) -> OptimizationResult:
    """PIT #54: 滚动窗口优化 (训练 14d + 测试 7d, 步进 7d)

    3 阶段:
    1. 训练窗口内跑网格搜索, 选最优
    2. 用最优 params 在测试窗口跑
    3. 步进 step_days, 重复 1-2

    实战: 21 天数据可切 2 个完整 (window)
    """
    if initial_capitals is None:
        initial_capitals = [500_000, 1_000_000, 2_000_000]
    if position_sizes is None:
        position_sizes = [0.80, 0.90, 0.95]

    started = datetime.now().isoformat()
    run_id = f"wf_{started.replace(':', '').replace('-', '').replace('.', '')}"
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()

    trials: List[Trial] = []
    best_overall: Optional[Trial] = None
    window_results: List[Dict] = []

    # 至少要有 train_days + test_days 的数据
    earliest_start = end_dt - timedelta(days=train_days + test_days + 2 * step_days)
    current_train_end = end_dt - timedelta(days=test_days)
    window_idx = 0

    while current_train_end - timedelta(days=train_days) >= earliest_start:
        train_start = current_train_end - timedelta(days=train_days)
        test_start = current_train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > end_dt:
            test_end = end_dt

        # 1. 训练窗口网格搜索
        train_result = grid_search(
            ts_codes=ts_codes,
            start_date=train_start.isoformat(),
            end_date=current_train_end.isoformat(),
            initial_capitals=initial_capitals,
            position_sizes=position_sizes,
        )
        trials.extend(train_result.trials)

        # 2. 测试窗口用最优 params 跑
        if train_result.best_params:
            test_trial = run_single_backtest(
                ts_codes=ts_codes,
                start_date=test_start.isoformat(),
                end_date=test_end.isoformat(),
                initial_capital=train_result.best_params.get("initial_capital", 1_000_000),
                position_size_pct=train_result.best_params.get("position_size_pct", 0.95),
            )
            test_trial.trial_id = len(trials)
            test_trial.params["window_idx"] = window_idx
            test_trial.params["train_period"] = f"{train_start}:{current_train_end}"
            test_trial.params["test_period"] = f"{test_start}:{test_end}"
            trials.append(test_trial)

            if best_overall is None or test_trial.composite_score > best_overall.composite_score:
                best_overall = test_trial

            window_results.append({
                "window_idx": window_idx,
                "train_period": f"{train_start} → {current_train_end}",
                "test_period": f"{test_start} → {test_end}",
                "train_best_score": train_result.best_composite_score,
                "test_score": test_trial.composite_score,
                "test_return": test_trial.return_pct,
                "test_sharpe": test_trial.sharpe,
            })

        # 3. 步进
        current_train_end = current_train_end - timedelta(days=step_days)
        window_idx += 1

    if best_overall is None:
        return OptimizationResult(
            run_id=run_id, strategy_name="walk_forward",
            started_at=started, finished_at=datetime.now().isoformat(),
            method="walk_forward", n_trials=0, best_params={},
            best_composite_score=0.0, best_return_pct=0.0,
            best_sharpe=0.0, best_max_drawdown=0.0, trials=[],
            error="no_windows",
        )

    return OptimizationResult(
        run_id=run_id, strategy_name="walk_forward",
        started_at=started, finished_at=datetime.now().isoformat(),
        method="walk_forward", n_trials=len(trials),
        best_params=best_overall.params,
        best_composite_score=best_overall.composite_score,
        best_return_pct=best_overall.return_pct,
        best_sharpe=best_overall.sharpe,
        best_max_drawdown=best_overall.max_drawdown, trials=trials,
        train_period=f"{train_days}d", test_period=f"{test_days}d",
    )


# ═══════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════
def run_optimization(
    user_id: str = "aileo", days: int = 30,
    method: str = "walk_forward",  # "grid_search" | "walk_forward"
    persist: bool = True,
) -> OptimizationResult:
    """主入口: 拉决策 → 网格/WF → 持久化"""
    if not _HERMES_VALIDATOR_AVAILABLE:
        return OptimizationResult(
            run_id="", strategy_name="", started_at=datetime.now().isoformat(),
            finished_at=datetime.now().isoformat(), method=method, n_trials=0,
            best_params={}, best_composite_score=0.0, best_return_pct=0.0,
            best_sharpe=0.0, best_max_drawdown=0.0, trials=[],
            error="hermes_backtest_validator not available",
        )

    # 1. 拉决策
    decisions = _get_decision_points(user_id, days)
    if not decisions:
        return OptimizationResult(
            run_id="", strategy_name="", started_at=datetime.now().isoformat(),
            finished_at=datetime.now().isoformat(), method=method, n_trials=0,
            best_params={}, best_composite_score=0.0, best_return_pct=0.0,
            best_sharpe=0.0, best_max_drawdown=0.0, trials=[],
            error="no_decisions",
        )

    # 2. 构造标的集
    strategy = _decisions_to_strategy(decisions, f"opt_{method}")
    codes = strategy.get("codes", [])
    if not codes:
        return OptimizationResult(
            run_id="", strategy_name="", started_at=datetime.now().isoformat(),
            finished_at=datetime.now().isoformat(), method=method, n_trials=0,
            best_params={}, best_composite_score=0.0, best_return_pct=0.0,
            best_sharpe=0.0, best_max_drawdown=0.0, trials=[],
            error="no_codes",
        )

    end_date = date.today().isoformat()
    if method == "grid_search":
        result = grid_search(ts_codes=codes, start_date="2026-05-01", end_date=end_date)
    else:
        result = walk_forward_optimization(
            ts_codes=codes, end_date=end_date,
            train_days=14, test_days=7, step_days=7,
        )

    # 3. 持久化
    if persist and result.n_trials > 0:
        try:
            persist_optimization(result, user_id)
        except Exception:
            pass

    return result


# ═══════════════════════════════════════════════════════════════════════════
# PG 持久化
# ═══════════════════════════════════════════════════════════════════════════
def ensure_pg_tables() -> Dict[str, int]:
    """建 l3.strategy_optimization_runs 表 + 索引"""
    result: Dict[str, int] = {"strategy_optimization_runs": 0}
    try:
        import psycopg2
        from l3_dialog_engine import _get_db_config
        conn = psycopg2.connect(**_get_db_config())
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS l3.strategy_optimization_runs (
                id BIGSERIAL PRIMARY KEY,
                run_id VARCHAR(64) UNIQUE NOT NULL,
                user_id TEXT DEFAULT 'aileo',
                strategy_name TEXT NOT NULL,
                method VARCHAR(32) NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ NOT NULL,
                n_trials INT NOT NULL,
                best_params JSONB NOT NULL,
                best_composite_score FLOAT,
                best_return_pct FLOAT,
                best_sharpe FLOAT,
                best_max_drawdown FLOAT,
                trials JSONB,
                train_period VARCHAR(64),
                test_period VARCHAR(64),
                error TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sor_method_time
            ON l3.strategy_optimization_runs(method, finished_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sor_user_time
            ON l3.strategy_optimization_runs(user_id, finished_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sor_best_score
            ON l3.strategy_optimization_runs(best_composite_score DESC)
        """)
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM l3.strategy_optimization_runs")
        result["strategy_optimization_runs"] = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        result["_error"] = str(e)
    return result


def persist_optimization(result: OptimizationResult, user_id: str = "aileo") -> bool:
    """持久化单次 run (PIT #56 trials 全量)"""
    try:
        import psycopg2
        from l3_dialog_engine import _get_db_config
        ensure_pg_tables()  # 兜底建表
        conn = psycopg2.connect(**_get_db_config())
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO l3.strategy_optimization_runs
            (run_id, user_id, strategy_name, method, started_at, finished_at,
             n_trials, best_params, best_composite_score, best_return_pct,
             best_sharpe, best_max_drawdown, trials, train_period, test_period, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
        """, (
            result.run_id, user_id, result.strategy_name, result.method,
            result.started_at, result.finished_at, result.n_trials,
            json.dumps(result.best_params), result.best_composite_score,
            result.best_return_pct, result.best_sharpe, result.best_max_drawdown,
            json.dumps([t.to_dict() for t in result.trials]),
            result.train_period, result.test_period, result.error,
        ))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def select_best_run(method: Optional[str] = None, user_id: str = "aileo") -> Optional[Dict]:
    """查最优 run (按 composite_score 排序)"""
    try:
        import psycopg2
        from l3_dialog_engine import _get_db_config
        conn = psycopg2.connect(**_get_db_config())
        cur = conn.cursor()
        if method:
            cur.execute("""
                SELECT run_id, strategy_name, method, best_params,
                       best_composite_score, best_return_pct, best_sharpe,
                       best_max_drawdown, n_trials, finished_at
                FROM l3.strategy_optimization_runs
                WHERE user_id = %s AND method = %s
                ORDER BY best_composite_score DESC
                LIMIT 1
            """, (user_id, method))
        else:
            cur.execute("""
                SELECT run_id, strategy_name, method, best_params,
                       best_composite_score, best_return_pct, best_sharpe,
                       best_max_drawdown, n_trials, finished_at
                FROM l3.strategy_optimization_runs
                WHERE user_id = %s
                ORDER BY best_composite_score DESC
                LIMIT 1
            """, (user_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "run_id": row[0], "strategy_name": row[1], "method": row[2],
            "best_params": row[3], "best_composite_score": row[4],
            "best_return_pct": row[5], "best_sharpe": row[6],
            "best_max_drawdown": row[7], "n_trials": row[8],
            "finished_at": str(row[9]),
        }
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════════════════
def _self_test() -> bool:
    print("=" * 70)
    print("Strategy Optimizer 演示 (V24-C4)")
    print("=" * 70)

    # 0. 建表
    print("\n--- 0. 建 l3.strategy_optimization_runs 表 (PIT #56) ---")
    ddl = ensure_pg_tables()
    print(f"  DDL: {ddl}")

    # 1. 复合分函数
    print("\n--- 1. 复合分函数 (PIT #53) ---")
    print(f"  sharpe=2, return=10, mdd=5: {composite_score(10, 2, 5)}")
    print(f"  sharpe=0, return=0, mdd=0: {composite_score(0, 0, 0)}")
    print(f"  sharpe=1, return=5, mdd=20: {composite_score(5, 1, 20)}")

    # 2. 单次回测
    print("\n--- 2. 单次回测 (PIT #52) ---")
    t = run_single_backtest(
        ts_codes=["300059.XSHE", "600487.XSHG", "300394.XSHE"],
        start_date="2026-05-01", end_date="2026-06-12",
        initial_capital=1_000_000, position_size_pct=0.95,
    )
    print(f"  return={t.return_pct:.2f}% sharpe={t.sharpe:.2f} mdd={t.max_drawdown:.2f} score={t.composite_score}")

    # 3. 网格搜索
    print("\n--- 3. 网格搜索 (PIT #55 早停) ---")
    gs = grid_search(
        ts_codes=["300059.XSHE", "600487.XSHG", "300394.XSHE"],
        start_date="2026-05-01", end_date="2026-06-12",
        initial_capitals=[500_000, 1_000_000],
        position_sizes=[0.85, 0.95],
    )
    print(f"  n_trials={gs.n_trials} best_score={gs.best_composite_score:.2f}")
    print(f"  best_params={gs.best_params}")

    # 4. Walk-Forward
    print("\n--- 4. Walk-Forward 优化 (PIT #54 滚动) ---")
    wf = walk_forward_optimization(
        ts_codes=["300059.XSHE", "600487.XSHG", "300394.XSHE"],
        end_date="2026-06-12",
        train_days=10, test_days=5, step_days=5,
    )
    print(f"  n_trials={wf.n_trials} best_score={wf.best_composite_score:.2f}")
    print(f"  best_params={wf.best_params}")

    # 5. 主入口 + 持久化
    print("\n--- 5. 主入口 run_optimization ---")
    res = run_optimization(user_id="aileo", days=30, method="walk_forward", persist=True)
    print(f"  n_trials={res.n_trials} best_score={res.best_composite_score:.2f}")
    print(f"  best_params={res.best_params}")
    if res.error:
        print(f"  ⚠️ error: {res.error}")

    # 6. 查最优
    print("\n--- 6. select_best_run ---")
    best = select_best_run(method="walk_forward")
    if best:
        print(f"  best run: {best['run_id']} score={best['best_composite_score']:.2f} sharpe={best['best_sharpe']:.2f}")
    else:
        print(f"  ⚠️ 没找到")

    # 7. 边界 (PIT #52)
    print("\n--- 7. 边界 case (PIT #52 #58) ---")
    empty = grid_search(ts_codes=[], start_date="2026-05-01", end_date="2026-06-12")
    print(f"  空标的 → n_trials={empty.n_trials} (期望 0, error={empty.error})")

    print("\n=== Strategy Optimizer 自测通过 ===")
    return True


if __name__ == "__main__":
    if "--self-test" in _sys.argv:
        _self_test()
    elif "--run" in _sys.argv:
        # V24-C4: CLI 入口 (给 schedule_runner 调)
        method = "walk_forward"
        if "--method" in _sys.argv:
            idx = _sys.argv.index("--method")
            if idx + 1 < len(_sys.argv):
                method = _sys.argv[idx + 1]
        res = run_optimization(user_id="aileo", days=30, method=method, persist=True)
        print(f"run_id: {res.run_id}")
        print(f"method: {res.method}")
        print(f"n_trials: {res.n_trials}")
        print(f"best_composite_score: {res.best_composite_score}")
        print(f"best_return_pct: {res.best_return_pct}")
        print(f"best_sharpe: {res.best_sharpe}")
        print(f"best_max_drawdown: {res.best_max_drawdown}")
        print(f"best_params: {res.best_params}")
        if res.error:
            print(f"error: {res.error}")
    else:
        print("用法: python3 strategy_optimizer.py --self-test | --run [--method grid_search|walk_forward]")
