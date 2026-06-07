"""
file_utils.py — 文件处理工具
"""

from pathlib import Path
from typing import Optional


def read_file_with_encoding(filepath: Path) -> Optional[str]:
    """
    多编码尝试读取文件内容。

    尝试顺序: utf-8 → gbk → cp936 → utf-8-sig
    任意一种编码成功则返回内容，全部失败返回 None。

    Args:
        filepath: 文件路径

    Returns:
        文件内容字符串，或失败时 None
    """
    for encoding in ['utf-8', 'gbk', 'cp936', 'utf-8-sig']:
        try:
            return filepath.read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None