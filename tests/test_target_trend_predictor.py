"""
test_target_trend_predictor.py — 趋势预测器单元测试
覆盖: TargetTrendPredictor 无数据库依赖的纯函数逻辑
"""

import pytest
from unittest.mock import patch, MagicMock
from target_trend_predictor import TargetTrendPredictor


class TestTargetTrendPredictorInit:
    """初始化测试"""

    def test_init_ts_code(self):
        """ts_code 应正确存储"""
        predictor = TargetTrendPredictor("000977.XSHE")
        assert predictor.ts_code == "000977.XSHE"


class TestEarningsSurprise:
    """季报超预期概率预测测试"""

    def test_insufficient_data(self):
        """数据不足时返回 unknown"""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur

        with patch("target_trend_predictor._get_db_conn", return_value=mock_conn):
            predictor = TargetTrendPredictor("000977.XSHE")
            result = predictor.predict_earnings_surprise_probability()
            assert result["surprise_direction"] == "unknown"
            assert result["probability"] == 0.0
            assert result["confidence"] == "low"

    def test_minimal_data(self):
        """数据不足3个季度时返回 unknown"""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            ("2026-03-31", 5000000, 500000, 10.0, 15.0),
            ("2025-12-31", 4800000, 480000, 8.0, 12.0),
        ]
        mock_conn.cursor.return_value = mock_cur

        with patch("target_trend_predictor._get_db_conn", return_value=mock_conn):
            predictor = TargetTrendPredictor("000977.XSHE")
            result = predictor.predict_earnings_surprise_probability()
            assert result["confidence"] == "low"
            assert result["data_points"] == 2


class TestDivergenceDetection:
    """背离检测测试"""

    def test_insufficient_price_data(self):
        """价格数据不足时返回空列表"""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur

        with patch("target_trend_predictor._get_db_conn", return_value=mock_conn):
            predictor = TargetTrendPredictor("000977.XSHE")
            result = predictor.detect_divergence_signals()
            assert isinstance(result, list)
            assert len(result) == 0


class TestRiskEscalation:
    """风险升级评估测试"""

    def test_insufficient_data(self):
        """数据不足时风险评分为 0"""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur

        with patch("target_trend_predictor._get_db_conn", return_value=mock_conn):
            predictor = TargetTrendPredictor("000977.XSHE")
            result = predictor.assess_risk_escalation()
            assert result["risk_level"] == 0