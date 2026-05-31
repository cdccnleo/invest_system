"""
test_strategy_engine.py — 策略引擎单元测试
覆盖: BaseStrategy, MACrossoverStrategy, MomentumStrategy, MeanReversionStrategy,
       PerformanceEvaluator, GridSearchOptimizer
"""

import math
import pytest
import pandas as pd
import numpy as np
from strategy_engine import (
    MACrossoverStrategy,
    MomentumStrategy,
    MeanReversionStrategy,
    PerformanceEvaluator,
    GridSearchOptimizer,
    StrategyRunner,
)


@pytest.fixture
def sample_data():
    """生成 200 个交易日的模拟行情数据"""
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=200, freq="B")
    close = 100.0 + np.cumsum(np.random.randn(200) * 1.5)
    close = np.maximum(close, 10.0)

    data = pd.DataFrame({
        "close": close,
        "high": close * 1.02,
        "low": close * 0.98,
        "volume": np.random.randint(10000, 100000, 200),
    }, index=dates)
    return data


@pytest.fixture
def uptrend_data():
    """生成上升趋势的模拟数据"""
    dates = pd.date_range("2025-01-01", periods=100, freq="B")
    close = 100.0 + np.arange(100) * 0.5
    data = pd.DataFrame({
        "close": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "volume": 50000,
    }, index=dates)
    return data


# ============================================================================
# MACrossoverStrategy 测试
# ============================================================================

class TestMACrossover:
    """均线交叉策略测试"""

    def test_default_params(self):
        """默认参数"""
        strategy = MACrossoverStrategy()
        assert strategy.params["short_window"] == 5
        assert strategy.params["long_window"] == 20

    def test_custom_params(self):
        """自定义参数"""
        strategy = MACrossoverStrategy(params={"short_window": 10, "long_window": 30})
        assert strategy.params["short_window"] == 10
        assert strategy.params["long_window"] == 30

    def test_generate_signals_output(self, sample_data):
        """信号输出格式"""
        strategy = MACrossoverStrategy()
        signals = strategy.generate_signals(sample_data)
        assert len(signals) == len(sample_data)
        assert set(signals.unique()).issubset({-1, 0, 1})

    def test_uptrend_buy_signal(self, uptrend_data):
        """上升趋势中短期均线应快于长期均线，产生买入信号"""
        strategy = MACrossoverStrategy(params={"short_window": 5, "long_window": 20})
        signals = strategy.generate_signals(uptrend_data)
        valid = signals.dropna()
        assert len(valid) > 0
        assert (valid.iloc[-10:] == 1).all(), \
            "上升趋势中后期应为买入信号"

    def test_name_format(self):
        """策略名称格式"""
        strategy = MACrossoverStrategy()
        assert "MA交叉" in strategy.name()
        assert "5" in strategy.name()
        assert "20" in strategy.name()

    def test_param_grid(self):
        """参数网格搜索空间"""
        strategy = MACrossoverStrategy()
        grid = strategy.get_param_grid()
        assert "short_window" in grid
        assert "long_window" in grid
        assert len(grid["short_window"]) == 4
        assert len(grid["long_window"]) == 4


# ============================================================================
# MomentumStrategy 测试
# ============================================================================

class TestMomentum:
    """动量策略测试"""

    def test_default_params(self):
        """默认参数"""
        strategy = MomentumStrategy()
        assert strategy.params["lookback"] == 20
        assert strategy.params["top_pct"] == 0.3

    def test_generate_signals_output(self, sample_data):
        """信号输出格式"""
        strategy = MomentumStrategy()
        signals = strategy.generate_signals(sample_data)
        assert len(signals) == len(sample_data)
        assert set(signals.dropna().unique()).issubset({-1, 0, 1})

    def test_name_format(self):
        """策略名称格式"""
        strategy = MomentumStrategy()
        assert "动量" in strategy.name()


# ============================================================================
# MeanReversionStrategy 测试
# ============================================================================

class TestMeanReversion:
    """均值回归策略测试"""

    def test_default_params(self):
        """默认参数"""
        strategy = MeanReversionStrategy()
        assert strategy.params["period"] == 20
        assert strategy.params["num_std"] == 2.0

    def test_generate_signals_output(self, sample_data):
        """信号输出格式"""
        strategy = MeanReversionStrategy()
        signals = strategy.generate_signals(sample_data)
        assert len(signals) == len(sample_data)
        assert set(signals.dropna().unique()).issubset({-1, 0, 1})

    def test_name_format(self):
        """策略名称格式"""
        strategy = MeanReversionStrategy()
        assert "均值回归" in strategy.name()


# ============================================================================
# PerformanceEvaluator 测试
# ============================================================================

