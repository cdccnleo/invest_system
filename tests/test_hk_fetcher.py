"""
test_hk_fetcher.py — 港股数据采集单元测试
"""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd


class TestNormalizeHKCode:
    """港股代码标准化测试"""

    def test_short_code(self):
        from hk_fetcher import normalize_hk_code
        assert normalize_hk_code("5") == "00005"

    def test_normal_code(self):
        from hk_fetcher import normalize_hk_code
        assert normalize_hk_code("700") == "00700"

    def test_full_code(self):
        from hk_fetcher import normalize_hk_code
        assert normalize_hk_code("09988") == "09988"


class TestFetchHKRealtimeQuote:
    """港股实时行情测试"""

    @patch("akshare.stock_hk_spot_em")
    def test_no_data_returns_empty(self, mock_spot):
        from hk_fetcher import fetch_hk_realtime_quote
        mock_spot.return_value = None
        result = fetch_hk_realtime_quote("00700")
        assert result == {}

    @patch("akshare.stock_hk_spot_em")
    def test_code_not_found_returns_empty(self, mock_spot):
        from hk_fetcher import fetch_hk_realtime_quote
        mock_spot.return_value = pd.DataFrame({"代码": ["00001"], "名称": ["长和"],
                                                "最新价": [50.0], "涨跌幅": [1.5]})
        result = fetch_hk_realtime_quote("00700")
        assert result == {}


class TestHKIndexMap:
    """港股指数映射测试"""

    def test_index_names(self):
        from hk_fetcher import HK_INDEX_MAP
        assert HK_INDEX_MAP["HSI"] == "恒生指数"
        assert HK_INDEX_MAP["HSTECH"] == "恒生科技指数"


class TestStoreHKToDB:
    """港股数据存储测试"""

    def test_empty_records(self):
        from hk_fetcher import store_hk_daily_to_db
        result = store_hk_daily_to_db([])
        assert result == 0