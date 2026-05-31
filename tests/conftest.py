"""
pytest 共享 fixtures
"""

import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture
def sample_uptrend_prices():
    """
    模拟上升趋势的收盘价序列（30 个交易日）
    从 10.0 逐步上涨到 15.5
    """
    return [10.0 + i * 0.2 + (i % 3) * 0.05 for i in range(30)]


@pytest.fixture
def sample_downtrend_prices():
    """
    模拟下降趋势的收盘价序列（30 个交易日）
    从 20.0 逐步下跌到 14.2
    """
    return [20.0 - i * 0.2 - (i % 3) * 0.05 for i in range(30)]


@pytest.fixture
def sample_sideways_prices():
    """
    模拟横盘震荡的收盘价序列（30 个交易日）
    在 10.0 附近上下波动
    """
    import math
    return [10.0 + math.sin(i * 0.5) * 0.5 for i in range(30)]


@pytest.fixture
def sample_ohlc_data():
    """
    模拟 OHLC 数据（30 个交易日）
    返回 (closes, highs, lows)
    """
    closes = [10.0 + i * 0.15 for i in range(30)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.3 for c in closes]
    return closes, highs, lows


@pytest.fixture
def sample_equity_curve():
    """
    模拟权益曲线（100 个交易日）
    先涨后跌，用于测试最大回撤计算
    """
    values = [100.0]
    for i in range(1, 100):
        if i < 50:
            values.append(values[-1] * (1 + 0.01))
        elif i < 70:
            values.append(values[-1] * (1 - 0.015))
        else:
            values.append(values[-1] * (1 + 0.005))
    return values