"""
test_data_validator.py — 数据质量校验层单元测试
覆盖: validate_quotes_data, validate_news_data
"""

import pytest
from data_validator import validate_quotes_data, validate_news_data


class TestValidateQuotesData:
    """行情数据校验测试"""

    def test_valid_data_passes(self):
        """正常数据应全部通过校验"""
        rows = [
            {"ts_code": "000977.XSHE", "close": 50.5, "volume": 10000000, "change_pct": 2.5},
            {"ts_code": "600519.XSHG", "close": 1800.0, "volume": 5000000, "change_pct": -1.2},
        ]
        valid, errors = validate_quotes_data(rows)
        assert len(valid) == 2
        assert len(errors) == 0

    def test_missing_ts_code(self):
        """缺失股票代码应被过滤"""
        rows = [{"ts_code": "", "close": 50.0, "volume": 1000}]
        valid, errors = validate_quotes_data(rows)  # noqa: F841
        assert len(valid) == 0
        assert len(errors) == 1

    def test_negative_price(self):
        """负价格应被过滤"""
        rows = [{"ts_code": "000977.XSHE", "close": -10.0, "volume": 1000}]
        valid, errors = validate_quotes_data(rows)  # noqa: F841
        assert len(valid) == 0

    def test_zero_price(self):
        """零价格应被过滤"""
        rows = [{"ts_code": "000977.XSHE", "close": 0.0, "volume": 1000}]
        valid, errors = validate_quotes_data(rows)  # noqa: F841
        assert len(valid) == 0

    def test_excessive_price(self):
        """超高价格应被过滤"""
        rows = [{"ts_code": "000977.XSHE", "close": 200000.0, "volume": 1000}]
        valid, errors = validate_quotes_data(rows)  # noqa: F841
        assert len(valid) == 0

    def test_negative_volume(self):
        """负成交量应被过滤"""
        rows = [{"ts_code": "000977.XSHE", "close": 50.0, "volume": -100}]
        valid, errors = validate_quotes_data(rows)  # noqa: F841
        assert len(valid) == 0

    def test_type_conversion(self):
        """字符串类型应按需转换"""
        rows = [{"ts_code": "000977.XSHE", "close": "50.5", "volume": "10000"}]
        valid, errors = validate_quotes_data(rows)  # noqa: F841
        assert len(valid) == 1
        assert isinstance(valid[0]["close"], float)
        assert isinstance(valid[0]["volume"], int)

    def test_invalid_price_parse(self):
        """无法解析的价格应被过滤"""
        rows = [{"ts_code": "000977.XSHE", "close": "abc", "volume": 1000}]
        valid, errors = validate_quotes_data(rows)  # noqa: F841
        assert len(valid) == 0

    def test_kcb_allow_30pct_change(self):
        """科创板允许 ±30% 涨跌幅"""
        rows = [{"ts_code": "688008.XSHG", "close": 50.0, "volume": 10000, "change_pct": 25.0}]
        valid, errors = validate_quotes_data(rows)
        assert len(valid) == 1

    def test_main_board_reject_30pct(self):
        """主板拒绝 >20% 涨跌幅（仅警告不跳过）"""
        rows = [{"ts_code": "000977.XSHE", "close": 50.0, "volume": 10000, "change_pct": 25.0}]
        valid, errors = validate_quotes_data(rows)
        assert len(valid) == 1
        assert len(errors) == 1

    def test_empty_list(self):
        """空列表输入"""
        valid, errors = validate_quotes_data([])  # noqa: F841
        assert valid == []


class TestValidateNewsData:
    """新闻数据校验测试"""

    def test_valid_news_passes(self):
        """正常新闻应通过校验"""
        articles = [
            {
                "title": "测试新闻标题",
                "content": "测试新闻内容",
                "published_at": "2026-05-31 10:00:00",
                "stock_codes": ["000977"],
            }
        ]
        valid, errors = validate_news_data(articles)
        assert len(valid) == 1

    def test_empty_title(self):
        """空标题应被过滤"""
        articles = [{"title": "", "content": "内容"}]
        valid, errors = validate_news_data(articles)  # noqa: F841
        assert len(valid) == 0

    def test_duplicate_detection(self):
        """重复标题应去重"""
        articles = [
            {"title": "相同标题", "content": "内容A", "published_at": "2026-05-31 10:00:00"},
            {"title": "相同标题", "content": "内容B", "published_at": "2026-05-31 11:00:00"},
        ]
        valid, errors = validate_news_data(articles)
        assert len(valid) == 1