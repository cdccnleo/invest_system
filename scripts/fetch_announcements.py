#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公告数据采集模块 v2.0 — EastMoney API 优先 + Sina 兜底
=======================================================
主源: EastMoney np-anotice-stock API (全市场覆盖，含科创板/北交所)
备源: Sina 财经 HTML 解析 (快速小量补充)

EastMoney API: https://np-anotice-stock.eastmoney.com/api/security/ann
  - 全市场股票公告，含科创板(688xxx)、北交所(8xxxxx)
  - 单次请求返回最多50条，支持分页
  - 2026-05-26 测试确认: 688025 返回30+条公告

数据表: research.announcements
"""

import requests
from bs4 import BeautifulSoup
import re
import time
import sys
import os
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SINA_BULLETIN_URL = "https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllBulletin/stockid/{code}/page/{page}.phtml"
EM_PAGE_SIZE = 50          # EastMoney 每页条数
MAX_EM_PAGES = 3           # EastMoney 最多翻3页（150条/股）
EM_TIMEOUT = 12            # 秒
SINA_TIMEOUT = 15          # 秒
PAGE_DELAY = 0.3            # 请求间隔（秒）


# ── 公告类型分类 ────────────────────────────────────────────────────────────

KEYWORD_TYPES = [
    (["年报", "年度报告"], "年度报告"),
    (["半年报", "中期报告"], "半年度报告"),
    (["三季报", "三季度"], "三季报"),
    (["一季报", "一季度"], "一季报"),
    (["季报"], "季报"),
    (["业绩预告"], "业绩预告"),
    (["业绩报表"], "业绩报表"),
    (["董事会"], "董事会决议"),
    (["股东大会", "年度股东", "临时股东"], "股东大会"),
    (["权益分派", "分红派息", "分红公告"], "分红公告"),
    (["回购"], "回购公告"),
    (["增持"], "增持公告"),
    (["减持"], "减持公告"),
    (["股权激励", "限制性股票", "授予", "归属"], "股权激励"),
    (["审计报告"], "审计报告"),
    (["法律意见"], "法律意见书"),
    (["核查意见"], "核查意见"),
    (["调查", "立案", "处罚", "监管措施"], "监管措施"),
    (["短期融资券", "公司债券", "发行"], "债券发行"),
    (["捐赠"], "重要事项"),
    (["高新技术"], "资质认定"),
    (["投资者关系", "业绩说明"], "投资者关系"),
    (["摘牌", "退市"], "退市风险"),
    (["股份回购", "回购预案"], "回购公告"),
]

def classify_announcement(title: str) -> str:
    for keywords, label in KEYWORD_TYPES:
        if any(kw in title for kw in keywords):
            return label
    return "一般公告"


# ── EastMoney 采集（主源） ──────────────────────────────────────────────────

def _fetch_em_single(code: str, max_pages: int = MAX_EM_PAGES, days_window: int = 30) -> list[dict]:
    """
    通过 EastMoney API 采集单只股票公告。
    EastMoney 全市场覆盖（主板/科创板/创业板/北交所均可用）。
    返回: [{ts_code, notice_date, title, ann_type, url, ann_id}, ...]
    """
    all_anns = []
    cutoff_str = (datetime.now() - timedelta(days=days_window)).strftime('%Y-%m-%d')
    cutoff_ts = int((datetime.now() - timedelta(days=days_window)).timestamp() * 1000)
    seen_ids = set()

    for page in range(1, max_pages + 1):
        url = (
            f"https://np-anotice-stock.eastmoney.com/api/security/ann"
            f"?cb=&sr=-1&page_size={EM_PAGE_SIZE}&page_index={page}"
            f"&ann_type=A&client_source=web&f_node=0&s_node=0&stock_list={code}"
        )
        try:
            resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=EM_TIMEOUT)
            if resp.status_code != 200:
                break

            raw = resp.content
            if not raw:
                break

            d = json.loads(raw)
            notices = []
            if d.get("data") and isinstance(d["data"], dict):
                notices = d["data"].get("list") or []

            # 检查第一条公告日期：若比窗口更旧则停止翻页
            if notices:
                first_date_str = notices[0].get("notice_date", "")
                if isinstance(first_date_str, str) and first_date_str:
                    first_dt = datetime.strptime(first_date_str[:10], "%Y-%m-%d")
                    if first_dt < datetime.strptime(cutoff_str, "%Y-%m-%d"):
                        break  # 本页第一条已比窗口旧，不再翻页

            if not notices:
                break

            has_old = False
            for n in notices:
                try:
                    notice_date = n.get("notice_date", "")
                    if not notice_date:
                        continue
                    # 时间戳处理（毫秒）
                    if isinstance(notice_date, (int, float)):
                        date_ts = datetime.fromtimestamp(notice_date / 1000)
                        date_str = date_ts.strftime("%Y-%m-%d")
                    else:
                        date_str = str(notice_date)[:10]
                        date_ts = datetime.strptime(date_str, "%Y-%m-%d")

                    # 过滤窗口外
                    if date_ts.timestamp() * 1000 < cutoff_ts:
                        has_old = True
                        continue

                    # 去重：用 date_str + title 前30字（art_id/id 均为 None）
                    title = (n.get("title") or n.get("notice_title") or "").strip()
                    if not title or len(title) < 5:
                        continue
                    dup_key = f"{date_str}|{title[:30]}"
                    if dup_key in seen_ids:
                        continue
                    seen_ids.add(dup_key)
                    em_url = f"https://data.eastmoney.com/notices/hot.html"  # art_id 为 None，暂用热点页

                    all_anns.append({
                        "ts_code": code,
                        "notice_date": date_str,
                        "title": title,
                        "ann_type": classify_announcement(title),
                        "url": em_url,
                        "ann_id": dup_key,
                    })
                except Exception:
                    continue

            if has_old:
                break  # 遇到旧数据不再翻页

        except Exception as e:
            # EastMoney 失败，换 Sina
            break

        time.sleep(PAGE_DELAY)

    return all_anns


# ── Sina 采集（备源） ───────────────────────────────────────────────────────

def _fetch_sina_single(code: str, max_pages: int = 5, days_window: int = 30) -> list[dict]:
    """
    通过 Sina 财经 HTML 解析采集单只股票公告。
    适合主板股票，作为 EastMoney 补充。
    """
    all_anns = []
    cutoff = (datetime.now() - timedelta(days=days_window)).strftime('%Y-%m-%d')
    seen_ids = set()

    for page in range(1, max_pages + 1):
        url = SINA_BULLETIN_URL.format(code=code, page=page)
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://vip.stock.finance.sina.com.cn/",
            }, timeout=SINA_TIMEOUT)
            if resp.status_code != 200:
                break

            page_anns = _parse_sina_page(code, resp.text)
            if not page_anns:
                break

            has_old = False
            for ann in page_anns:
                if ann["notice_date"] < cutoff:
                    has_old = True
                    continue
                if ann["ann_id"] in seen_ids:
                    continue
                seen_ids.add(ann["ann_id"])
                all_anns.append(ann)

            if has_old:
                break

        except Exception:
            break

        time.sleep(PAGE_DELAY)

    return all_anns


def _parse_sina_page(code: str, html: str) -> list[dict]:
    """解析 Sina 公告列表页 HTML。"""
    anns = []
    seen_ids = set()
    soup = BeautifulSoup(html, 'html.parser')

    for td in soup.find_all('td'):
        links = td.find_all('a', href=True)
        if not any('BulletinDetail' in str(lk) for lk in links):
            continue

        td_str = re.sub(
            r'<br\s*/?>',
            '|BR|',
            str(td).replace('&amp;', '&').replace('&gt;', '>').replace('&lt;', '<'),
            flags=re.IGNORECASE
        )

        for segment in td_str.split('|BR|'):
            id_m = re.search(r'[?&]id=(\d+)', segment)
            if not id_m:
                continue
            ann_id = id_m.group(1)
            if ann_id in seen_ids:
                continue
            seen_ids.add(ann_id)

            date_m = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2})', segment)
            if not date_m:
                continue
            date_str = date_m.group(1).replace('/', '-')

            title_m = re.search(r'>([^<>]+)<', segment)
            if not title_m:
                continue
            title = title_m.group(1).strip()
            if not title or len(title) < 5:
                continue

            url = (f"https://vip.stock.finance.sina.com.cn"
                   f"/corp/view/vCB_AllBulletinDetail.php?stockid={code}&id={ann_id}")

            anns.append({
                'ts_code': code,
                'notice_date': date_str,
                'title': title,
                'ann_type': classify_announcement(title),
                'url': url,
                'ann_id': ann_id,
            })

    return anns


# ── 公开接口：采集单只股票 ──────────────────────────────────────────────────

def fetch_announcements_for_stock(code: str, max_pages: int = 3,
                                  days_window: int = 30) -> list[dict]:
    """
    采集指定股票近 days_window 天的公告（EastMoney 优先，Sina 兜底）。
    代码需为 6 位纯数字。
    """
    code = str(code).zfill(6)
    if not re.match(r'^\d{6}$', code):
        return []

    # 优先 EastMoney（全市场覆盖）
    em_anns = _fetch_em_single(code, max_pages=max_pages, days_window=days_window)
    if em_anns:
        return em_anns

    # EastMoney 无数据则尝试 Sina
    return _fetch_sina_single(code, max_pages=5, days_window=days_window)


# ── 公开接口：全持仓公告采集 ──────────────────────────────────────────────

def fetch_all_positions_announcements(positions: list = None,
                                     days_window: int = 30,
                                     max_pages: int = 3) -> list[dict]:
    """
    采集所有持仓股近 N 天的公告并写入数据库。
    - 从 DB 读取持仓（load_positions_from_db）
    - 股票：EastMoney 主采（全市场覆盖，含科创板）
    - ETF（场内）：EastMoney 主采
    - 主动管理型基金：跳过（无透明持仓）
    """
    from pgcrypto_migration import load_positions_from_db

    if positions is None:
        positions = load_positions_from_db()

    from storage_factory import get_storage
    storage = get_storage()

    all_anns = []
    now = datetime.now()
    cutoff = (now - timedelta(days=days_window)).strftime('%Y-%m-%d')

    # 主动管理型基金关键词（跳过公告采集）
    SKIP_FUND_KEYWORDS = ['多因子', '混合', '优选', '成长', '价值', '灵活配置']

    for pos in positions:
        code = str(pos.get('code', '')).zfill(6)
        name = pos.get('name', '')
        pos_type = pos.get('type', '')

        # 非6位标准代码跳过
        if not re.match(r'^\d{6}$', code):
            continue

        # 主动管理型基金跳过
        if pos_type == 'fund' and any(kw in name for kw in SKIP_FUND_KEYWORDS):
            continue

        # 跳过 B股/债券等（公告数据少）
        # 6位数字代码均可采集，暂不跳过

        print(f"  📋 {code} {name[:12]}", end="", flush=True)
        anns = fetch_announcements_for_stock(code, max_pages=max_pages, days_window=days_window)
        new_anns = [a for a in anns if a['notice_date'] >= cutoff]
        print(f" → {len(new_anns)} 条（{cutoff}后）")

        if new_anns:
            all_anns.extend(new_anns)

        time.sleep(PAGE_DELAY)

    return all_anns


if __name__ == "__main__":
    print("=" * 60)
    print("公告采集工具 v2.0（EastMoney 优先 + Sina 兜底）")
    print("=" * 60)

    from pgcrypto_migration import load_positions_from_db
    positions = load_positions_from_db()

    # 测试 688025（科创板，之前无数据）
    print("\n[测试] 688025 杰普特（科创板）:")
    test_anns = fetch_announcements_for_stock("688025", max_pages=2, days_window=30)
    print(f"  → {len(test_anns)} 条公告")
    for a in test_anns[:3]:
        print(f"    {a['notice_date']} [{a['ann_type']}] {a['title'][:50]}")

    print("\n[测试] 300059 东方财富（创业板）:")
    test_anns2 = fetch_announcements_for_stock("300059", max_pages=2, days_window=30)
    print(f"  → {len(test_anns2)} 条公告")
    for a in test_anns2[:3]:
        print(f"    {a['notice_date']} [{a['ann_type']}] {a['title'][:50]}")

    print(f"\n[全量采集] 共 {len(positions)} 只持仓 ...")
    all_anns = fetch_all_positions_announcements(positions, days_window=30, max_pages=3)
    print(f"\n采集完成: {len(all_anns)} 条公告")
