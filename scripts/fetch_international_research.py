#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_international_research.py — 国际投行研报/宏观研究采集模块
============================================================
数据来源：
  1. 华尔街见闻 RSS（feed.wallstreetcn.com）— 每日约26条，含大量国际投行引用
  2. Benzinga RSS（benzinga.com/feed）— 分析师评级/投行研究新闻
  3. Goldman Sachs Insights（部分文章页）— 宏观/市场策略

投行关键词：Morgan Stanley / Goldman Sachs / JPMorgan / Citi / UBS /
          Deutsche Bank / HSBC / Barclays / Credit Suisse / Bank of America 等

注意：完整投行深度报告（PDF）需付费订阅（Bloomberg/Refinitiv），
本模块通过追踪中文财经媒体对国际投行研究的引用和解读，
提供可供 A 股投资参考的结构化投行观点数据。
"""

import logging
import re

import requests
import xml.etree.ElementTree as ET

logger = logging.getLogger("invest_system.fetch_international_research")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

TIMEOUT = 15

# ── 投行关键词 ────────────────────────────────────────────────────────────────

INSTITUTION_KEYWORDS = {
    # 英文全称
    "Morgan Stanley": "摩根士丹利",
    "Goldman Sachs": "高盛",
    "Goldman": "高盛",
    "JPMorgan": "摩根大通",
    "JP Morgan": "摩根大通",
    "Citi": "花旗",
    "Citigroup": "花旗",
    "UBS": "瑞银",
    "Deutsche Bank": "德意志银行",
    "HSBC": "汇丰银行",
    "Barclays": "巴克莱银行",
    "Credit Suisse": "瑞士信贷",
    "Bank of America": "美国银行",
    "BofA": "美国银行",
    " Merrill Lynch": "美林",
    "Morgan": "摩根",
    "MS": "摩根士丹利",
    "GS": "高盛",
    "DB": "德意志银行",
    # 中文简称
    "摩根士丹利": "摩根士丹利",
    "摩根大通": "摩根大通",
    "高盛集团": "高盛",
    "花旗银行": "花旗",
    "瑞银集团": "瑞银",
    "德意志银行": "德意志银行",
    "汇丰银行": "汇丰银行",
    "巴克莱银行": "巴克莱银行",
    "美国银行": "美国银行",
    "美银": "美国银行",
    "美林证券": "美林",
}

# 宏观/市场类关键词（用于判断文章类型）
MACRO_KEYWORDS = [
    "美联储", "降息", "加息", "利率", "CPI", "PPI", "非农",
    "GDP", "衰退", "通胀", "紧缩", "量化宽松", "QE", "QT",
    "联邦基金", "美债", "美元", "汇率", "贸易战", "关税",
    "宏观", "经济", "政策", "央行", "ECB", "BOJ",
]

SECTOR_KEYWORDS = [
    "半导体", "AI", "芯片", "云计算", "新能源", "锂电", "电动车",
    "医药", "生物科技", "互联网", "电商", "金融", "银行",
    "石油", "大宗商品", "黄金", "原油", "铜",
    "房地产", "建筑", "消费", "零售",
]


# ── 数据模型 ────────────────────────────────────────────────────────────────

def _parse_wscn_rss() -> list[dict]:
    """
    采集华尔街见闻 RSS（主力数据源）
    https://feed.wallstreetcn.com/latest
    每日约26条，涵盖全球宏观/美股/A股，含大量国际投行引用。
    """
    articles = []
    try:
        resp = requests.get(
            "https://feed.wallstreetcn.com/latest",
            headers=HEADERS, timeout=TIMEOUT
        )
        if resp.status_code != 200:
            logger.warning(f"华尔街见闻 RSS HTTP {resp.status_code}")
            return articles

        root = ET.fromstring(resp.content)
        items = root.findall('.//item')

        for item in items:
            title = (item.findtext('title') or '').strip()
            desc_raw = item.findtext('description') or ''
            # 去掉 HTML 标签
            desc = re.sub(r'<[^>]+>', '', desc_raw).strip()[:300]
            link = (item.findtext('link') or '').strip()
            pub_text = (item.findtext('pubDate') or '')[:16]
            author = (item.findtext('author') or item.findtext('dc:creator') or '华尔街见闻').strip()

            if not title:
                continue

            text = title + desc
            # 匹配投行
            matched_banks = [kw for kw in INSTITUTION_KEYWORDS if kw in text]

            # 判断类型
            article_type = "general"
            if any(kw in text for kw in MACRO_KEYWORDS):
                article_type = "macro"
            if any(kw in text for kw in SECTOR_KEYWORDS):
                article_type = "sector"

            # 提取来源（华尔街见闻文章中标注的引用投行）
            cited_banks = []
            for kw, zh_name in INSTITUTION_KEYWORDS.items():
                if kw in title or kw in desc:
                    cited_banks.append(zh_name)

            articles.append({
                'title': title,
                'desc': desc[:200],
                'link': link,
                'published_at': pub_text,
                'author': author,
                'source': '华尔街见闻',
                'source_type': 'chinese_media',  # 中文财经媒体对投行研究的引用
                'cited_institutions': list(set(cited_banks)),
                'article_type': article_type if matched_banks else 'other',
                'is_bank_related': bool(matched_banks),
            })

        logger.info(f"华尔街见闻 RSS: {len(items)} 条, 投行相关: {sum(1 for a in articles if a['is_bank_related'])} 条")

    except Exception as e:
        logger.warning(f"华尔街见闻 RSS 采集失败: {e}")

    return articles


def _parse_benzinga_rss() -> list[dict]:
    """
    采集 Benzinga RSS（投资银行/分析师评级新闻）
    https://www.benzinga.com/feed
    """
    articles = []
    try:
        resp = requests.get(
            "https://www.benzinga.com/feed",
            headers=HEADERS, timeout=TIMEOUT
        )
        if resp.status_code != 200:
            logger.warning(f"Benzinga RSS HTTP {resp.status_code}")
            return articles

        root = ET.fromstring(resp.content)
        items = root.findall('.//item')

        for item in items:
            title = (item.findtext('title') or '').strip()
            desc_raw = item.findtext('description') or ''
            desc = re.sub(r'<[^>]+>', '', desc_raw).strip()[:300]
            link = (item.findtext('link') or '').strip()
            pub_text = (item.findtext('pubDate') or '')[:16]

            if not title:
                continue

            text = title + desc
            matched_banks = [kw for kw in INSTITUTION_KEYWORDS if kw in text]

            cited_banks = [INSTITUTION_KEYWORDS[kw] for kw in matched_banks]

            # Benzinga 特有：分析师评级（upgrade/downgrade/target）
            rating_type = ""
            if any(kw in text.lower() for kw in ['upgrade', 'buy rating', 'outperform']):
                rating_type = "buy"
            elif any(kw in text.lower() for kw in ['downgrade', 'sell rating', 'underperform']):
                rating_type = "sell"
            elif any(kw in text.lower() for kw in ['target price', 'price target', 'pt:']):
                rating_type = "target"
            elif any(kw in text.lower() for kw in ['analyst', 'rating', 'coverage']):
                rating_type = "analyst"

            articles.append({
                'title': title,
                'desc': desc[:200],
                'link': link,
                'published_at': pub_text,
                'author': 'Benzinga',
                'source': 'Benzinga',
                'source_type': 'financial_news',
                'cited_institutions': list(set(cited_banks)),
                'article_type': rating_type or ('macro' if any(kw in text for kw in MACRO_KEYWORDS) else 'general'),
                'is_bank_related': bool(matched_banks),
            })

        logger.info(f"Benzinga RSS: {len(items)} 条, 投行相关: {sum(1 for a in articles if a['is_bank_related'])} 条")

    except Exception as e:
        logger.warning(f"Benzinga RSS 采集失败: {e}")

    return articles


def _fetch_goldman_insights() -> list[dict]:
    """
    采集 Goldman Sachs Insights 公开文章。
    注意：GS Insights 页面为纯客户端渲染，无法获取完整文章列表。
    仅保留占位，后续如找到可访问的文章 URL 可扩展。
    """
    # GS 文章 URL 无法通过稳定模式获取，暂跳过
    return []


# ── 主入口 ────────────────────────────────────────────────────────────────

def collect_international_research(days: int = 3) -> list[dict]:
    """
    采集国际投行研究相关文献/新闻。

    Returns:
        list[dict]: 投行研究文章列表（含去重），按发布时间倒序。
    """
    logger.info("开始采集国际投行研究...")

    all_articles = []
    seen_titles = set()

    sources = [
        ("华尔街见闻", _parse_wscn_rss),
        ("Benzinga", _parse_benzinga_rss),
    ]

    for source_name, fetcher in sources:
        try:
            articles = fetcher()
            for a in articles:
                # 按标题前60字去重
                key = a['title'][:60]
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_articles.append(a)
            logger.info(f"  {source_name}: 添加 {len(articles)} 条")
        except Exception as e:
            logger.warning(f"  {source_name} 采集异常: {e}")

    # 按时间倒序，投行相关优先
    bank_first = sorted(
        all_articles,
        key=lambda x: (
            0 if x['is_bank_related'] else 1,
            x['published_at'] or ''
        ),
        reverse=False
    )

    # 投行相关置顶
    bank_related = [a for a in bank_first if a['is_bank_related']]
    others = [a for a in bank_first if not a['is_bank_related']]

    result = bank_related + others

    logger.info(
        f"国际投行研究采集完成: 共 {len(result)} 条"
        f"（投行相关: {len(bank_related)} 条）"
    )
    return result


def format_research_for_prompt(articles: list[dict], max_items: int = 8) -> str:
    """
    将投行研究文章格式化为 LLM Prompt 文本。
    """
    if not articles:
        return ""

    bank_articles = [a for a in articles if a['is_bank_related']][:max_items]
    if not bank_articles:
        return ""

    lines = ["\n## 国际投行研究（摘录）\n"]
    for a in bank_articles:
        cited = ', '.join(a.get('cited_institutions', [])) or a['source']
        art_type = a.get('article_type', '')
        type_str = f"[{art_type}] " if art_type else ""
        lines.append(f"- {type_str}**{cited}**: {a['title']}")
        if a.get('desc'):
            lines.append(f"  {a['desc'][:120]}")

    return '\n'.join(lines)


# ── 独立运行 ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    articles = collect_international_research()
    print(f"\n共采集: {len(articles)} 条")
    print(f"投行相关: {sum(1 for a in articles if a['is_bank_related'])} 条")

    for a in articles[:10]:
        cited = ', '.join(a.get('cited_institutions', [])) or a['source']
        print(f"\n  [{a['source']}] {cited}")
        print(f"  {a['title'][:70]}")
        print(f"  {a['published_at']}")
