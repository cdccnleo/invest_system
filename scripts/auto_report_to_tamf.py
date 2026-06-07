"""
auto_report_to_tamf.py — 研报摘要自动同步至 TAMF 第6章
将已生成摘要的研报聚合为投资研判，写入对应标的 TAMF 文件的第六章。

触发时机：
  1. schedule_runner.job_report_summary_to_tamf() — 每日 16:05（研报采集后）
  2. 可被 job_tamf_update 调用链中引用
"""

import logging
from datetime import date, timedelta
from collections import defaultdict

import psycopg2
import psycopg2.extras

logger = logging.getLogger("invest_system.auto_report_to_tamf")

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",
}


def _get_db_conn():
    from pgcrypto_migration import get_credential
    cfg = dict(DB_CONFIG)
    cfg["password"] = get_credential("DB_PASSWORD")
    return psycopg2.connect(**cfg)


def _ensure_summary_column():
    """确保 research_reports 表有 summary 列"""
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'research' AND table_name = 'research_reports'
              AND column_name = 'summary'
        """)
        if not cur.fetchone():
            cur.execute("ALTER TABLE research.research_reports ADD COLUMN summary TEXT")
            conn.commit()
            logger.info("已添加 research.research_reports.summary 列")
    except Exception as e:
        conn.rollback()
        logger.warning(f"添加 summary 列失败: {e}")
    finally:
        conn.commit()
        cur.close()
        conn.close()


# ─── 研报聚合摘要生成 ─────────────────────────────────────────

def _build_investment_summary(reports: list[dict]) -> str:
    """
    将多篇研报聚合成一段投资研判文本（markdown格式）。
    格式：
      ```\n[聚合内容]\n```
    """
    if not reports:
        return ""
    lines = []
    for r in reports:
        dt = r.get("report_date", "—")
        src = r.get("org_name", r.get("source", "?"))
        rating = r.get("rating", "?")
        title = r.get("title", "?")[:40]
        summary = (r.get("summary") or "")[:200]
        lines.append(f"- **{dt}** [{src}] {rating}: {title}")
        if summary:
            lines.append(f"  > {summary}")
    body = "\n".join(lines)
    return f"""```\n近{len(reports)}篇研报汇总：\n{body}\n```"""


# ─── 核心同步函数 ─────────────────────────────────────────────

def sync_reports_to_tamf(days: int = 7, limit: int = 30) -> dict:
    """
    主函数：从数据库获取近N天带摘要的研报，按 ts_code 聚合，
    生成投资研判后写入对应标的 TAMF 第6章。

    Args:
        days:  回溯天数（默认7天）
        limit: 最大处理研报数

    Returns:
        {
            "processed": 处理研报数,
            "targets_updated": 更新标的数,
            "skipped": 跳过数,
            "details": {ts_code: status}
        }
    """
    _ensure_summary_column()
    conn = _get_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute("""
            SELECT id, ts_code, title, summary, report_date,
                   org_name, rating, target_price, stock_name
            FROM research.research_reports
            WHERE report_date >= %s
              AND summary IS NOT NULL
              AND summary != ''
              AND ts_code IS NOT NULL
            ORDER BY report_date DESC
            LIMIT %s
        """, (date.today() - timedelta(days=days), limit))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        logger.info("无待同步研报摘要")
        return {"processed": 0, "targets_updated": 0, "skipped": 0, "details": {}}

    # 按 ts_code 分组
    reports_by_code: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        ts = row.get("ts_code", "")
        if ts:
            reports_by_code[ts].append(dict(row))

    # 加载持仓代码映射（6位纯数字 → ts_code）
    from tamf_updater import load_positions, normalize_ts_code
    positions = load_positions()
    # ts_code → 持仓code（用于写文件）
    ts_to_code: dict[str, str] = {}
    code_to_name: dict[str, str] = {}
    for pos in positions:
        code = pos["code"]
        name = pos["name"]
        ts = normalize_ts_code(code, name)
        ts_to_code[ts] = code
        code_to_name[code] = name

    # 逐标的写入第6章
    from tamf_updater import update_chapter_6_for_ts_code
    updated = 0
    skipped = 0
    details: dict[str, str] = {}

    for ts_code, reports in reports_by_code.items():
        code = ts_to_code.get(ts_code, "")
        if not code:
            # ts_code 不在持仓中 → 跳过
            skipped += len(reports)
            details[ts_code] = "no_position"
            continue

        summary_text = _build_investment_summary(reports)
        if not summary_text:
            skipped += len(reports)
            details[ts_code] = "empty_summary"
            continue

        result = update_chapter_6_for_ts_code(code, summary_text)
        status = result.get("status", "unknown")
        details[ts_code] = status
        if status == "updated":
            updated += 1
        else:
            skipped += len(reports)

    total = sum(len(v) for v in reports_by_code.values())
    logger.info(
        f"研报摘要→TAMF同步完成: 处理{total}篇, "
        f"更新{updated}只标的, 跳过{skipped}篇"
    )
    return {
        "processed": total,
        "targets_updated": updated,
        "skipped": skipped,
        "details": details,
    }


# ─── 定时任务入口 ─────────────────────────────────────────────

def job_report_summary_to_tamf() -> dict:
    """
    每日 16:05 定时任务入口（schedule_runner 调度）。
    在 job_reports_collection 完成研报采集和摘要生成后，
    将最新研报摘要写入对应持仓标的的 TAMF 第6章。
    """
    logger.info("=" * 50)
    logger.info("16:05 研报摘要→TAMF第6章同步启动")
    try:
        result = sync_reports_to_tamf(days=7, limit=30)
        logger.info(
            f"TAMF第6章同步完成: 更新{result['targets_updated']}只标的, "
            f"处理{result['processed']}篇研报"
        )
        return result
    except Exception as e:
        logger.error(f"研报摘要→TAMF同步异常: {e}")
        try:
            from notification import send_notification
            send_notification("🔴 研报摘要→TAMF同步异常", f"错误: {e}", level="ERROR")
        except Exception:
            pass
        return {"processed": 0, "targets_updated": 0, "skipped": 0, "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = sync_reports_to_tamf(days=7, limit=30)
    print(f"同步结果: {result}")
