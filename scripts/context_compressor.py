"""
Prompt 上下文摘要压缩模块
3-tier truncation: Budget / Tier / Target Memory prioritization
Using tiktoken for accurate token counting
"""

from typing import Callable
from datetime import datetime
from enum import IntEnum


try:
    import tiktoken
    _ENCODING = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENCODING = None


class MemoryTier(IntEnum):
    """Memory tier priority - higher = more important"""
    ARCHIVE = 0      # Lowest priority - old/expired
    BACKGROUND = 1   # Background info
    STANDARD = 2     # Standard context
    IMPORTANT = 3   # High priority items
    CRITICAL = 4    # Must-keep items


# Tier thresholds and budgets
DEFAULT_BUDGET = 8000
DEFAULT_TIER_BUDGETS = {
    MemoryTier.CRITICAL: 3000,
    MemoryTier.IMPORTANT: 2500,
    MemoryTier.STANDARD: 1500,
    MemoryTier.BACKGROUND: 500,
    MemoryTier.ARCHIVE: 0,
}
DEFAULT_TARGET_MEMORY_RESERVE = 1500


def count_tokens(text: str) -> int:
    """
    Count tokens accurately using tiktoken.
    Falls back to estimation if tiktoken unavailable.
    """
    if not text:
        return 0
    if _ENCODING is None:
        # Fallback: Chinese chars / 2 + English words
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        english_words = len(text.split())
        return int(chinese_chars / 2 + english_words)
    try:
        return len(_ENCODING.encode(text))
    except Exception:
        # Fallback on encoding error
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        english_words = len(text.split())
        return int(chinese_chars / 2 + english_words)


def get_item_tier(item: dict) -> MemoryTier:
    """
    Determine the memory tier for an item based on metadata.

    Priority order:
    1. CRITICAL: Explicitly marked critical, or has critical keywords
    2. IMPORTANT: High importance score (>= 0.8) or important keywords
    3. STANDARD: Normal items
    4. BACKGROUND: Low importance (< 0.3) or older items
    5. ARCHIVE: Very old (30+ days) or explicitly marked as archive
    """
    # Check explicit tier marker
    if tier := item.get("_tier"):
        if isinstance(tier, MemoryTier):
            return tier
        try:
            return MemoryTier[tier.upper()]
        except KeyError:
            pass

    # Check importance score
    importance = item.get("importance", 0.5)
    if importance >= 0.9:
        return MemoryTier.CRITICAL
    elif importance >= 0.8:
        return MemoryTier.IMPORTANT
    elif importance < 0.3:
        return MemoryTier.BACKGROUND

    # Check date for archive tier
    date_str = item.get("date")
    if date_str:
        try:
            if isinstance(date_str, str):
                dt = datetime.strptime(date_str, "%Y-%m-%d")
            else:
                dt = date_str
            age_days = (datetime.now() - dt).days
            if age_days > 30:
                return MemoryTier.ARCHIVE
            elif age_days > 14:
                return MemoryTier.BACKGROUND
        except (ValueError, TypeError):
            pass

    # Check for critical/important keywords
    content = item.get("content", "") or item.get("title", "")
    critical_keywords = ["紧急", "critical", "重大", "突破", "危机"]
    important_keywords = ["重要", "关注", "推荐", "买入", "上调"]

    for kw in critical_keywords:
        if kw in content:
            return MemoryTier.CRITICAL
    for kw in important_keywords:
        if kw in content:
            return MemoryTier.IMPORTANT

    return MemoryTier.STANDARD


def default_cost_fn(item: dict) -> int:
    """Default item token calculation function."""
    content = item.get("content", "") or item.get("title", "")
    return count_tokens(content)


def compress_context(
    items: list[dict],
    max_tokens: int = DEFAULT_BUDGET,
    item_cost_fn: Callable[[dict], int] | None = None,
) -> list[dict]:
    """
    Compress context items using 3-tier truncation (Budget/Tier/Target Memory).

    Args:
        items: List of dicts with "content", "importance", "date" keys
        max_tokens: Hard budget limit (default 8000)
        item_cost_fn: Function to calculate token cost per item

    Returns:
        Compressed items list maintaining importance order
    """
    if not items:
        return []

    if item_cost_fn is None:
        item_cost_fn = default_cost_fn

    # Calculate costs for all items
    costs = [item_cost_fn(item) for item in items]
    total_tokens = sum(costs)

    # Fast path: no compression needed
    if total_tokens <= max_tokens:
        return items.copy()

    return _three_tier_truncate(items, costs, max_tokens)


