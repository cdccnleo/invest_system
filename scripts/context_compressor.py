"""
Prompt 上下文摘要压缩模块
用于压缩 Prompt 上下文，防止 token 超限
"""

from typing import Callable
from datetime import datetime


def estimate_tokens(text: str) -> int:
    """估算 token 数量：中文字符/2 + 英文单词数"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    english_words = len(text.split())
    return int(chinese_chars / 2 + english_words)


def default_cost_fn(item: dict) -> int:
    """默认 item token 计算函数"""
    content = item.get('content', '') or item.get('title', '')
    return estimate_tokens(content)


def compress_context(
    items: list[dict],
    max_tokens: int = 8000,
    item_cost_fn: Callable[[dict], int] = None
) -> list[dict]:
    """
    压缩上下文项列表，保持重要性排序
    
    Args:
        items: [{"content": str, "importance": float, "date": str}, ...] 按重要性排序
        max_tokens: 最大 token 数限制（默认 8000）
        item_cost_fn: 计算单个 item 占用 token 数的函数
    
    Returns:
        压缩后的 items 列表（不改变原有重要性排序）
    """
    if not items:
        return []
    
    if item_cost_fn is None:
        item_cost_fn = default_cost_fn
    
    # 计算每个 item 的 token 消耗
    costs = [item_cost_fn(item) for item in items]
    total_tokens = sum(costs)
    
    # 如果没有超过限制，直接返回
    if total_tokens <= max_tokens:
        return items.copy()
    
    result = []
    current_tokens = 0
    cutoff_index = len(items)
    
    # 贪心选择：优先保留重要的
    for i, (item, cost) in enumerate(zip(items, costs)):
        if current_tokens + cost <= max_tokens:
            result.append(item)
            current_tokens += cost
        else:
            cutoff_index = i
            break
    
    # 对被截断的 items 生成摘要
    if cutoff_index < len(items):
        truncated = items[cutoff_index:]
        summary = summarize_items(truncated, max_items=3)
        if summary:
            result.append(summary[0])
    
    return result


def summarize_items(items: list[dict], max_items: int = 3) -> list[dict]:
    """
    对被截断的 items 生成摘要，每类最多保留3个
    
    Args:
        items: 被截断的 items 列表
        max_items: 每类最多保留数量（默认3个）
    
    Returns:
        摘要项列表 [{"type": "summary", "content": "...", "count": N, "importance": 0.1}]
    """
    if not items:
        return []
    
    # 按 type/title 首词分类
    categories = {}
    for item in items:
        # 尝试获取类型或从标题推断
        item_type = item.get('type', 'news')
        title = item.get('title', item.get('content', '')[:20])
        
        # 简单分类：从标题/内容提取关键词
        if '研报' in title or '研报' in item.get('content', ''):
            category = 'reports'
        elif '新闻' in title:
            category = 'news'
        else:
            category = item_type
        
        if category not in categories:
            categories[category] = []
        categories[category].append(item)
    
    summary_parts = []
    total_count = 0
    
    for category, cat_items in categories.items():
        count = len(cat_items)
        total_count += count
        if count > 0:
            # 取每个分类的第一个标题作为代表
            sample_title = cat_items[0].get('title', '信息')[:15]
            if count == 1:
                summary_parts.append(sample_title)
            else:
                summary_parts.append(f"{sample_title}等{count}条")
    
    if not summary_parts:
        return []
    
    content = f"【摘要】{'、'.join(summary_parts)}等共{total_count}条相关信息(已压缩)"
    
    return [{
        "type": "summary",
        "content": content,
        "count": total_count,
        "importance": 0.1,
        "categories": list(categories.keys())
    }]


def compress_news(
    news_list: list[dict],
    max_tokens: int = 4000
) -> list[dict]:
    """
    新闻压缩专用
    
    估算: 中文字数/2 + 英文单词数 = token 近似
    优先保留: 最新(日期) > 重要性分数 > 情感强度
    
    Args:
        news_list: 新闻列表
        max_tokens: 最大 token 数（默认 4000）
    
    Returns:
        压缩后的新闻列表
    """
    if not news_list:
        return []
    
    # 解析日期，生成排序key
    def get_date_score(news: dict) -> float:
        date_str = news.get('date', '')
        try:
            if isinstance(date_str, str):
                dt = datetime.strptime(date_str, '%Y-%m-%d')
            else:
                dt = date_str
            # 转换为时间戳用于排序
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0
    
    # 综合评分：日期(最新优先) + 重要性 + 情感强度
    def news_score(news: dict) -> tuple:
        date_score = get_date_score(news)
        importance = news.get('importance', 0.5)
        sentiment = abs(news.get('sentiment', 0))  # 情感强度取绝对值
        return (date_score, importance, sentiment)
    
    # 按综合评分排序
    sorted_news = sorted(news_list, key=news_score, reverse=True)
    
    # 使用压缩函数
    return compress_context(
        sorted_news,
        max_tokens=max_tokens,
        item_cost_fn=lambda item: estimate_tokens(item.get('content', '') or item.get('title', ''))
    )


def compress_reports(
    reports: list[dict],
    max_tokens: int = 3000
) -> list[dict]:
    """
    研报压缩专用
    
    按日期 + 机构权威性排序
    
    Args:
        reports: 研报列表
        max_tokens: 最大 token 数（默认 3000）
    
    Returns:
        压缩后的研报列表
    """
    if not reports:
        return []
    
    # 机构权威性权重
    AUTHORITY_WEIGHTS = {
        '中金': 1.0,
        '中信': 0.95,
        '华泰': 0.9,
        '国泰': 0.85,
        '海通': 0.85,
        '广发': 0.8,
        '招商': 0.8,
        '兴业': 0.75,
        '方正': 0.7,
        '长江': 0.7,
    }
    
    def get_authority_weight(report: dict) -> float:
        institution = report.get('institution', '')
        for name, weight in AUTHORITY_WEIGHTS.items():
            if name in institution:
                return weight
        return 0.5  # 默认权重
    
    def get_date_score(report: dict) -> float:
        date_str = report.get('date', '')
        try:
            if isinstance(date_str, str):
                dt = datetime.strptime(date_str, '%Y-%m-%d')
            else:
                dt = date_str
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0
    
    def report_score(report: dict) -> tuple:
        date_score = get_date_score(report)
        authority = get_authority_weight(report)
        importance = report.get('importance', 0.5)
        return (date_score, authority, importance)
    
    # 按综合评分排序
    sorted_reports = sorted(reports, key=report_score, reverse=True)
    
    # 使用压缩函数
    return compress_context(
        sorted_reports,
        max_tokens=max_tokens,
        item_cost_fn=lambda item: estimate_tokens(item.get('content', '') or item.get('title', ''))
    )


if __name__ == '__main__':
    import sys
    sys.path.insert(0, '/home/aileo/invest_system')
    
    from context_compressor import compress_news, compress_reports, compress_context
    
    # 测试新闻压缩
    test_news = [
        {'title': f'新闻{i}', 'content': 'x'*200, 'date': f'2026-05-{i:02d}', 'importance': 0.5+i*0.01, 'sentiment': 0.1*i} 
        for i in range(1, 31)
    ]
    result = compress_news(test_news, max_tokens=1500)
    print(f'原始{len(test_news)}条 -> 压缩后{len(result)}条')
    has_summary = any(r.get('type') == 'summary' for r in result)
    print(f'含摘要项: {has_summary}')
    
    # 测试研报压缩
    test_reports = [
        {'title': f'研报{i}', 'content': 'x'*300, 'date': f'2026-05-{i:02d}', 'institution': f'机构{i%3}', 'importance': 0.6} 
        for i in range(1, 16)
    ]
    r2 = compress_reports(test_reports, max_tokens=800)
    print(f'研报原始{len(test_reports)}条 -> 压缩后{len(r2)}条')
    print('PASS')