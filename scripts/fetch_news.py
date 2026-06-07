"""
fetch_news.py — 新闻数据采集模块
支持多源：财联社网页版 / 同花顺快讯 / 东方财富资讯 / 新浪财经 / 金十数据
财联社已从 RSS 迁移至网页版（__NEXT_DATA__ 解析）
"""

import logging
import time
import re
from datetime import datetime

import requests

logger = logging.getLogger("invest_system.fetch_news")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.eastmoney.com/",
}

TIMEOUT = 15

# ─── 关键词过滤 ────────────────────────────────────────────────────────────

RELEVANT_KEYWORDS = [
    "A股", "上证", "深证", "创业板", "科创板", "北交所",
    "央行", "货币政策", "降息", "降准", "LPR",
    "证监会", "银保监会", "财政部", "商务部",
    "证券", "ETF", "基金", "QFII", "北上资金",
    "年报", "季报", "分红", "除权", "配股", "回购",
    "涨停", "跌停", "龙虎榜", "主力资金",
    "半导体", "新能源", "医药", "白酒", "银行", "券商",
    "房地产", "人工智能", "AI", "芯片",
    "美股", "港股", "美联储", "美债", "原油", "黄金",
]


def is_relevant(title: str, content: str = "") -> bool:
    """判断新闻是否与持仓相关（关键词过滤）"""
    text = (title + content).upper()
    for kw in RELEVANT_KEYWORDS:
        if kw.upper() in text:
            return True
    return False


def assess_severity(title: str) -> str:
    """评估新闻严重程度"""
    high = ["降息", "降准", "证监会", "央行", "财政部", "涨停", "跌停", "重大", "黑天鹅", "监管", "暂停", "终止"]
    medium = ["北上资金", "龙虎榜", "回购", "分红", "业绩", "突破", "加仓"]
    for kw in high:
        if kw in title:
            return "HIGH"
    for kw in medium:
        if kw in title:
            return "MEDIUM"
    return "LOW"


# ─── 单数据源采集 ──────────────────────────────────────────────────────────