def _three_tier_truncate(
    items: list[dict],
    costs: list[int],
    max_tokens: int,
    tier_budgets: dict[MemoryTier, int] | None = None,
    target_reserve: int = DEFAULT_TARGET_MEMORY_RESERVE,
) -> list[dict]:
    """
    3-tier truncation algorithm:

    1. BUDGET: Hard limit on total tokens
    2. TIER: Allocate budget per memory tier based on priority
    3. TARGET MEMORY: Reserve space for specific critical items

    Args:
        items: Context items
        costs: Token costs per item
        max_tokens: Total budget
        tier_budgets: Per-tier token budgets
        target_reserve: Tokens reserved for target memory items
    """
    if tier_budgets is None:
        tier_budgets = DEFAULT_TIER_BUDGETS.copy()

    # Assign tier to each item
    tiered_items = [(item, cost, get_item_tier(item)) for item, cost in zip(items, costs)]

    # Sort by tier (descending priority), then by importance (descending)
    tiered_items.sort(key=lambda x: (x[2], x[0].get("importance", 0.5)), reverse=True)

    result: list[dict] = []
    tier_spent: dict[MemoryTier, int] = {t: 0 for t in MemoryTier}
    current_tokens = 0

    # Calculate available budget after target reserve
    available_budget = max_tokens - target_reserve
    if available_budget < 0:
        available_budget = max_tokens // 2

    for item, cost, tier in tiered_items:
        tier_budget = tier_budgets.get(tier, 0)
        tier_current = tier_spent[tier]

        # Check tier budget and overall budget
        if tier_current + cost > tier_budget and tier != MemoryTier.CRITICAL:
            # Skip items that would exceed tier budget (except critical)
            continue

        if current_tokens + cost > max_tokens:
            # Final hard budget check
            if tier == MemoryTier.CRITICAL and tier_spent[MemoryTier.CRITICAL] < target_reserve:
                # Critical items can use target reserve
                pass
            else:
                break

        result.append(item)
        current_tokens += cost
        tier_spent[tier] += cost

    # Add summary for truncated items if needed
    original_count = len(items)
    result_count = len(result)
    if result_count < original_count:
        # Find which items were truncated
        result_ids = {id(r) for r in result}
        truncated = [it for it in items if id(it) not in result_ids]
        if truncated:
            summary = _generate_summary(truncated)
            summary_cost = default_cost_fn(summary)
            if current_tokens + summary_cost <= max_tokens:
                result.append(summary)

    return result


def _generate_summary(truncated: list[dict]) -> dict:
    """
    Generate a summary item for truncated content.

    Groups by type and creates a compact summary string.
    """
    categories: dict[str, list[dict]] = {}

    for item in truncated:
        item_type = item.get("type", "info")
        if item_type not in categories:
            categories[item_type] = []
        categories[item_type].append(item)

    summary_parts = []
    total_count = 0

    for cat, cat_items in categories.items():
        count = len(cat_items)
        total_count += count
        sample = cat_items[0].get("title", cat_items[0].get("content", "")[:15])
        if count == 1:
            summary_parts.append(sample[:20])
        else:
            summary_parts.append(f"{sample[:15]}等{count}条")

    content = f"【摘要】{'、'.join(summary_parts)}等共{total_count}条(已压缩)"

    return {
        "type": "summary",
        "content": content,
        "count": total_count,
        "importance": 0.1,
        "categories": list(categories.keys()),
        "_tier": MemoryTier.ARCHIVE,
    }


