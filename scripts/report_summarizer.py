"""
report_summarizer.py — 研报/公告智能摘要模块
基于 LLM 对采集的研报和公告进行批量智能摘要生成
摘要注入 TAMF 记忆文件，支持仪表盘展示
"""

import logging
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

logger = logging.getLogger("invest_system.report_summarizer")

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",
}

MAX_REPORT_CHARS = 3000
SUMMARY_MAX_CHARS = 200


def _get_db_conn():
    """获取数据库连接"""
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


def summarize_report(title: str, content: str) -> str:
    """
    使用 LLM 对单篇研报生成摘要

    Args:
        title: 研报标题
        content: 研报正文（截断至 MAX_REPORT_CHARS）

    Returns:
        中文摘要（不超过 SUMMARY_MAX_CHARS 字）
    """
    if not content or not title:
        return ""

    truncated = content[:MAX_REPORT_CHARS]
    prompt = (
        f"请用不超过{SUMMARY_MAX_CHARS}字的中文总结以下研报的核心观点和投资建议。\n\n"
        f"标题: {title}\n\n"
        f"正文: {truncated}\n\n"
        "请输出: 1)核心观点 2)评级/目标价 3)主要风险"
    )

    try:
        from agent_interface import get_agent
        agent = get_agent()
        result = agent.chat(prompt, system="你是专业金融研报分析师，擅长提炼核心观点。")
        content = result.get("content", "")
        if result.get("error"):
            logger.warning(f"摘要生成失败: {result['error']}")
            return ""
        return content[:SUMMARY_MAX_CHARS * 2]
    except Exception as e:
        logger.warning(f"LLM 摘要异常: {e}")
        return ""


def summarize_reports_batch(days: int = 7, limit: int = 20) -> dict:
    """
    批量摘要生成：对近 N 天未生成摘要的研报进行 LLM 摘要

    Args:
        days: 回溯天数
        limit: 最大处理数量

    Returns:
        {"summarized": 处理数量, "success": 成功数量, "skipped": 跳过数量}
    """
    _ensure_summary_column()
    conn = _get_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute("""
            SELECT id, title, content, info_code
            FROM research.research_reports
            WHERE report_date >= %s
              AND (summary IS NULL OR summary = '')
            ORDER BY report_date DESC
            LIMIT %s
        """, (date.today() - timedelta(days=days), limit))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        logger.info("无待摘要研报")
        return {"summarized": 0, "success": 0, "skipped": 0}

    success = 0
    skipped = 0
    for row in rows:
        report_id = row["id"]
        title = row.get("title", "")
        content = row.get("content", "")

        if not content:
            skipped += 1
            continue

        summary = summarize_report(title, content)
        if not summary:
            skipped += 1
            continue

        # 存储摘要
        conn2 = _get_db_conn()
        cur2 = conn2.cursor()
        try:
            cur2.execute(
                "UPDATE research.research_reports SET summary = %s WHERE id = %s",
                (summary, report_id),
            )
            conn2.commit()
            success += 1
        except Exception as e:
            conn2.rollback()
            logger.warning(f"摘要存储失败 id={report_id}: {e}")
            skipped += 1
        finally:
            cur2.close()
            conn2.close()

    logger.info(f"研报摘要完成: {success} 成功, {skipped} 跳过")
    return {"summarized": len(rows), "success": success, "skipped": skipped}


