"""
fetch_reports.py — 东方财富研报采集模块
接口: https://reportapi.eastmoney.com/report/list
数据写入: research.research_reports
向量存储: research.report_embeddings（通过 embedding_service）
"""

import logging
import time
from datetime import datetime, date

import requests
import psycopg2

logger = logging.getLogger("invest_system.fetch_reports")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
}

TIMEOUT = 20

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",  # 运行时由 _get_password() 注入,
}

# ─── 数据库操作 ────────────────────────────────────────────────────────────

def _get_password():
    from pgcrypto_migration import get_credential
    return get_credential("DB_PASSWORD")

def get_db_conn():
    cfg = dict(DB_CONFIG)
    cfg["password"] = _get_password()
    return psycopg2.connect(**cfg)


def is_duplicate(info_code: str, title: str, report_dt: date) -> bool:
    """检查研报是否已存在（按 infoCode 或 title+date 去重）"""
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT 1 FROM research.research_reports
            WHERE (info_code = %s AND %s != '')
               OR (title = %s AND report_date = %s)
            LIMIT 1
        """, (info_code, info_code, title, report_dt))
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()


def save_report(report: dict) -> bool:
    """写入单条研报到 research_reports，返回是否新增成功"""
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO research.research_reports
                (ts_code, title, summary, rating, report_date, source, url, info_code)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (info_code) DO NOTHING
            RETURNING id
        """, (
            report.get("ts_code", ""),
            report["title"],
            report.get("summary", ""),
            report.get("rating", ""),
            report.get("report_date"),
            report.get("source", ""),
            report.get("url", ""),
            report.get("info_code", ""),
        ))
        inserted = cur.fetchone() is not None
        conn.commit()
        return inserted
    except Exception as e:
        conn.rollback()
        logger.warning(f"写入研报失败: {e}")
        return False
    finally:
        cur.close()
        conn.close()


# ─── 研报抓取 ──────────────────────────────────────────────────────────────

