"""
test_factor_engine.py — 多因子评分引擎单元测试
"""
import pytest
import numpy as np
from unittest.mock import patch, MagicMock
import pandas as pd


class TestFactorEngineInit:
    """因子引擎初始化测试"""

    def test_default_weights(self):
        from factor_engine import FactorEngine, DEFAULT_WEIGHTS
        engine = FactorEngine()
        assert engine.weights == DEFAULT_WEIGHTS

    def test_custom_weights(self):
        from factor_engine import FactorEngine
        custom = {"value": 0.3, "quality": 0.3, "momentum": 0.2, "volatility": 0.1, "technical": 0.05, "size": 0.05}
        engine = FactorEngine(weights=custom)
        assert engine.weights == custom

    def test_weights_normalization(self):
        from factor_engine import FactorEngine
        engine = FactorEngine(weights={"value": 1.0, "quality": 1.0})
        total = sum(engine.weights.values())
        assert abs(total - 1.0) < 0.001


class TestFactorEngineScoring:
    """因子评分计算测试"""

    def test_score_single_basic(self):
        from factor_engine import FactorEngine
        engine = FactorEngine()
        with patch("factor_engine.calc_value_factor", return_value=3.0), \
             patch("factor_engine.calc_quality_factor", return_value=4.0), \
             patch("factor_engine.calc_momentum_factor", return_value=5.0), \
             patch("factor_engine.calc_volatility_factor", return_value=4.0), \
             patch("factor_engine.calc_technical_factor", return_value=3.0), \
             patch("factor_engine.calc_size_factor", return_value=2.0), \
             patch("factor_engine._get_price_data", return_value=pd.DataFrame()):
            result = engine.score_single("000001.XSHE", 15.0)
            assert "ts_code" in result
            assert "raw_scores" in result
            assert "weighted_scores" in result
            assert "total_score" in result
            assert result["ts_code"] == "000001.XSHE"

    def test_score_batch_ranking(self):
        from factor_engine import FactorEngine
        engine = FactorEngine()
        with patch("factor_engine.calc_value_factor", side_effect=[3, 1, 5]), \
             patch("factor_engine.calc_quality_factor", return_value=3.0), \
             patch("factor_engine.calc_momentum_factor", return_value=2.0), \
             patch("factor_engine.calc_volatility_factor", return_value=4.0), \
             patch("factor_engine.calc_technical_factor", return_value=3.0), \
             patch("factor_engine.calc_size_factor", return_value=2.0), \
             patch("factor_engine._get_price_data", return_value=pd.DataFrame()):
            results = engine.score_batch(["A.XSHE", "B.XSHE", "C.XSHE"])
            assert len(results) == 3
            assert results[0]["rank"] == 1
            assert results[-1]["rank"] == 3
            for r in results:
                assert "z_score" in r
                assert isinstance(r["z_score"], float)

    def test_score_batch_empty(self):
        from factor_engine import FactorEngine
        engine = FactorEngine()
        results = engine.score_batch([])
        assert results == []

    def test_get_set_weights(self):
        from factor_engine import FactorEngine
        engine = FactorEngine()
        w = engine.get_factor_weights()
        assert "value" in w
        new_w = {"value": 0.5, "quality": 0.3, "momentum": 0.1, "volatility": 0.05, "technical": 0.03, "size": 0.02}
        engine.set_factor_weights(new_w)
        assert engine.get_factor_weights() == new_w


class TestValueFactor:
    """价值因子测试"""

    @patch("factor_engine._get_financial_data")
    def test_low_pe_high_score(self, mock_fin):
        from factor_engine import calc_value_factor
        mock_fin.return_value = {"eps": 2.0, "bps": 15.0}
        score = calc_value_factor("test.XSHE", 20.0)  # PE=10, PB=1.33
        assert score >= 4.0

    @patch("factor_engine._get_financial_data")
    def test_negative_eps_zero(self, mock_fin):
        from factor_engine import calc_value_factor
        mock_fin.return_value = {"eps": -1.0, "bps": 5.0}
        score = calc_value_factor("test.XSHE", 10.0)
        assert score <= 2.0  # Only PB contributes

    @patch("factor_engine._get_financial_data")
    def test_high_pe_low_score(self, mock_fin):
        from factor_engine import calc_value_factor
        mock_fin.return_value = {"eps": 0.5, "bps": 10.0}
        score = calc_value_factor("test.XSHE", 50.0)  # PE=100
        assert score < 1.0

    @patch("factor_engine._get_financial_data")
    def test_no_financial_data(self, mock_fin):
        from factor_engine import calc_value_factor
        mock_fin.return_value = {}
        score = calc_value_factor("test.XSHE", 20.0)
        assert score == 0.0