def get_reports_with_summary(days: int = 7, limit: int = 30) -> list[dict]:
    """
    获取带摘要的研报列表

    Args:
        days: 回溯天数
        limit: 最大返回数量

    Returns:
        研报列表（含 summary 字段）
    """
    _ensure_summary_column()
    conn = _get_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT id, title, summary, report_date, org_name, info_code,
                   stock_name, rating, target_price
            FROM research.research_reports
            WHERE report_date >= %s
            ORDER BY report_date DESC
            LIMIT %s
        """, (date.today() - timedelta(days=days), limit))
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        cur.close()
        conn.close()


def summarize_announcements_batch(days: int = 7, limit: int = 20) -> dict:
    """
    批量摘要公告

    Args:
        days: 回溯天数
        limit: 最大处理数量

    Returns:
        {"summarized": 处理数量, "success": 成功数量, "skipped": 跳过数量}
    """
    conn = _get_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # 确保有 summary 列
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'research' AND table_name = 'announcements'
              AND column_name = 'summary'
        """)
        if not cur.fetchone():
            cur.execute("ALTER TABLE research.announcements ADD COLUMN summary TEXT")
            conn.commit()

        cur.execute("""
            SELECT id, title, ts_code, notice_date, ann_type
            FROM research.announcements
            WHERE notice_date >= %s
              AND (summary IS NULL OR summary = '')
            ORDER BY notice_date DESC
            LIMIT %s
        """, (date.today() - timedelta(days=days), limit))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        return {"summarized": 0, "success": 0, "skipped": 0}

    success = 0
    skipped = 0
    for row in rows:
        ann_id = row["id"]
        title = row.get("title", "")
        ann_type = row.get("ann_type", "")
        ts_code = row.get("ts_code", "")

        prompt = (
            f"请用一句话（不超过50字）总结以下公告的核心内容。\n\n"
            f"公告类型: {ann_type}\n"
            f"标题: {title}\n"
            f"股票: {ts_code}"
        )

        try:
            from agent_interface import get_agent
            agent = get_agent()
            result = agent.chat(prompt, system="你是专业金融分析师，擅长提炼公告要点。")
            summary = result.get("content", "")[:100]
            if not result.get("error") and summary:
                conn2 = _get_db_conn()
                cur2 = conn2.cursor()
                try:
                    cur2.execute(
                        "UPDATE research.announcements SET summary = %s WHERE id = %s",
                        (summary, ann_id),
                    )
                    conn2.commit()
                    success += 1
                except Exception as e:
                    conn2.rollback()
                    logger.warning(f"公告摘要存储失败: {e}")
                    skipped += 1
                finally:
                    cur2.close()
                    conn2.close()
            else:
                skipped += 1
        except Exception as e:
            logger.warning(f"公告摘要生成异常: {e}")
            skipped += 1

    return {"summarized": len(rows), "success": success, "skipped": skipped}


def inject_summaries_to_tamf(ts_code: str, summaries: list[str]) -> bool:
    """
    将研报摘要注入对应标的的 TAMF 文件

    Args:
        ts_code: 股票代码
        summaries: 摘要列表

    Returns:
        是否注入成功
    """
    from tamf_updater import get_tamf_path

    if not summaries:
        return False

    tamf_path = get_tamf_path(ts_code)
    if not tamf_path or not tamf_path.exists():
        return False

    try:
        content = tamf_path.read_text(encoding="utf-8")
    except Exception:
        return False

    section_header = "## 研报摘要 (自动生成)"
    if section_header in content:
        # 替换已有摘要
        parts = content.split(section_header)
        new_section = section_header + "\n\n" + "\n\n".join(
            f"- {s[:200]}" for s in summaries[:5]
        ) + "\n"
        new_content = parts[0] + new_section
        if len(parts) > 1 and "\n## " in parts[1]:
            after = parts[1].split("\n## ", 1)
            new_content += "\n## " + after[1] if len(after) > 1 else ""
    else:
        # 在文件末尾添加
        new_section = f"\n\n{section_header}\n\n" + "\n\n".join(
            f"- {s[:200]}" for s in summaries[:5]
        ) + "\n"
        new_content = content + new_section

    try:
        tamf_path.write_text(new_content, encoding="utf-8")
        logger.info(f"TAMF 摘要注入: {ts_code} ({len(summaries)} 条)")
        return True
    except Exception as e:
        logger.warning(f"TAMF 摘要注入失败 {ts_code}: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = summarize_reports_batch(days=7, limit=5)
    print(f"研报摘要: {result}")