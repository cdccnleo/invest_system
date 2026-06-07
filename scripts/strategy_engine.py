"""
strategy_engine.py — 回测策略引擎（升级版）

提供:
  - BaseStrategy: 策略抽象基类
  - MACrossoverStrategy: 均线交叉策略
  - MomentumStrategy: 动量策略
  - MeanReversionStrategy: 均值回归策略
  - PerformanceEvaluator: 绩效评估
  - GridSearchOptimizer: 参数网格搜索优化
"""

import math
from abc import ABC, abstractmethod
from typing import Optional
import pandas as pd


# ============================================================================
# 策略抽象基类
# ============================================================================

class BaseStrategy(ABC):
    """
    策略抽象基类。
    所有策略必须实现 generate_signals 方法。
    """

    def __init__(self, params: Optional[dict] = None):
        self.params = params or {}
        self._default_params()

    def _default_params(self):
        """子类重写以设置默认参数"""

    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """
        生成交易信号。

        Args:
            data: 包含 close/high/low/volume 列的 DataFrame

        Returns:
            pd.Series: 交易信号，1=买入，-1=卖出，0=持有
        """

    @abstractmethod
    def name(self) -> str:
        """策略名称"""

    def get_param_grid(self) -> dict:
        """
        返回参数网格搜索空间。
        子类重写以提供参数优化空间。
        """
        return {}


# ============================================================================
# 均线交叉策略
# ============================================================================

class MACrossoverStrategy(BaseStrategy):
    """
    均线交叉策略。
    短期均线上穿长期均线 → 买入信号
    短期均线下穿长期均线 → 卖出信号
    """

    def _default_params(self):
        self.params.setdefault("short_window", 5)
        self.params.setdefault("long_window", 20)

    def name(self) -> str:
        return f"MA交叉({self.params['short_window']}/{self.params['long_window']})"

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        short = self.params["short_window"]
        long = self.params["long_window"]

        close = data["close"]
        ma_short = close.rolling(window=short).mean()
        ma_long = close.rolling(window=long).mean()

        signals = pd.Series(0, index=data.index)
        signals[ma_short > ma_long] = 1
        signals[ma_short < ma_long] = -1

        return signals

    def get_param_grid(self) -> dict:
        return {
            "short_window": [3, 5, 10, 15],
            "long_window": [20, 30, 50, 60],
        }


# ============================================================================
# 动量策略
# ============================================================================

class MomentumStrategy(BaseStrategy):
    """
    动量策略。
    基于过去 N 日涨跌幅排名，买入高动量标的，卖出低动量标的。
    """

    def _default_params(self):
        self.params.setdefault("lookback", 20)
        self.params.setdefault("top_pct", 0.3)

    def name(self) -> str:
        return f"动量({self.params['lookback']}日)"

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        lookback = self.params["lookback"]
        close = data["close"]

        momentum = close.pct_change(periods=lookback)
        signals = pd.Series(0, index=data.index)

        threshold = momentum.quantile(1 - self.params["top_pct"])
        signals[momentum > threshold] = 1
        signals[momentum < -threshold] = -1

        return signals

    def get_param_grid(self) -> dict:
        return {
            "lookback": [10, 20, 30, 60],
            "top_pct": [0.2, 0.3, 0.4],
        }


# ============================================================================
# 均值回归策略
# ============================================================================

class MeanReversionStrategy(BaseStrategy):
    """
    均值回归策略（基于布林带）。
    价格触及下轨 → 买入（预期回归均值）
    价格触及上轨 → 卖出（预期回归均值）
    """

    def _default_params(self):
        self.params.setdefault("period", 20)
        self.params.setdefault("num_std", 2.0)

    def name(self) -> str:
        return "均值回归(布林带)"

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        period = self.params["period"]
        num_std = self.params["num_std"]
        close = data["close"]

        middle = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        upper = middle + num_std * std
        lower = middle - num_std * std

        signals = pd.Series(0, index=data.index)
        signals[close < lower] = 1
        signals[close > upper] = -1

        return signals

    def get_param_grid(self) -> dict:
        return {
            "period": [10, 20, 30],
            "num_std": [1.5, 2.0, 2.5],
        }


# ============================================================================
# 绩效评估器
# ============================================================================

