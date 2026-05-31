"""
test_backtest_engine.py — 回测引擎与技术指标单元测试
覆盖: TechnicalIndicators, StressTestEngine, 绩效计算函数
"""

import math
import pytest
from backtest_engine import TechnicalIndicators, StressTestEngine, _calc_monthly_returns


# ============================================================================
# TechnicalIndicators.calc_bollinger_bands 测试
# ============================================================================

class TestBollingerBands:
    """布林带指标计算测试"""

    def test_basic_calculation(self, sample_uptrend_prices):
        """基本布林带计算：验证输出结构和数值合理性"""
        result = TechnicalIndicators.calc_bollinger_bands(
            sample_uptrend_prices, period=20
        )
        assert len(result) == len(sample_uptrend_prices)

        for i, entry in enumerate(result):
            if i < 19:
                assert entry["upper"] is None
                assert entry["middle"] is None
                assert entry["lower"] is None
                assert entry["signal"] == "insufficient_data"
            else:
                assert entry["upper"] is not None
                assert entry["middle"] is not None
                assert entry["lower"] is not None
                assert entry["upper"] >= entry["middle"] >= entry["lower"]

    def test_position_range(self, sample_uptrend_prices):
        """布林带 position 应在合理范围内"""
        result = TechnicalIndicators.calc_bollinger_bands(
            sample_uptrend_prices, period=20
        )
        for entry in result[20:]:
            assert 0.0 <= entry["position"] <= 1.0, \
                f"position={entry['position']} 超出 [0,1] 范围"

    def test_upper_breakout_signal(self):
        """突破上轨应产生 upper_breakout 信号"""
        values = [10.0] * 19 + [20.0]
        result = TechnicalIndicators.calc_bollinger_bands(values, period=20)
        assert result[-1]["signal"] == "upper_breakout"

    def test_lower_breakout_signal(self):
        """突破下轨应产生 lower_breakout 信号"""
        values = [10.0] * 19 + [5.0]
        result = TechnicalIndicators.calc_bollinger_bands(values, period=20)
        assert result[-1]["signal"] == "lower_breakout"

    def test_custom_period(self):
        """自定义周期参数"""
        prices = [10.0 + i * 0.1 for i in range(25)]
        result = TechnicalIndicators.calc_bollinger_bands(prices, period=10)
        for i in range(9):
            assert result[i]["signal"] == "insufficient_data"
        for i in range(9, 25):
            assert result[i]["upper"] is not None

    def test_sideways_market(self, sample_sideways_prices):
        """横盘市场中布林带宽度应较小"""
        result = TechnicalIndicators.calc_bollinger_bands(
            sample_sideways_prices, period=20
        )
        valid = [r for r in result[20:] if r["bandwidth"] is not None]
        if valid:
            avg_bandwidth = sum(r["bandwidth"] for r in valid) / len(valid)
            assert avg_bandwidth < 0.5, f"横盘市场带宽应较小: {avg_bandwidth}"


# ============================================================================
# TechnicalIndicators.calc_cci 测试
# ============================================================================

class TestCCI:
    """CCI 顺势指标计算测试"""

    def test_basic_calculation(self, sample_ohlc_data):
        """基本 CCI 计算：验证输出结构"""
        closes, highs, lows = sample_ohlc_data
        result = TechnicalIndicators.calc_cci(closes, highs, lows, period=14)
        assert len(result) == len(closes)

        for i, entry in enumerate(result):
            if i < 13:
                assert entry["cci"] is None
                assert entry["signal"] == "insufficient_data"
            else:
                assert entry["cci"] is not None

    def test_overbought_signal(self):
        """大幅上涨应产生超买信号"""
        closes = [10.0 + i * 0.5 for i in range(30)]
        highs = [c + 0.8 for c in closes]
        lows = [c - 0.2 for c in closes]
        result = TechnicalIndicators.calc_cci(closes, highs, lows, period=14)
        last = result[-1]
        assert last["cci"] is not None
        assert last["cci"] > 0, f"上涨趋势 CCI 应为正: {last['cci']}"

    def test_oversold_signal(self):
        """大幅下跌应产生超卖信号"""
        closes = [20.0 - i * 0.5 for i in range(30)]
        highs = [c + 0.2 for c in closes]
        lows = [c - 0.8 for c in closes]
        result = TechnicalIndicators.calc_cci(closes, highs, lows, period=14)
        last = result[-1]
        assert last["cci"] is not None
        assert last["cci"] < 0, f"下跌趋势 CCI 应为负: {last['cci']}"

    def test_flat_prices_zero_cci(self):
        """价格完全不变时 CCI 应接近 0"""
        closes = [10.0] * 30
        highs = [10.0] * 30
        lows = [10.0] * 30
        result = TechnicalIndicators.calc_cci(closes, highs, lows, period=14)
        last = result[-1]
        assert last["cci"] == 0.0