class TestPerformanceEvaluator:
    """绩效评估器测试"""

    def test_sharpe_ratio_positive(self):
        """正收益应产生正夏普比率"""
        returns = pd.Series([0.001, 0.002, 0.0015, 0.003, 0.002])
        sr = PerformanceEvaluator.sharpe_ratio(returns, risk_free_rate=0.0)
        assert sr > 0

    def test_sharpe_ratio_negative(self):
        """负收益应产生负夏普比率"""
        returns = pd.Series([-0.002, -0.001, -0.003, -0.001, -0.002])
        sr = PerformanceEvaluator.sharpe_ratio(returns, risk_free_rate=0.0)
        assert sr < 0

    def test_sharpe_ratio_short_series(self):
        """短序列夏普比率"""
        returns = pd.Series([0.01])
        sr = PerformanceEvaluator.sharpe_ratio(returns)
        assert sr == 0.0

    def test_max_drawdown_normal(self):
        """最大回撤计算"""
        equity = pd.Series([100.0, 105.0, 103.0, 101.0, 107.0])
        result = PerformanceEvaluator.max_drawdown(equity)
        assert result["max_drawdown_pct"] > 0
        assert result["max_drawdown_pct"] < 10

    def test_max_drawdown_no_loss(self):
        """无亏损时最大回撤为 0"""
        equity = pd.Series([100.0, 101.0, 102.0, 103.0])
        result = PerformanceEvaluator.max_drawdown(equity)
        assert result["max_drawdown_pct"] == 0.0

    def test_win_rate(self):
        """胜率计算"""
        trades = pd.DataFrame({"pnl": [100, -50, 200, -30, 150]})
        wr = PerformanceEvaluator.win_rate(trades)
        assert wr == 0.6

    def test_win_rate_empty(self):
        """空交易列表胜率为 0"""
        trades = pd.DataFrame({"pnl": []})
        wr = PerformanceEvaluator.win_rate(trades)
        assert wr == 0.0

    def test_annualized_return(self):
        """年化收益率计算"""
        equity = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0])
        ar = PerformanceEvaluator.annualized_return(equity)
        assert ar > 0

    def test_information_ratio(self):
        """信息比率计算"""
        returns = pd.Series([0.001, 0.002, 0.001, 0.003, 0.002])
        benchmark = pd.Series([0.0005, 0.001, 0.0005, 0.001, 0.001])
        ir = PerformanceEvaluator.information_ratio(returns, benchmark)
        assert ir > 0

    def test_full_report(self):
        """完整报告生成"""
        equity = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0])
        returns = pd.Series([0.01, 0.0099, 0.0098, 0.0097])
        trades = pd.DataFrame({"pnl": [100, 200, -50]})
        report = PerformanceEvaluator.full_report(equity, returns, trades)
        assert "sharpe_ratio" in report
        assert "annualized_return_pct" in report
        assert "max_drawdown" in report
        assert "win_rate_pct" in report


# ============================================================================
# GridSearchOptimizer 测试
# ============================================================================

class TestGridSearchOptimizer:
    """参数优化器测试"""

    def test_expand_grid(self):
        """参数网格展开"""
        grid = {"a": [1, 2], "b": [3, 4]}
        combinations = GridSearchOptimizer._expand_grid(grid)
        assert len(combinations) == 4
        assert {"a": 1, "b": 3} in combinations
        assert {"a": 2, "b": 4} in combinations

    def test_optimize_macrossover(self, sample_data):
        """MACrossoverStrategy 参数优化"""
        strategy = MACrossoverStrategy()
        grid = strategy.get_param_grid()
        result = GridSearchOptimizer.optimize(
            MACrossoverStrategy, grid, sample_data
        )
        assert "best_params" in result
        assert "best_score" in result
        assert "all_results" in result
        assert len(result["all_results"]) > 0

    def test_optimize_empty_grid(self, sample_data):
        """空参数网格返回默认参数结果"""
        result = GridSearchOptimizer.optimize(
            MACrossoverStrategy, {}, sample_data
        )
        assert result["best_params"] == {}


# ============================================================================
# StrategyRunner 测试
# ============================================================================

class TestStrategyRunner:
    """策略对比运行器测试"""

    def test_compare_multiple_strategies(self, sample_data):
        """多策略对比"""
        strategies = [
            MACrossoverStrategy(),
            MomentumStrategy(),
            MeanReversionStrategy(),
        ]
        result = StrategyRunner.compare(strategies, sample_data)
        assert len(result) == 3
        assert "strategy" in result.columns
        assert "sharpe_ratio" in result.columns
        assert "total_return_pct" in result.columns