def fetch_cailian_web() -> list[dict]:
    """
    财联社电报网页版（https://www.cls.cn/telegraph）
    通过解析 __NEXT_DATA__ JSON 获取结构化数据，每页20条。
    已替代已停用的财联社 RSS（2025年停用）。
    """
    results = []
    try:
        resp = requests.get(
            "https://www.cls.cn/telegraph",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.cls.cn/",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(f"财联社网页版 HTTP {resp.status_code}")
            return results

        # 从 HTML 中提取 __NEXT_DATA__ JSON
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
        if not m:
            logger.warning("财联社网页版未找到 __NEXT_DATA__")
            return results

        import json as _json
        data = _json.loads(m.group(1))
        telegraph = data.get("props", {}).get("initialState", {}).get("telegraph", {}).get("telegraphList", [])

        for item in telegraph:
            if item.get("is_ad"):  # 过滤广告
                continue

            title = item.get("title", "").strip()
            content = item.get("content", "").strip()
            if not title:
                continue

            # 过滤不相关
            if not is_relevant(title, content):
                continue

            # 时间戳转换
            ts = int(item.get("ctime", 0))
            if ts:
                pub_dt = datetime.fromtimestamp(ts)
                pub_time = pub_dt.isoformat()
            else:
                pub_time = datetime.now().isoformat()

            # 关联股票
            stock_list = item.get("stock_list") or []
            stocks = []
            for s in stock_list:
                name = s.get("name", "")
                code = s.get("code", "")
                if name:
                    stocks.append(f"{name}({code})" if code else name)

            results.append({
                "title": title,
                "content": (content or "")[:500],
                "source": item.get("author", "财联社") or "财联社",
                "url": item.get("shareurl") or "",
                "published_at": pub_time,
                "severity": assess_severity(title),
                "keywords": [kw for kw in RELEVANT_KEYWORDS if kw in title],
                "_stocks": ",".join(stocks),  # 关联股票（供后续使用）
            })

        logger.info(f"财联社网页版获取 {len(results)} 条")

    except Exception as e:
        logger.warning(f"财联社网页版获取失败: {e}")

    return results


def fetch_sina_finance() -> list[dict]:
    """新浪财经 A 股快讯（备源）"""
    results = []
    try:
        url = "https://feed.mix.sina.com.cn/api/roll/get"
        params = {
            "pageid": 153,
            "lid": 2516,  # A 股新闻
            "k": "",
            "num": 30,
            "page": 1,
            "r": 0.5,
            "time": int(time.time()),
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        data = resp.json()

        for item in data.get("result", {}).get("data", []):
            title = item.get("title", "").strip()
            if not title or not is_relevant(title):
                continue
            try:
                pub_ts = int(item.get("ctime", 0))
                pub_time = datetime.fromtimestamp(pub_ts).isoformat() if pub_ts else datetime.now().isoformat()
            except Exception:
                pub_time = datetime.now().isoformat()

            results.append({
                "title": title,
                "content": item.get("intro", "")[:500],
                "source": "新浪财经",
                "url": item.get("url", ""),
                "published_at": pub_time,
                "severity": assess_severity(title),
                "keywords": [kw for kw in RELEVANT_KEYWORDS if kw in title],
            })
        logger.info(f"新浪财经获取 {len(results)} 条")
    except Exception as e:
        logger.warning(f"新浪财经获取失败: {e}")
    return results


def fetch_jin10_news() -> list[dict]:
    """金十数据（宏观/大宗商品）"""
    results = []
    try:
        url = "https://www.jin10.com/get_actual_news"
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        data = resp.json()
        for item in (data.get("data", []) or [])[:20]:
            title = str(item.get("content", "")).strip()
            if not title or not is_relevant(title):
                continue
            results.append({
                "title": title,
                "content": "",
                "source": "金十数据",
                "url": "",
                "published_at": item.get("time", datetime.now().isoformat()),
                "severity": assess_severity(title),
                "keywords": [kw for kw in RELEVANT_KEYWORDS if kw in title],
            })
        logger.info(f"金十数据获取 {len(results)} 条")
    except Exception as e:
        logger.warning(f"金十数据获取失败: {e}")
    return results


# ─── 主入口 ────────────────────────────────────────────────────────────────

def collect_news() -> list[dict]:
    """多源采集新闻，自动去重，关键词过滤"""
    logger.info("开始采集新闻...")
    all_news = []
    seen_titles = set()

    # ── 财联社电报网页版（主源，__NEXT_DATA__ 解析）────────────────────
    try:
        cailian_news = fetch_cailian_web()
        for n in cailian_news:
            prefix = n["title"][:40]
            if prefix not in seen_titles:
                seen_titles.add(prefix)
                all_news.append(n)
        logger.info(f"  财联社电报: 添加 {len(cailian_news)} 条")
    except Exception as e:
        logger.warning(f"  财联社采集异常: {e}")

    # ── 同花顺快讯（备源）─────────────────────────────────────────────
    try:
        from fetch_ths_news import fetch_ths_news
        ths_news = fetch_ths_news(pages=3)
        for n in ths_news:
            prefix = n["title"][:40]
            if prefix not in seen_titles:
                seen_titles.add(prefix)
                all_news.append(n)
        logger.info(f"  同花顺快讯: 添加 {len(ths_news)} 条")
    except Exception as e:
        logger.warning(f"  同花顺采集异常: {e}")

    sources = [
        ("新浪财经", fetch_sina_finance),
        ("金十数据", fetch_jin10_news),
    ]

    for name, fetcher in sources:
        try:
            news = fetcher()
            added = 0
            for n in news:
                prefix = n["title"][:40]
                if prefix not in seen_titles:
                    seen_titles.add(prefix)
                    all_news.append(n)
                    added += 1
            logger.info(f"  {name}: 添加 {added} 条")
        except Exception as e:
            logger.warning(f"  {name} 采集异常: {e}")

    # 按严重程度排序（HIGH > MEDIUM > LOW），同级别按时间倒序
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    all_news.sort(key=lambda x: (
        severity_order.get(x.get("severity", "LOW"), 2),
        x.get("published_at", "")
    ))
    # 按严重程度倒序（HIGH 在前），同级别按时间倒序
    all_news.sort(key=lambda x: (
        -severity_order.get(x.get("severity", "LOW"), 2),
        x.get("published_at", "")
    ))

    logger.info(f"新闻采集完成: 共 {len(all_news)} 条（已去重）")
    return all_news


# ─── 数据库持久化 ──────────────────────────────────────────────────────────

def save_news_to_db(news: list[dict]) -> int:
    """
    将采集的新闻列表写入 PostgreSQL research.news_articles 表。
    通过 storage_factory 的 write_news 方法实现 ON CONFLICT (title, published_at) DO NOTHING 去重。

    Args:
        news: collect_news() 返回的新闻字典列表

    Returns:
        实际新增写入的新闻条数
    """
    if not news:
        logger.info("save_news_to_db: 无新闻数据可写入")
        return 0

    try:
        from storage_factory import get_storage
        storage = get_storage()
        saved = storage.write_news(news)
        storage.close()
        logger.info(f"save_news_to_db: 采集 {len(news)} 条, 新增写入 {saved} 条")
        return saved
    except Exception as e:
        logger.error(f"save_news_to_db 失败: {e}")
        return 0


def collect_and_save_news() -> dict:
    """
    一键采集并保存新闻，用于手动同步按钮调用。

    Returns:
        {"status": "ok"|"empty"|"error", "total": int, "saved": int, "error": str|None}
    """
    try:
        news = collect_news()
        if not news:
            return {"status": "empty", "total": 0, "saved": 0, "error": None}
        saved = save_news_to_db(news)
        return {"status": "ok", "total": len(news), "saved": saved, "error": None}
    except Exception as e:
        logger.error(f"collect_and_save_news 异常: {e}")
        return {"status": "error", "total": 0, "saved": 0, "error": str(e)}
