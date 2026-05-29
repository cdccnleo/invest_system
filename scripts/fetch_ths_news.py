"""
fetch_ths_news.py — 同花顺快讯采集模块
接口: https://news.10jqka.com.cn/tapp/news/push/stock/
作为财联社 RSS 失效后的主要补充新闻源
"""

import logging
import time
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger("invest_system.fetch_ths")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.10jqka.com.cn/",
}

TIMEOUT = 15
PAGE_SIZE = 30   # 每页条数
MAX_PAGES = 5     # 最多抓取页数（5页≈100条，过滤后约60条相关）

# ─── 关键词过滤（扩展版 — 覆盖A股、宏观、全球市场）──────────────────────

RELEVANT_KEYWORDS = [
    # A股专用
    "A股", "上证", "深证", "创业板", "科创板", "北交所", "沪深", "沪市", "深市", "主板",
    "证监会", "交易所", "发审委", "科创板", "创业板", "注册制",
    # 市场与资金
    "大盘", "指数", "沪指", "深成", "权重", "板块", "概念", "题材",
    "涨停", "跌停", "龙虎榜", "主力资金", "北向", "南向", "北上资金",
    "ETF", "基金", "公募", "私募", "社保", "保险资金", "融资融券", "做空", "做多",
    # 宏观政策
    "央行", "货币政策", "降息", "降准", "LPR", "麻辣粉", "SLF", "逆回购",
    "财政部", "商务部", "银保监会", "证监会", "发改委",
    "美联储", "美债", "美股", "港股", "港交所",
    "原油", "黄金", "大宗商品", "汇率", "人民币", "美元", "在岸", "离岸",
    # 行业与主题
    "半导体", "新能源", "医药", "白酒", "银行", "券商", "房地产", "AI", "芯片", "人工智能",
    "芯片", "光伏", "风电", "储能", "锂电池", "电动汽车", "自动驾驶",
    "华为", "比亚迪", "宁德", "茅台", "平安", "中信", "阿里", "腾讯", "京东",
    # 公司行为
    "回购", "分红", "增持", "减持", "定增", "配股", "除权", "填权",
    "业绩", "年报", "季报", "营收", "净利润", "扣非", "超预期", "低于预期",
    "公告", "停牌", "复牌", "上市", "IPO", "破发", "发行",
    "评级", "目标价", "买入", "增持", "卖出", "减持", "覆盖",
    "研报", "机构", "券商", "估值", "PE", "PB", "市值",
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
    high = ["降息", "降准", "证监会", "央行", "财政部", "涨停", "跌停", "重大", "黑天鹅", "监管", "暂停", "终止", "核查", "问询"]
    medium = ["北上资金", "龙虎榜", "回购", "分红", "业绩", "突破", "加仓", "减持", "增持", "战略", "合作"]
    for kw in high:
        if kw in title:
            return "HIGH"
    for kw in medium:
        if kw in title:
            return "MEDIUM"
    return "LOW"


def _fetch_page(page: int, page_size: int = PAGE_SIZE) -> list[dict]:
    """抓取单页同花顺快讯"""
    url = "https://news.10jqka.com.cn/tapp/news/push/stock/"
    params = {
        "page": page,
        "tag": "",       # 空=全部，A股用"23"
        "track": "website",
        "pageSize": page_size,
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    data = resp.json()

    if str(data.get("code")) != "200":
        logger.warning(f"同花顺 API 返回错误: code={data.get('code')} msg={data.get('msg')}")
        return []

    articles = data.get("data", {}).get("list", [])
    results = []
    for item in articles:
        title = item.get("title", "").strip()
        if not title or not is_relevant(title, item.get("digest", "")):
            continue

        # 时间戳转换
        try:
            pub_ts = int(item.get("ctime", 0))
            pub_time = datetime.fromtimestamp(pub_ts).isoformat() if pub_ts else datetime.now().isoformat()
        except Exception:
            pub_time = datetime.now().isoformat()

        # 提取关键词
        matched_kws = [kw for kw in RELEVANT_KEYWORDS if kw in title or kw in item.get("digest", "")]

        results.append({
            "title": title,
            "content": item.get("digest", "")[:500],
            "source": "同花顺",
            "url": item.get("url", "") or item.get("shareUrl", ""),
            "published_at": pub_time,
            "severity": assess_severity(title),
            "keywords": matched_kws,
        })

    return results


def fetch_ths_news(pages: int = MAX_PAGES) -> list[dict]:
    """
    采集同花顺快讯（多页）

    Args:
        pages: 抓取页数，默认5页（约100条，过滤后约60条相关）
              （同花顺每页固定20条）

    Returns:
        list[dict]: 过滤后的相关新闻列表
    """
    logger.info(f"开始采集同花顺快讯（{pages}页）...")
    all_news = []
    seen_titles = set()

    for page in range(1, pages + 1):
        try:
            news = _fetch_page(page)
            added = 0
            for n in news:
                # 去重（按标题前40字符）
                prefix = n["title"][:40]
                if prefix not in seen_titles:
                    seen_titles.add(prefix)
                    all_news.append(n)
                    added += 1
            logger.info(f"  第{page}页: 获取 {len(news)} 条, 新增 {added} 条")
            # 避免请求过快
            if page < pages:
                time.sleep(0.5)
        except Exception as e:
            logger.warning(f"  第{page}页采集失败: {e}")

    # 按严重程度 + 时间排序
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    all_news.sort(key=lambda x: (
        severity_order.get(x.get("severity", "LOW"), 2),
        x.get("published_at", "")
    ), reverse=True)

    logger.info(f"同花顺快讯采集完成: 共 {len(all_news)} 条（已去重）")
    return all_news


# ─── 可独立运行 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    news = fetch_ths_news(pages=2)
    print(f"\n共获取 {len(news)} 条相关快讯:")
    for n in news[:5]:
        print(f"  [{n['severity']}] {n['published_at'][:10]} {n['title'][:50]}")
        print(f"    来源:{n['source']} 关键词:{n['keywords']}")