def fetch_reports_page(
    begin_time: str,
    end_time: str,
    page_no: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """
    抓取单页研报

    Returns:
        (reports, total_count)
    """
    url = "https://reportapi.eastmoney.com/report/list"
    params = {
        "beginTime": begin_time,
        "endTime": end_time,
        "pageNo": page_no,
        "pageSize": page_size,
        "qType": 0,        # 0=全部研报
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    data = resp.json()

    if data.get("code") is not None and data.get("code") != 0:
        logger.warning(f"东方财富研报 API 错误: code={data.get('code')} msg={data.get('message', '')}")
        return [], 0

    raw_reports = data.get("data", [])
    total = data.get("hits", 0)
    if isinstance(raw_reports, dict):
        raw_reports = raw_reports.get("list", [])
    elif not isinstance(raw_reports, list):
        raw_reports = []

    results = []
    for r in raw_reports:
        title = r.get("title", "").strip()
        if not title:
            continue

        info_code = r.get("infoCode", "")
        # 日期解析
        pub_date_str = r.get("publishDate", "")
        try:
            if pub_date_str:
                report_dt = datetime.strptime(pub_date_str[:10], "%Y-%m-%d").date()
            else:
                report_dt = date.today()
        except Exception:
            report_dt = date.today()

        # 格式化评级
        rating = r.get("sRatingName", "") or r.get("emRatingName", "") or r.get("rank", "") or ""

        results.append({
            "info_code": info_code,
            "ts_code": r.get("stockCode", "") or "",
            "title": title,
            "summary": r.get("summary", "") or r.get("digest", "") or "",
            "rating": str(rating),
            "report_date": report_dt,
            "source": r.get("orgName", "") or r.get("source", "") or "东方财富",
            "url": r.get("docUrl", "") or r.get("encodeUrl", "") or "",
        })

    return results, total


def collect_reports(
    days_back: int = 3,
    max_pages: int = 10,
    save_to_db: bool = True,
) -> list[dict]:
    """
    采集近 N 天研报

    Args:
        days_back: 追溯天数，默认3天
        max_pages: 每批次最大页数（防过量）
        save_to_db: 是否直接写入数据库

    Returns:
        采集的研报列表（含去重标记）
    """
    end_dt = date.today()
    begin_dt = date.fromordinal(end_dt.toordinal() - days_back)
    begin_str = begin_dt.isoformat()
    end_str = end_dt.isoformat()

    logger.info(f"开始采集研报: {begin_str} → {end_str}")

    all_reports = []
    seen_titles = set()
    total_hits = 0

    for page in range(1, max_pages + 1):
        try:
            reports, total = fetch_reports_page(begin_str, end_str, page_no=page)
            if page == 1:
                total_hits = total
                logger.info(f"  服务器返回总数: {total} 条")

            if not reports:
                logger.info(f"  第{page}页无数据，停止")
                break

            new_count = 0
            for r in reports:
                prefix = r["title"][:60]
                if prefix in seen_titles:
                    continue
                seen_titles.add(prefix)
                all_reports.append(r)

                if save_to_db:
                    # 去重检查
                    if is_duplicate(r["info_code"], r["title"], r["report_date"]):
                        continue
                    if save_report(r):
                        new_count += 1

            logger.info(f"  第{page}页: 获取 {len(reports)} 条, 新增写入 {new_count} 条")
            if new_count == 0 and page > 1:
                # 说明已经都重复了
                pass

            # 停止条件：返回数据 < page_size 说明到最后一页了
            if len(reports) < 20:
                break

            time.sleep(0.3)

        except Exception as e:
            logger.warning(f"  第{page}页采集失败: {e}")
            break

    logger.info(f"研报采集完成: 共 {len(all_reports)} 条（去重后），服务器总命中 {total_hits} 条")
    return all_reports


# ─── 持仓股票研报专项采集 ─────────────────────────────────────────────────

def collect_reports_for_positions(ts_codes: list[str]) -> list[dict]:
    """
    针对持仓股票采集研报（近7天）

    Args:
        ts_codes: 股票代码列表，支持纯数字（如 "300059"）或带后缀（如 "300059.SH"）

    Returns:
        持仓相关研报列表（已写入数据库）
    """
    # 标准化代码（去掉 .SH/.SZ/.XSHE 后缀）
    normalized = set()
    for c in ts_codes:
        code = c.strip().split(".")[0]
        if code:
            normalized.add(code)

    end_dt = date.today()
    begin_dt = date.fromordinal(end_dt.toordinal() - 7)
    begin_dt.isoformat()
    end_dt.isoformat()

    # 获取近7天全市场研报
    all_reports = collect_reports(days_back=7, save_to_db=True, max_pages=10)
    logger.info(f"近7天全市场研报: {len(all_reports)} 份")

    # 按代码过滤
    position_reports = []
    for r in all_reports:
        stock_code = (r.get("ts_code") or "").strip()
        if stock_code in normalized:
            position_reports.append(r)
            logger.info(f"  [持仓相关] {stock_code}: {r['title'][:50]}")

    logger.info(f"持仓相关研报: {len(position_reports)} 份（总持仓股: {len(normalized)} 只）")
    return position_reports


# ─── 向量化（调用 embedding_service）────────────────────────────────────────

def embed_reports(report_ids: list[int] = None):
    """
    对入库研报生成向量（调用 embedding_service）

    Args:
        report_ids: 指定研报 ID 列表；None 时对所有未向量化的研报处理
    """
    try:
        from embedding_service import embed_research_report
        conn = get_db_conn()
        cur = conn.cursor()
        try:
            if report_ids:
                cur.execute("""
                    SELECT id, title, summary, source
                    FROM research.research_reports
                    WHERE id = ANY(%s)
                      AND id NOT IN (SELECT report_id FROM research.report_embeddings WHERE report_id IS NOT NULL)
                """, (report_ids,))
            else:
                cur.execute("""
                    SELECT rr.id, rr.title, rr.summary, rr.source
                    FROM research.research_reports rr
                    LEFT JOIN research.report_embeddings re ON re.report_id = rr.id
                    WHERE re.id IS NULL
                    LIMIT 50
                """)
            rows = cur.fetchall()
            logger.info(f"待向量化研报: {len(rows)} 条")
            for row in rows:
                rid, title, summary, source = row
                text = f"{source}研报：{title}。{summary or ''}"
                vec = embed_research_report(rid, text)
                logger.info(f"  研报{id} 向量化: {'成功' if vec else '失败'}")
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.warning(f"向量化失败: {e}")


# ─── 可独立运行 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    reports = collect_reports(days_back=3, save_to_db=True)
    print(f"\n采集完成: {len(reports)} 条研报（含去重）")
    for r in reports[:5]:
        print(f"  [{r['report_date']}] {r['source']} | {r['rating']} | {r['title'][:55]}")
        print(f"    ts_code={r['ts_code']} url={r['url'][:50] if r['url'] else 'N/A'}")
