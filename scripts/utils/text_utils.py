"""
text_utils.py — 文本处理工具
"""

from typing import List


def chunk_text(text: str, max_chars: int = 500) -> List[str]:
    """
    简单分块：按句号/换行切分，合并到 max_chars。

    Args:
        text: 待分块的原始文本
        max_chars: 每个 chunk 的最大字符数（默认 500）

    Returns:
        分块后的字符串列表
    """
    sentences = text.replace("\n", "。").split("。")
    chunks, current = [], ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(current) + len(s) < max_chars:
            current += ("。" if current else "") + s
        else:
            if current:
                chunks.append(current)
            current = s
    if current:
        chunks.append(current)
    return chunks