# ============================================================================
# TechnicalIndicators.calc_rsi 测试
# ============================================================================

class TestRSI:
    """RSI 相对强弱指标计算测试"""

    def test_basic_calculation(self, sample_uptrend_prices):
        """基本 RSI 计算：验证输出结构和范围"""
        result = TechnicalIndicators.calc_rsi(sample_uptrend_prices, period=14)
        assert len(result) == len(sample_uptrend_prices)

        for i, entry in enumerate(result):
            if i < 14:
                assert entry["rsi"] is None
            else:
                assert 0.0 <= entry["rsi"] <= 100.0, \
                    f"RSI={entry['rsi']} 超出 [0,100] 范围"

    def test_uptrend_high_rsi(self, sample_uptrend_prices):
        """上升趋势中 RSI 应偏高"""
        result = TechnicalIndicators.calc_rsi(sample_uptrend_prices, period=14)
        valid = [r["rsi"] for r in result[14:] if r["rsi"] is not None]
        avg_rsi = sum(valid) / len(valid)
        assert avg_rsi > 50, f"上升趋势 RSI 应 > 50: {avg_rsi}"

    def test_downtrend_low_rsi(self, sample_downtrend_prices):
        """下降趋势中 RSI 应偏低"""
        result = TechnicalIndicators.calc_rsi(sample_downtrend_prices, period=14)
        valid = [r["rsi"] for r in result[14:] if r["rsi"] is not None]
        avg_rsi = sum(valid) / len(valid)
        assert avg_rsi < 50, f"下降趋势 RSI 应 < 50: {avg_rsi}"

    def test_all_gains_rsi_100(self):
        """连续上涨时 RSI 应接近 100"""
        prices = [10.0 + i * 0.5 for i in range(30)]
        result = TechnicalIndicators.calc_rsi(prices, period=14)
        last_rsi = result[-1]["rsi"]
        assert last_rsi > 90, f"连续上涨 RSI 应 > 90: {last_rsi}"

    def test_all_losses_rsi_low(self):
        """连续下跌时 RSI 应接近 0"""
        prices = [30.0 - i * 0.5 for i in range(30)]
        result = TechnicalIndicators.calc_rsi(prices, period=14)
        last_rsi = result[-1]["rsi"]
        assert last_rsi < 10, f"连续下跌 RSI 应 < 10: {last_rsi}"

    def test_short_sequence(self):
        """短序列应返回 insufficient_data"""
        result = TechnicalIndicators.calc_rsi([10.0, 10.5, 10.3], period=14)
        assert len(result) == 3
        for entry in result:
            assert entry["signal"] == "insufficient_data"


# ============================================================================
# TechnicalIndicators.analyze_signals 测试
# ============================================================================

class TestAnalyzeSignals:
    """综合信号分析测试"""

    @pytest.fixture
    def bullish_bollinger(self):
        return [{"upper": 11.0, "lower": 9.0, "close": 9.1, "signal": "near_lower"}]

    @pytest.fixture
    def bearish_bollinger(self):
        return [{"upper": 11.0, "lower": 9.0, "close": 10.9, "signal": "near_upper"}]

    @pytest.fixture
    def bullish_cci(self):
        return [{"cci": -120, "signal": "oversold"}]

    @pytest.fixture
    def bearish_cci(self):
        return [{"cci": 120, "signal": "overbought"}]

    @pytest.fixture
    def bullish_rsi(self):
        return [{"rsi": 25, "signal": "oversold"}]

    @pytest.fixture
    def bearish_rsi(self):
        return [{"rsi": 75, "signal": "overbought"}]

    def test_strong_buy_signal(self, bullish_bollinger, bullish_cci, bullish_rsi):
        """三个指标共振做多 → 强烈买入信号"""
        result = TechnicalIndicators.analyze_signals(
            bullish_bollinger, bullish_cci, bullish_rsi
        )
        assert result["score"] == 1
        assert "买入" in result["verdict"]

    def test_strong_sell_signal(self, bearish_bollinger, bearish_cci, bearish_rsi):
        """三个指标共振做空 → 强烈卖出信号"""
        result = TechnicalIndicators.analyze_signals(
            bearish_bollinger, bearish_cci, bearish_rsi
        )
        assert result["score"] == -1
        assert "卖出" in result["verdict"]

    def test_mixed_signals(self, bullish_bollinger, bullish_cci):
        """混合信号（1多0空） → 轻微买入"""
        neutral_rsi = [{"rsi": 50, "signal": "neutral"}]
        result = TechnicalIndicators.analyze_signals(
            bullish_bollinger, bullish_cci, neutral_rsi
        )
        assert result["score"] == 0.5
        assert "买入" in result["verdict"]

    def test_balanced_signals(self, bullish_bollinger, bearish_cci, bullish_rsi):
        """势均力敌信号（1多1空） → 中性"""
        result = TechnicalIndicators.analyze_signals(
            bullish_bollinger, bearish_cci, bullish_rsi
        )
        assert result["score"] == 0

    def test_neutral_signals(self):
        """全部中性 → 中性信号"""
        neutral_bb = [{"upper": 11.0, "lower": 9.0, "close": 10.0, "signal": "neutral"}]
        neutral_cci = [{"cci": 0, "signal": "neutral"}]
        neutral_rsi = [{"rsi": 50, "signal": "neutral"}]
        result = TechnicalIndicators.analyze_signals(
            neutral_bb, neutral_cci, neutral_rsi
        )
        assert result["score"] == 0