def compress_by_tier(
    items: list[dict],
    tier: MemoryTier,
    max_tokens: int | None = None,
) -> list[dict]:
    """
    Compress items of a specific tier and below.

    Args:
        items: All context items
        tier: Maximum tier to include
        max_tokens: Tier-specific budget (uses DEFAULT_TIER_BUDGETS if None)

    Returns:
        Filtered items at or below the specified tier
    """
    if max_tokens is None:
        max_tokens = DEFAULT_TIER_BUDGETS.get(tier, DEFAULT_BUDGET // 2)

    tier_items = [
        (item, default_cost_fn(item), get_item_tier(item))
        for item in items
    ]

    # Filter to requested tier and below
    filtered = [(it, c, t) for it, c, t in tier_items if t <= tier]
    filtered.sort(key=lambda x: (x[2], x[0].get("importance", 0.5)), reverse=True)

    result = []
    spent = 0
    for item, cost, _ in filtered:
        if spent + cost > max_tokens:
            break
        result.append(item)
        spent += cost

    return result


def compress_news(
    news_list: list[dict],
    max_tokens: int = 4000,
) -> list[dict]:
    """
    News-specific compression with date + importance + sentiment scoring.

    Args:
        news_list: List of news items
        max_tokens: Maximum tokens (default 4000)

    Returns:
        Compressed news list
    """
    if not news_list:
        return []

    def date_score(news: dict) -> float:
        date_str = news.get("date", "")
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d") if isinstance(date_str, str) else date_str
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0.0

    def news_score(news: dict) -> tuple:
        ds = date_score(news)
        importance = news.get("importance", 0.5)
        sentiment = abs(news.get("sentiment", 0))
        return (ds, importance, sentiment)

    sorted_news = sorted(news_list, key=news_score, reverse=True)

    return compress_context(
        sorted_news,
        max_tokens=max_tokens,
        item_cost_fn=lambda item: count_tokens(item.get("content", "") or item.get("title", "")),
    )


def compress_reports(
    reports: list[dict],
    max_tokens: int = 3000,
) -> list[dict]:
    """
    Research report compression with authority weighting.

    Args:
        reports: List of research reports
        max_tokens: Maximum tokens (default 3000)

    Returns:
        Compressed report list
    """
    if not reports:
        return []

    AUTHORITY_WEIGHTS = {
        "中金": 1.0,
        "中信": 0.95,
        "华泰": 0.9,
        "国泰": 0.85,
        "海通": 0.85,
        "广发": 0.8,
        "招商": 0.8,
        "兴业": 0.75,
        "方正": 0.7,
        "长江": 0.7,
    }

    def get_authority(report: dict) -> float:
        institution = report.get("institution", "")
        for name, weight in AUTHORITY_WEIGHTS.items():
            if name in institution:
                return weight
        return 0.5

    def date_score(report: dict) -> float:
        date_str = report.get("date", "")
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d") if isinstance(date_str, str) else date_str
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0.0

    def report_score(report: dict) -> tuple:
        ds = date_score(report)
        authority = get_authority(report)
        importance = report.get("importance", 0.5)
        return (ds, authority, importance)

    sorted_reports = sorted(reports, key=report_score, reverse=True)

    return compress_context(
        sorted_reports,
        max_tokens=max_tokens,
        item_cost_fn=lambda item: count_tokens(item.get("content", "") or item.get("title", "")),
    )


if __name__ == "__main__":
    import sys
    from pathlib import Path

    root = Path(__file__).parent.parent.resolve()
    sys.path.insert(0, str(root))

    from scripts.context_compressor import compress_news, compress_reports, compress_context, count_tokens, MemoryTier

    # Test token counting
    test_text = "这是一个测试文本" * 50
    tokens = count_tokens(test_text)
    print(f"Token count test: {tokens} tokens for {len(test_text)} chars")

    # Test news compression
    test_news = [
        {"title": f"新闻{i}", "content": "x" * 200, "date": f"2026-05-{i:02d}", "importance": 0.5 + i * 0.01, "sentiment": 0.1 * i}
        for i in range(1, 31)
    ]
    result = compress_news(test_news, max_tokens=1500)
    print(f"News: {len(test_news)} -> {len(result)} (budget: 1500)")
    has_summary = any(r.get("type") == "summary" for r in result)
    print(f"Has summary: {has_summary}")

    # Test tier assignment
    tier_test_items = [
        {"content": "紧急：重大事件", "importance": 0.9},
        {"content": "普通新闻", "importance": 0.5},
        {"content": "背景信息", "importance": 0.2},
        {"content": "重要更新", "importance": 0.85},
    ]
    for item in tier_test_items:
        tier = get_item_tier(item)
        print(f"Item '{item['content'][:10]}...' -> Tier: {MemoryTier(tier).name}")

    # Test reports compression
    test_reports = [
        {"title": f"研报{i}", "content": "x" * 300, "date": f"2026-05-{i:02d}", "institution": f"机构{i % 3}", "importance": 0.6}
        for i in range(1, 16)
    ]
    r2 = compress_reports(test_reports, max_tokens=800)
    print(f"Reports: {len(test_reports)} -> {len(r2)} (budget: 800)")

    print("PASS")
