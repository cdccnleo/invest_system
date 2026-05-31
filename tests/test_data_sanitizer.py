"""
test_data_sanitizer.py — 数据脱敏器单元测试
覆盖: _get_anon_id, _get_real_code, reset_mapping, sanitize_snapshot
"""

import pytest
from data_sanitizer import _get_anon_id, _get_real_code, reset_mapping, sanitize_snapshot


class TestCodeMapping:
    """股票代码匿名映射测试"""

    def setup_method(self):
        reset_mapping()

    def test_get_anon_id_consistency(self):
        """同一股票代码应映射到相同匿名 ID"""
        id1 = _get_anon_id("000977")
        id2 = _get_anon_id("000977")
        assert id1 == id2

    def test_get_anon_id_format(self):
        """匿名 ID 格式应为 STK_NNN"""
        anon_id = _get_anon_id("600519")
        assert anon_id.startswith("STK_")
        assert len(anon_id) == 7

    def test_different_codes_different_ids(self):
        """不同股票代码应有不同匿名 ID"""
        id1 = _get_anon_id("000977")
        id2 = _get_anon_id("600519")
        assert id1 != id2

    def test_real_code_roundtrip(self):
        """代码 → 匿名 ID → 代码 应能还原"""
        anon_id = _get_anon_id("002050")
        real = _get_real_code(anon_id)
        assert real == "002050"

    def test_reset_mapping(self):
        """重置后重新映射"""
        _get_anon_id("000977")
        reset_mapping()
        new_id = _get_anon_id("000977")
        assert new_id == "STK_001"

    def test_unknown_real_code(self):
        """未知匿名 ID 应返回自身"""
        assert _get_real_code("UNKNOWN") == "UNKNOWN"


class TestSanitizeSnapshot:
    """持仓快照脱敏测试"""

    def setup_method(self):
        reset_mapping()

    def test_basic_sanitization(self):
        """基本脱敏：金额转百分比，代码转匿名 ID"""
        positions = [
            {"code": "000977", "name": "浪潮信息", "market_value": 50000,
             "cost": 45000, "close": 50.0, "shares": 1000, "weight": 50.0},
            {"code": "600519", "name": "贵州茅台", "market_value": 50000,
             "cost": 48000, "close": 1800.0, "shares": 28, "weight": 50.0},
        ]
        result, mapping = sanitize_snapshot(100000.0, positions)
        assert len(result) == 2
        assert len(mapping) == 2

        for item in result:
            assert "code" in item or "anon_id" in item
            assert "pnl_dir" in item

    def test_pnl_direction_profit(self):
        """盈利应标记为盈利方向"""
        positions = [
            {"code": "000977", "name": "测试", "market_value": 12000,
             "cost": 10.0, "close": 12.0, "shares": 1000, "weight": 100.0},
        ]
        result, _ = sanitize_snapshot(12000.0, positions)
        assert "盈利" in result[0]["pnl_dir"]

    def test_pnl_direction_loss(self):
        """亏损应标记为亏损方向"""
        positions = [
            {"code": "000977", "name": "测试", "market_value": 8000,
             "cost": 10.0, "close": 8.0, "shares": 1000, "weight": 100.0},
        ]
        result, _ = sanitize_snapshot(8000.0, positions)
        assert "亏损" in result[0]["pnl_dir"]

    def test_zero_market_value(self):
        """总市值为零时返回空"""
        result, mapping = sanitize_snapshot(0.0, [])
        assert result == []

    def test_mapping_structure(self):
        """反向映射表应包含代码和名称"""
        positions = [
            {"code": "000977", "name": "浪潮信息", "market_value": 50000,
             "cost": 45000, "close": 50.0, "shares": 1000, "weight": 50.0},
        ]
        _, mapping = sanitize_snapshot(50000.0, positions)
        for anon_id, info in mapping.items():
            assert "code" in info
            assert "name" in info