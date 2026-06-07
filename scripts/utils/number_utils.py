"""
number_utils.py — 数值处理工具
"""


def safe_float(v, default: float = 0.0) -> float:
    """
    安全将值转为 float，转换失败返回 default。

    Args:
        v: 待转换的值
        default: 转换失败时的默认值（默认 0.0）

    Returns:
        转换后的 float 值
    """
    try:
        return float(str(v).replace(',', '').replace('"', '').strip() or '0')
    except (ValueError, TypeError):
        return default