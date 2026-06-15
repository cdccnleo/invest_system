"""InvestPilot 跨模块工具函数 (P1-T6 6/15 实战 PIT #117 防御)

PIT #117 (6/15 实战): dict.get(key, 0) 当 key 存在但值为 None 时, 默认值 0
**不生效** 直接返 None, 后续 None > 0 比较会抛 TypeError. 修复用 isinstance 兜底
None (亦覆盖 str/None 等非数值).

Class-level 铁律 (跨项目通用):
    任何 dict.get(key, default_value) 后接 > < == != + - * / 数值运算,
    都**必**用 _safe_num 兜底 None.

实战反模式 6 形态 (全文件 grep 必查):
    | 形态       | 反例                            | 安全写法                       |
    |------------|--------------------------------|-------------------------------|
    | 比较       | if p.get("close", 0) > 0:      | if _safe_num(p.get("close")) > 0: |
    | 加减       | total = z.get("n", 0) + 1      | total = _safe_num(z.get("n")) + 1 |
    | 浮点       | weight = w.get("w", 0) * 1.0   | weight = _safe_num(w.get("w")) * 1.0 |
    | if/while   | if x.get("pct", 0) < 100:      | if _safe_num(x.get("pct")) < 100: |
    | return     | return y.get("n", 0) * 1.0     | return _safe_num(y.get("n")) * 1.0 |
    | f-string   | f"{x.get('p', 0):.2f}"         | f"{_safe_num(x.get('p')):.2f}" |

实战 6/15 触发链: PIT #112 数据修复未完成 (6/19 周末修) → cost=None 入库 →
PIT #117 close=None 比较 → TypeError. 修后整个脚本统一用 _safe_num.
"""
from __future__ import annotations


def _safe_num(v, default: float = 0):
    """PIT #117 6/15 实战: dict.get(key, 0) 当 key 存在但值为 None 时, 默认值 0
    不生效直接返 None, 后续 None > 0 比较会抛 TypeError. 修复用 isinstance 兜底
    None (亦覆盖 str/None 等非数值). default 参数允许指定兜底值 (如 close=None 时
    用 cost 兜底).

    Examples:
        >>> _safe_num(None)
        0
        >>> _safe_num(None, default=1.5)
        1.5
        >>> _safe_num(0)
        0
        >>> _safe_num(0.0)
        0.0
        >>> _safe_num("0")
        0
        >>> _safe_num("abc")
        0
        >>> _safe_num([1, 2])
        0
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return int(v)  # True/False → 1/0 (避免被算作 1.0)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except (ValueError, TypeError):
            return default
    return default  # list/dict/其他非数值


__all__ = ["_safe_num"]