# ============================================================================
# StressTestEngine 静态方法测试
# ============================================================================

class TestStressTestEngine:
    """压力测试引擎测试"""

    def test_calc_var_95_confidence(self):
        """95% 置信度 VaR 计算"""
        var = StressTestEngine.calc_var(
            position_value=100000.0,
            daily_vol=0.02,
            confidence=0.95,
            days=1,
        )
        expected = 100000 * 0.02 * 1.645
        assert abs(var - expected) < 1.0, f"VaR={var}, expected≈{expected}"

    def test_calc_var_99_confidence(self):
        """99% 置信度 VaR 计算"""
        var = StressTestEngine.calc_var(
            position_value=500000.0,
            daily_vol=0.03,
            confidence=0.99,
            days=1,
        )
        expected = 500000 * 0.03 * 2.326
        assert abs(var - expected) < 1.0

    def test_calc_var_multi_day(self):
        """多日 VaR 计算（含 sqrt(T) 调整）"""
        var_1d = StressTestEngine.calc_var(
            position_value=100000.0, daily_vol=0.02, confidence=0.95, days=1
        )
        var_5d = StressTestEngine.calc_var(
            position_value=100000.0, daily_vol=0.02, confidence=0.95, days=5
        )
        expected_ratio = math.sqrt(5)
        assert abs(var_5d / var_1d - expected_ratio) < 0.1

    def test_calc_max_drawdown_normal(self, sample_equity_curve):
        """最大回撤计算：先涨后跌场景"""
        result = StressTestEngine.calc_max_drawdown(sample_equity_curve)
        assert result["max_drawdown_pct"] > 0
        assert result["peak_value"] > 100.0

    def test_calc_max_drawdown_no_loss(self):
        """无亏损场景：最大回撤为 0"""
        curve = [100.0, 101.0, 102.0, 103.0, 104.0]
        result = StressTestEngine.calc_max_drawdown(curve)
        assert result["max_drawdown_pct"] == 0.0

    def test_calc_max_drawdown_short_curve(self):
        """短曲线边界测试"""
        result = StressTestEngine.calc_max_drawdown([100.0])
        assert result["max_drawdown_pct"] == 0.0

    def test_calc_max_drawdown_empty(self):
        """空曲线边界测试"""
        result = StressTestEngine.calc_max_drawdown([])
        assert result["max_drawdown_pct"] == 0.0


# ============================================================================
# 月度收益计算测试
# ============================================================================

class TestMonthlyReturns:
    """月度收益率计算测试"""

    def test_basic_calculation(self):
        """基本月度收益计算"""
        values = [100.0, 101.0, 102.0, 103.0]
        dates = ["2026-01-01", "2026-01-15", "2026-02-01", "2026-02-15"]
        result = _calc_monthly_returns(values, dates)
        assert len(result) >= 1
        for entry in result:
            assert "month" in entry
            assert "return_pct" in entry

    def test_single_month(self):
        """跨月数据（需要跨月才有月度收益）"""
        result = _calc_monthly_returns(
            [100.0, 105.0, 110.0],
            ["2026-01-01", "2026-01-31", "2026-02-15"]
        )
        assert len(result) == 1
        assert result[0]["month"] == "2026-02"

    def test_empty_input(self):
        """空输入"""
        result = _calc_monthly_returns([], [])
        assert result == []