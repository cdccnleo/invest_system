"""
test_tamf_updater.py — TAMF 更新引擎单元测试
覆盖: normalize_ts_code, get_tamf_path, TAMF 章节解析
"""

import pytest
from pathlib import Path
from tamf_updater import normalize_ts_code, get_tamf_path


class TestNormalizeTsCode:
    """股票代码归一化测试"""

    def test_fund_code(self):
        """基金代码应映射到 .OF"""
        assert normalize_ts_code("159516") == "159516.OF"
        assert normalize_ts_code("512880") == "512880.OF"
        assert normalize_ts_code("007355") == "007355.OF"

    def test_double_innovation(self):
        """科创板 688xxx 应映射到上交所"""
        assert normalize_ts_code("688008") == "688008.XSHG"
        assert normalize_ts_code("688025") == "688025.XSHG"

    def test_shanghai_main(self):
        """上交所主板应映射到 .XSHG"""
        assert normalize_ts_code("600519") == "600519.XSHG"
        assert normalize_ts_code("601208") == "601208.XSHG"
        assert normalize_ts_code("603259") == "603259.XSHG"

    def test_shenzhen_main(self):
        """深交所主板应映射到 .XSHE"""
        assert normalize_ts_code("000977") == "000977.XSHE"
        assert normalize_ts_code("002050") == "002050.XSHE"
        assert normalize_ts_code("300059") == "300059.XSHE"

    def test_fund_code_with_name(self):
        """ETF 名称应参与基金判断"""
        assert normalize_ts_code("510050", "上证50ETF") == "510050.OF"

    def test_fund_code_with_chinese_name(self):
        """中文名称含'基金'应识别为基金"""
        assert normalize_ts_code("404002", "某某基金") == "404002.OF"

    def test_default_to_shenzhen(self):
        """未知格式默认深交所"""
        assert normalize_ts_code("999999") == "999999.XSHE"


class TestGetTamfPath:
    """TAMF 文件路径测试"""

    def test_basic_path(self):
        """基本路径生成"""
        path = get_tamf_path("000977")
        assert isinstance(path, Path)
        assert path.name == "000977.md"

    def test_path_structure(self):
        """路径应在 data/target_memories/ 下"""
        path = get_tamf_path("600519")
        assert "target_memories" in str(path)
        assert path.suffix == ".md"