class PerformanceEvaluator:
    """
    策略绩效评估器。
    计算夏普比率、最大回撤、胜率、年化收益率、信息比率等指标。
    """

    @staticmethod
    def sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.03) -> float:
        """
        计算年化夏普比率。

        Args:
            returns: 日收益率序列
            risk_free_rate: 无风险利率（默认 3%）
        """
        if len(returns) < 2:
            return 0.0
        excess = returns - risk_free_rate / 252
        if excess.std() == 0:
            return 0.0
        return float(excess.mean() / excess.std() * math.sqrt(252))

    @staticmethod
    def max_drawdown(equity_curve: pd.Series) -> dict:
        """
        计算最大回撤。

        Returns:
            {"max_drawdown_pct": float, "peak_date": str, "trough_date": str, "duration_days": int}
        """
        if len(equity_curve) < 2:
            return {"max_drawdown_pct": 0.0, "peak_date": None, "trough_date": None, "duration_days": 0}  # noqa: E501

        running_max = equity_curve.cummax()
        drawdown = (equity_curve - running_max) / running_max
        max_dd = drawdown.min()

        trough_idx = drawdown.idxmin()
        peak_idx = running_max.loc[:trough_idx].idxmax()

        result = {
            "max_drawdown_pct": float(abs(max_dd) * 100),
            "peak_date": str(peak_idx),
            "trough_date": str(trough_idx),
            "duration_days": 0,
        }

        if hasattr(peak_idx, 'days') and hasattr(trough_idx, 'days'):
            result["duration_days"] = (trough_idx - peak_idx).days
        elif isinstance(peak_idx, int) and isinstance(trough_idx, int):
            result["duration_days"] = trough_idx - peak_idx

        return result

    @staticmethod
    def win_rate(trades: pd.DataFrame) -> float:
        """
        计算胜率。

        Args:
            trades: 包含 pnl 列的 DataFrame
        """
        if len(trades) == 0:
            return 0.0
        winning = (trades["pnl"] > 0).sum()
        return float(winning / len(trades))

    @staticmethod
    def annualized_return(equity_curve: pd.Series) -> float:
        """
        计算年化收益率。
        """
        if len(equity_curve) < 2:
            return 0.0
        total_return = (equity_curve.iloc[-1] / equity_curve.iloc[0]) - 1
        days = len(equity_curve)
        years = days / 252
        if years == 0:
            return 0.0
        return float((1 + total_return) ** (1 / years) - 1)

    @staticmethod
    def information_ratio(returns: pd.Series, benchmark_returns: pd.Series) -> float:
        """
        计算信息比率（相对于基准）。

        Args:
            returns: 策略日收益率
            benchmark_returns: 基准日收益率
        """
        if len(returns) < 2 or len(benchmark_returns) < 2:
            return 0.0
        active = returns - benchmark_returns
        if active.std() == 0:
            return 0.0
        return float(active.mean() / active.std() * math.sqrt(252))

    @staticmethod
    def full_report(
        equity_curve: pd.Series,
        returns: pd.Series,
        trades: Optional[pd.DataFrame] = None,
        benchmark_returns: Optional[pd.Series] = None,
    ) -> dict:
        """
        生成完整绩效报告。
        """
        report = {
            "annualized_return_pct": round(PerformanceEvaluator.annualized_return(equity_curve) * 100, 2),  # noqa: E501
            "sharpe_ratio": round(PerformanceEvaluator.sharpe_ratio(returns), 2),
            "max_drawdown": PerformanceEvaluator.max_drawdown(equity_curve),
            "volatility_pct": round(float(returns.std() * math.sqrt(252) * 100), 2),
        }

        if trades is not None and len(trades) > 0:
            report["win_rate_pct"] = round(PerformanceEvaluator.win_rate(trades) * 100, 2)
            report["total_trades"] = len(trades)

        if benchmark_returns is not None:
            report["information_ratio"] = round(
                PerformanceEvaluator.information_ratio(returns, benchmark_returns), 2
            )

        return report


# ============================================================================
# 参数网格搜索优化器
# ============================================================================

class GridSearchOptimizer:
    """
    网格搜索参数优化器。
    遍历所有参数组合，找到最优参数。
    """

    @staticmethod
    def _expand_grid(param_grid: dict) -> list[dict]:
        """
        展开参数网格为参数组合列表。
        """
        import itertools

        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(itertools.product(*values))
        return [dict(zip(keys, combo)) for combo in combinations]

    @staticmethod
    def optimize(
        strategy_class: type,
        param_grid: dict,
        data: pd.DataFrame,
        metric: str = "sharpe_ratio",
    ) -> dict:
        """
        网格搜索最优参数。

        Args:
            strategy_class: 策略类（BaseStrategy 子类）
            param_grid: 参数网格
            data: 回测数据
            metric: 优化目标（sharpe_ratio / annualized_return）

        Returns:
            {"best_params": dict, "best_score": float, "all_results": list[dict]}
        """
        combinations = GridSearchOptimizer._expand_grid(param_grid)
        results = []

        for params in combinations:
            strategy = strategy_class(params=params)
            signals = strategy.generate_signals(data)

            valid = signals[signals != 0].dropna()
            if len(valid) < 2:
                continue

            daily_returns = signals.shift(1) * data["close"].pct_change()
            daily_returns = daily_returns.dropna()

            equity = (1 + daily_returns).cumprod()

            if metric == "sharpe_ratio":
                score = PerformanceEvaluator.sharpe_ratio(daily_returns)
            elif metric == "annualized_return":
                score = PerformanceEvaluator.annualized_return(equity)
            else:
                score = PerformanceEvaluator.sharpe_ratio(daily_returns)

            results.append({
                "params": params,
                "score": score,
                "total_return_pct": round(float((equity.iloc[-1] - 1) * 100), 2),
            })

        if not results:
            return {"best_params": {}, "best_score": 0.0, "all_results": []}

        results.sort(key=lambda x: x["score"], reverse=True)
        return {
            "best_params": results[0]["params"],
            "best_score": results[0]["score"],
            "all_results": results,
        }


# ============================================================================
# 策略对比运行器
# ============================================================================

class StrategyRunner:
    """
    策略运行器：对多个策略在同一数据上运行并对比。
    """

    @staticmethod
    def compare(
        strategies: list[BaseStrategy],
        data: pd.DataFrame,
        initial_capital: float = 100000.0,
    ) -> pd.DataFrame:
        """
        对比多个策略的回测结果。

        Returns:
            DataFrame 包含各策略的绩效指标
        """
        results = []
        for strategy in strategies:
            signals = strategy.generate_signals(data)
            daily_returns = signals.shift(1) * data["close"].pct_change()
            daily_returns = daily_returns.dropna()
            equity = (1 + daily_returns).cumprod() * initial_capital

            report = PerformanceEvaluator.full_report(equity, daily_returns)
            report["strategy"] = strategy.name()
            report["total_return_pct"] = round(float((equity.iloc[-1] / initial_capital - 1) * 100), 2)  # noqa: E501
            results.append(report)

        return pd.DataFrame(results)