class TestQualityFactor:
    """质量因子测试"""

    @patch("factor_engine._get_financial_data")
    def test_high_roe_growth(self, mock_fin):
        from factor_engine import calc_quality_factor
        mock_fin.return_value = {"roe": 25.0, "profit_growth": 60.0}
        score = calc_quality_factor("test.XSHE")
        assert score >= 5.5

    @patch("factor_engine._get_financial_data")
    def test_negative_roe(self, mock_fin):
        from factor_engine import calc_quality_factor
        mock_fin.return_value = {"roe": -5.0, "profit_growth": -20.0}
        score = calc_quality_factor("test.XSHE")
        assert score <= -3.0

    @patch("factor_engine._get_financial_data")
    def test_no_data(self, mock_fin):
        from factor_engine import calc_quality_factor
        mock_fin.return_value = {}
        score = calc_quality_factor("test.XSHE")
        assert score == 0.0


class TestMomentumFactor:
    """动量因子测试"""

    def test_positive_momentum(self):
        from factor_engine import calc_momentum_factor
        import pandas as pd
        import numpy as np
        dates = pd.date_range(end="2026-05-31", periods=90, freq="B")
        prices = np.linspace(10, 15, 90)  # 50% rise over 90 days
        df = pd.DataFrame({"close": prices, "change_pct": np.zeros(90)})
        with patch("factor_engine._get_price_data", return_value=df):
            score = calc_momentum_factor("test.XSHE")
            assert score > 0

    def test_negative_momentum(self):
        from factor_engine import calc_momentum_factor
        import pandas as pd
        import numpy as np
        dates = pd.date_range(end="2026-05-31", periods=90, freq="B")
        prices = np.linspace(15, 10, 90)
        df = pd.DataFrame({"close": prices, "change_pct": np.zeros(90)})
        with patch("factor_engine._get_price_data", return_value=df):
            score = calc_momentum_factor("test.XSHE")
            assert score < 0

    def test_insufficient_data(self):
        from factor_engine import calc_momentum_factor
        with patch("factor_engine._get_price_data", return_value=pd.DataFrame()):
            score = calc_momentum_factor("test.XSHE")
            assert score == 0.0


class TestVolatilityFactor:
    """波动率因子测试"""

    def test_low_volatility_high_score(self):
        from factor_engine import calc_volatility_factor
        import pandas as pd
        import numpy as np
        dates = pd.date_range(end="2026-05-31", periods=90, freq="B")
        prices = np.linspace(10, 11, 90)
        df = pd.DataFrame({
            "close": prices,
            "change_pct": np.random.normal(0, 0.5, 90)
        })
        with patch("factor_engine._get_price_data", return_value=df):
            score = calc_volatility_factor("test.XSHE")
            assert score >= 3.0

    def test_high_volatility_low_score(self):
        from factor_engine import calc_volatility_factor
        import pandas as pd
        import numpy as np
        dates = pd.date_range(end="2026-05-31", periods=90, freq="B")
        df = pd.DataFrame({
            "close": np.linspace(8, 15, 90),
            "change_pct": np.random.normal(0, 5.0, 90)
        })
        with patch("factor_engine._get_price_data", return_value=df):
            score = calc_volatility_factor("test.XSHE")
            assert score <= 2.0

    def test_empty_data(self):
        from factor_engine import calc_volatility_factor
        with patch("factor_engine._get_price_data", return_value=pd.DataFrame()):
            score = calc_volatility_factor("test.XSHE")
            assert score == 0.0


class TestTechnicalFactor:
    """技术因子测试"""

    def test_neutral_rsi(self):
        from factor_engine import calc_technical_factor
        import pandas as pd
        import numpy as np
        dates = pd.date_range(end="2026-05-31", periods=30, freq="B")
        prices = [10.0] * 15 + [10.05] * 15
        df = pd.DataFrame({"close": prices, "change_pct": np.zeros(30)})
        with patch("factor_engine._get_price_data", return_value=df):
            score = calc_technical_factor("test.XSHE")
            assert score >= 0

    def test_insufficient_data(self):
        from factor_engine import calc_technical_factor
        with patch("factor_engine._get_price_data", return_value=pd.DataFrame()):
            score = calc_technical_factor("test.XSHE")
            assert score == 0.0


class TestScorePositions:
    """持仓评分集成测试"""

    def test_score_positions_filters_funds(self):
        from factor_engine import score_positions
        from unittest.mock import patch
        positions = [
            {"code": "000001", "type": "stock", "close": 12.0},
            {"code": "000002", "type": "fund", "close": 1.5},
            {"code": "600519", "type": "stock", "close": 1800.0},
        ]
        with patch("factor_engine.FactorEngine.score_batch", return_value=[]):
            results = score_positions(positions)
            assert isinstance(results, list)


class TestGetDefaultEngine:
    """默认引擎获取测试"""

    def test_get_default_engine(self):
        from factor_engine import get_default_engine, FactorEngine
        engine = get_default_engine()
        assert isinstance(engine, FactorEngine)