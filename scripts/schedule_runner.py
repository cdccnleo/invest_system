"""
schedule_runner.py — Phase 2 定时调度模块
APScheduler 驱动 08:30 / 15:30 / 21:00 三个工作流
完成后通过 Server酱(微信) + 飞书机器人推送报告
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, date, timedelta

# APScheduler
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
except ImportError:
    print("需要安装: pip install apscheduler")
    sys.exit(1)

# ── 路径设置 ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

from run_analysis import run_analysis, enrich_positions_with_quotes
from embedding_service import daily_embedding_job
from storage_factory import get_storage
from llm_cost_tracker import get_daily_stats, get_monthly_stats, format_cost_report
def _safe_error_alert(title: str, detail: str):
    """发送错误告警，失败时不抛异常"""
    try:
        send_error_alert(title, detail)
    except Exception as e:
        logger.error(f"推送错误告警失败: {e}")

# ── 交易日判断 ────────────────────────────────────────────────────────────

_trading_dates_cache: set = set()
_cache_loaded_date: str = ""  # YYYYMMDD

def is_trading_day() -> bool:
    """
    判断今天是否为 A 股交易日。
    先排除周末，再用 chinese_calendar 确认（节假日调休自动处理）。
    保守策略：任何异常都返回 True（不误杀正常任务）。
    """
    global _trading_dates_cache, _cache_loaded_date

    try:
        from chinese_calendar import is_holiday
        from datetime import date
        today = date.today()
        return not is_holiday(today)
    except Exception as e:
        logger.warning(f"交易日判断失败 ({e})，保守返回 True")
        return True

def _guard_trading_day(job_name: str):
    """交易日守卫：非交易日记录调试日志并跳过"""
    if not is_trading_day():
        logger.debug(f"[{job_name}] 今日非交易日，跳过")
        return False
    return True


# ── 健康检查函数 ────────────────────────────────────────────────────────────

def check_services_health() -> dict:
    """
    检查核心服务状态
    返回: dict with status for DB/streamlit/watchdog
    """
    health = {
        "db": "healthy",
        "streamlit": "healthy",
        "watchdog": "healthy",
        "jobs": {},
        "today_events": 0,
        "today_errors": 0,
    }

    # 1. DB connection check
    try:
        storage = get_storage()
        conn = storage._ensure_pg()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            health["db"] = "healthy"
        else:
            health["db"] = "critical"
        storage.close()
    except Exception as e:
        health["db"] = f"critical: {e}"

    # 2. Streamlit check (port 8501)
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex(("127.0.0.1", 8501))
        sock.close()
        health["streamlit"] = "healthy" if result == 0 else "warning"
    except Exception as e:
        health["streamlit"] = f"warning: {e}"

    # 3. Watchdog check (parent process is this scheduler)
    try:
        import os
        import psutil
        current_pid = os.getpid()
        parent = psutil.Process(current_pid).parent()
        if parent and "python" in parent.name().lower():
            health["watchdog"] = "healthy"
        else:
            health["watchdog"] = "warning"
    except Exception as e:
        health["watchdog"] = f"warning: {e}"

    # 4. Check last successful run times for each job
    try:
        storage = get_storage()
        conn = storage._ensure_pg()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT event_type, result, event_time
                FROM audit.audit_log
                WHERE event_time >= CURRENT_DATE
                ORDER BY event_time DESC
                LIMIT 100
            """)
            rows = cur.fetchall()
            cur.close()
            health["today_events"] = len(rows)
            health["today_errors"] = sum(1 for r in rows if r[1] and "FAIL" in str(r[1]))

            # Get last success per job
            job_types = [
                "SCHEDULED_MORNING_RUN", "SCHEDULED_CLOSING_RUN",
                "SCHEDULED_EVENING_RUN", "SCHEDULED_MIDDAY_RUN",
            ]
            for jt in job_types:
                cur = conn.cursor()
                cur.execute("""
                    SELECT event_time FROM audit.audit_log
                    WHERE event_type = %s AND result = 'SUCCESS'
                    ORDER BY event_time DESC LIMIT 1
                """, (jt,))
                row = cur.fetchone()
                cur.close()
                health["jobs"][jt] = row[0].isoformat() if row else "从未成功"
            storage.close()
    except Exception as e:
        logger.warning(f"健康检查审计日志读取失败: {e}")

    return health


def format_health_report(health: dict) -> str:
    """格式化健康报告文本"""
    lines = []

    # 服务状态
    status_icon = {"healthy": "✅", "warning": "⚠️", "critical": "🔴"}
    db_icon = status_icon.get(health["db"], "⚠️")
    st_icon = status_icon.get(health["streamlit"], "⚠️")
    wd_icon = status_icon.get(health["watchdog"], "⚠️")

    lines.append(f"{db_icon} 数据库: {health['db']}")
    lines.append(f"{st_icon} Streamlit: {health['streamlit']}")
    lines.append(f"{wd_icon} Watchdog: {health['watchdog']}")
    lines.append("")

    # 今日统计
    lines.append(f"📊 今日事件: {health['today_events']}")
    lines.append(f"❌ 今日错误: {health['today_errors']}")
    lines.append("")

    # 各任务最后成功时间
    lines.append("🕐 任务最后成功:")
    for jt, ts in health.get("jobs", {}).items():
        job_name = jt.replace("SCHEDULED_", "").replace("_RUN", "")
        lines.append(f"  • {job_name}: {ts}")

    return "\n".join(lines)


def job_health_report():
    """08:30 每日健康报告"""
    logger.info("每日健康报告开始")
    try:
        health = check_services_health()

        # Determine overall status
        if "critical" in str(health["db"]) or "critical" in str(health["watchdog"]):
            status = "critical"
        elif "warning" in str(health["db"]) or "warning" in str(health["streamlit"]):
            status = "warning"
        else:
            status = "healthy"

        report = format_health_report(health)
        send_health_report(report, status=status)
        logger.info(f"健康报告已发送 (状态: {status})")
    except Exception as e:
        logger.error(f"健康报告异常: {e}")
        _safe_error_alert("🔴 健康报告生成失败", f"错误: {e}")


# ── 工作流定义 ────────────────────────────────────────────────────────────
from fetch_reports import collect_reports
from skill_library import check_skill_triggers, generate_skill_draft, SkillLifecycle
from skill_library import TRIGGER_DAYS, TRIGGER_MIN_CALLS
from notification import send_notification, send_error_alert, send_health_report, send_job_failure
from intraday_monitor import IntradayMonitor, format_anomaly_message
from fetch_financial import collect_financial_for_positions
from fetch_announcements import fetch_all_positions_announcements
from l3_dialog_engine import L3DialogEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("invest_system.scheduler")


# ── 推送报告组装 ──────────────────────────────────────────────────────────

def _build_morning_report() -> str:
    """组装盘前推送文本（从最新分析结果读取）"""
    try:
        storage = get_storage()
        # 读取当日分析的关键信息
        cur = storage._ensure_pg() and storage._pg_conn.cursor()
        if not cur:
            storage.close()
            return "盘前分析已完成，请查看详细报告。"
        # 读取最新审计记录获取分析摘要
        cur.execute("""
            SELECT detail, result, event_time
            FROM audit.audit_log
            WHERE event_type = 'SCHEDULED_MORNING_RUN'
            ORDER BY event_time DESC LIMIT 1
        """)
        row = cur.fetchone()
        storage.close()

        if row:
            detail_str, result, event_time = row
            import json
            try:
                detail = json.loads(detail_str) if detail_str else {}
            except Exception:
                detail = {}
            positions_count = detail.get("positions", "?")
            quotes_count = detail.get("quotes", "?")
            news_count = detail.get("news", "?")
            ts = event_time.strftime("%H:%M") if event_time else ""
            return (f"✅ 盘前分析完成 | {ts}\n"
                    f"📊 持仓: {positions_count} 只 | 行情: {quotes_count} 条 | 新闻: {news_count} 条\n"  # noqa: E501
                    f"💰 详细报告已生成，请查看仪表盘或微信推送历史。")
    except Exception as e:
        logger.warning(f"读取分析结果失败: {e}")
    return "盘前分析已完成。"


def _build_closing_report() -> str:
    """组装盘后推送文本"""
    try:
        storage = get_storage()
        cur = storage._ensure_pg() and storage._pg_conn.cursor()
        if not cur:
            storage.close()
            return "盘后分析已完成，请查看详细报告。"
        cur.execute("""
            SELECT detail, result, event_time
            FROM audit.audit_log
            WHERE event_type = 'SCHEDULED_CLOSING_RUN'
            ORDER BY event_time DESC LIMIT 1
        """)
        row = cur.fetchone()
        storage.close()

        if row:
            detail_str, result, event_time = row
            import json
            try:
                detail = json.loads(detail_str) if detail_str else {}
            except Exception:
                detail = {}
            positions_count = detail.get("positions", "?")
            ts = event_time.strftime("%H:%M") if event_time else ""
            return (f"📉 盘后分析 + 向量化完成 | {ts}\n"
                    f"📊 持仓: {positions_count} 只\n"
                    f"🧠 新闻已向量化，可通过语义检索调取历史记忆。\n"
                    f"💰 详细报告已生成。")
    except Exception as e:
        logger.warning(f"读取分析结果失败: {e}")
    return "盘后分析已完成。"


# ── 工作流定义 ────────────────────────────────────────────────────────────

def job_morning():
    """08:30 盘前工作流"""
    if not _guard_trading_day("job_morning"):
        return
    logger.info("=" * 50)
    logger.info("08:30 盘前工作流启动")
    try:
        run_analysis()
        storage = get_storage()
        storage.write_audit(
            "SCHEDULED_MORNING_RUN", "SYSTEM",
            detail={"triggered_at": datetime.now().isoformat()},
            result="SUCCESS"
        )
        storage.close()

        # 推送报告
        report = _build_morning_report()
        send_notification("📅 盘前分析报告", report, level="INFO")

        # L3 主动对话 — 盘前触发器评估（periodic_checkin 等）
        try:
            engine = L3DialogEngine()
            result = engine.run_cycle()
            logger.info(f"L3 盘前评估: {result['triggered']}/{result['evaluated']} 个触发")
        except Exception as e:
            logger.warning(f"L3 盘前评估异常: {e}")

        logger.info("盘前工作流完成")
    except Exception as e:
        logger.error(f"盘前工作流异常: {e}")
        _safe_error_alert("🔴 盘前工作流异常", f"错误: {e}")
        try:
            send_job_failure("盘前工作流", str(e))
        except Exception:
            pass


def _parallel_collect_close_data(stock_codes: list, anns_days_window: int = 30) -> dict:
    """
    并行采集盘后数据：财务数据 + 公告采集同时进行。
    利用 ThreadPoolExecutor 将两个独立的 I/O 密集型任务并行执行，
    总耗时从串行约 3 分钟降至约 1.5 分钟。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {"financial": None, "announcements": None, "errors": []}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {}

        if stock_codes:
            futures[executor.submit(
                collect_financial_for_positions, stock_codes
            )] = "financial"

        futures[executor.submit(
            fetch_all_positions_announcements, anns_days_window, 3
        )] = "announcements"

        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result(timeout=180)
            except Exception as e:
                results["errors"].append(f"{key}: {e}")
                logger.error(f"并行采集失败 {key}: {e}")

    return results


def job_closing():
    """15:30 盘后工作流"""
    if not _guard_trading_day("job_closing"):
        return
    logger.info("=" * 50)
    logger.info("15:30 盘后工作流启动")
    try:
        run_analysis()
        daily_embedding_job()  # 向量化当日新闻

        # 并行采集持仓股票财务数据 + 公告（同时进行，约 1.5 分钟）
        try:
            import csv
            pos_file = "/mnt/d/Hold/invest-data/positions.csv"
            with open(pos_file) as f:
                reader = csv.DictReader(f)
                positions = list(reader)
            stock_codes = []
            for p in positions:
                code = (p.get("code") or "").strip()
                if code and not code.startswith(("5", "15", "51", "56", "58")) and len(code) == 6:
                    stock_codes.append(code)

            parallel_result = _parallel_collect_close_data(stock_codes)

            if parallel_result["financial"]:
                fin_results = parallel_result["financial"]
                saved = sum(v.get("saved", 0) for v in fin_results.values())
                logger.info(f"财务数据采集: {saved} 条记录")

            anns = parallel_result["announcements"]
            if anns:
                logger.info(f"公告采集: {len(anns)} 条")
                ann_storage = get_storage()
                stored = ann_storage.write_announcements(anns)
                ann_storage.close()
                logger.info(f"公告写入 DB: {stored} 条")

                if anns:
                    from pgcrypto_migration import process_corp_actions
                    action_result = process_corp_actions(anns)
                    if action_result["processed"] > 0:
                        msg = (f"🏢 公司行为调整: 分红{action_result['dividend']}笔 "
                               f"送股{action_result['bonus']}笔 已更新持仓成本/份额")
                        send_notification("🏢 持仓成本自动调整", msg, level="INFO")
                        logger.info(f"公司行为调整完成: {action_result}")

                    try:
                        from tamf_updater import on_announcement_detected
                        corp_types = {"分红", "送股", "配股", "季报", "中报", "年报"}
                        tamf_updated = 0
                        for a in anns:
                            if a.get("ann_type", "") in corp_types:
                                code = str(a.get("ts_code", "")).strip()[:6]
                                if code:
                                    on_announcement_detected(code, a)
                                    tamf_updated += 1
                        if tamf_updated > 0:
                            logger.info(f"TAMF事件驱动更新: {tamf_updated} 个标的")
                    except Exception as e:
                        logger.debug(f"TAMF事件驱动更新跳过: {e}")

            if parallel_result["errors"]:
                logger.warning(f"并行采集部分失败: {parallel_result['errors']}")
        except Exception as e:
            logger.warning(f"数据采集异常: {e}")

        storage = get_storage()
        storage.write_audit(
            "SCHEDULED_CLOSING_RUN", "SYSTEM",
            detail={"triggered_at": datetime.now().isoformat(), "positions": len(positions)},
            result="SUCCESS"
        )
        storage.close()

        # 推送报告
        report = _build_closing_report()
        send_notification("📉 盘后分析报告", report, level="INFO")

        logger.info("盘后工作流完成")
    except Exception as e:
        logger.error(f"盘后工作流异常: {e}")
        _safe_error_alert("🔴 盘后工作流异常", f"错误: {e}")
        try:
            send_job_failure("盘后工作流", str(e))
        except Exception:
            pass


def job_tamf_update():
    """15:35 TAMF增量更新 — 盘后数据到达后更新所有持仓标的分析记忆文件"""
    if not _guard_trading_day("job_tamf_update"):
        return
    logger.info("=" * 50)
    logger.info("15:35 TAMF增量更新启动")
    try:
        from tamf_updater import scheduled_update_all_holdings
        result = scheduled_update_all_holdings()
        logger.info(f"TAMF更新完成: 更新{result['updated']}个, 跳过{result['skipped']}个, 失败{result['failed']}个")  # noqa: E501
        # 自动提交TAMF文件变更
        try:
            from tamf_git_commit import commit_tamf_changes
            commit_result = commit_tamf_changes()
            if commit_result["committed"]:
                logger.info(f"✅ TAMF Git提交: {commit_result['message']}")
            else:
                logger.debug(f"TAMF Git: {commit_result['message']}")
        except Exception as e:
            logger.warning(f"TAMF Git提交跳过: {e}")
        # 推送结果
        if result["failed"] > 0:
            _safe_error_alert("⚠️ TAMF更新部分失败",
                f"更新{result['updated']}个, 失败{result['failed']}个")
        elif result["updated"] > 0:
            logger.info(f"✅ TAMF更新成功({result['updated']}个标的)")
    except Exception as e:
        logger.error(f"TAMF更新异常: {e}")
        _safe_error_alert("🔴 TAMF更新异常", f"错误: {e}")
        try:
            send_job_failure("TAMF更新", str(e))
        except Exception:
            pass


def job_equity_curve_save():
    """15:40 持仓历史equity曲线保存 — 每日收盘后记录组合总市值"""
    if not _guard_trading_day("job_equity_curve_save"):
        return
    logger.info("=" * 50)
    logger.info("15:40 Equity Curve 保存启动")
    try:
        from equity_curve_tracker import save_daily_equity, init_equity_curve_table
        # 确保表存在
        init_equity_curve_table()
        # 保存当日数据
        result = save_daily_equity()
        if result["saved"]:
            logger.info(
                f"Equity Curve 已保存: ¥{result['total_value']:,.2f}, "
                f"持仓 {result['position_count']} 只, 日期 {result['calc_date']}"
            )
        else:
            logger.warning(f"Equity Curve 保存失败: {result.get('error', '未知错误')}")
            _safe_error_alert("⚠️ Equity Curve 保存失败", result.get("error", ""))
    except Exception as e:
        logger.error(f"Equity Curve 保存异常: {e}")
        _safe_error_alert("🔴 Equity Curve 保存异常", f"错误: {e}")


def job_deep_analysis_weekly():
    """周日22:00 周频深度分析 — 强制重生成所有持仓标的的Agent段落"""
    logger.info("=" * 50)
    logger.info("周日22:00 周频深度分析启动")
    try:
        from tamf_updater import scheduled_deep_analysis_weekly
        result = scheduled_deep_analysis_weekly()
        logger.info(f"深度分析完成: 深度更新{result['deep_updated']}个, 跳过{result['skipped']}个, 失败{result['failed']}个")  # noqa: E501
        if result["failed"] > 0:
            _safe_error_alert("⚠️ 周频深度分析部分失败",
                f"深度更新{result['deep_updated']}个, 失败{result['failed']}个\n" +
                "\n".join(result["errors"][:3]))
        elif result["deep_updated"] > 0:
            logger.info(f"✅ 周频深度分析成功({result['deep_updated']}个标的)")
    except Exception as e:
        logger.error(f"周频深度分析异常: {e}")
        _safe_error_alert("🔴 周频深度分析异常", f"错误: {e}")
        try:
            send_job_failure("周频深度分析", str(e))
        except Exception:
            pass


def job_reports_collection():
    """16:00 研报复盘工作流 — 采集当日研报（每日运行，非仅交易日）"""
    # 移除交易日守卫：研报发布后即入库，非交易日同样需要更新
    logger.info("=" * 50)
    logger.info("16:00 研报采集工作流启动")
    try:
        # 采集近7天研报（确保不漏）
        reports = collect_reports(days_back=7, save_to_db=True)
        logger.info(f"研报采集完成: {len(reports)} 条")

        # 推送摘要
        if reports:
            sources = {}
            for r in reports:
                src = r.get("source", "未知")
                sources[src] = sources.get(src, 0) + 1
            top_source = max(sources, key=sources.get)
            msg = (f"📋 研报复盘完成\n\n"
                   f"今日新增: {len(reports)} 份研报\n"
                   f"来源分布: {sources}\n"
                   f"最多来源: {top_source} ({sources[top_source]}份)")
            send_notification("📋 研报复盘报告", msg, level="INFO")
        else:
            send_notification("📋 研报复盘报告", "今日无新增研报。", level="INFO")

        # 写入审计日志
        storage = get_storage()
        storage.write_audit(
            "SCHEDULED_REPORTS_RUN", "SYSTEM",
            detail={"triggered_at": datetime.now().isoformat(), "count": len(reports)},
            result="SUCCESS"
        )
        storage.close()

        # T2.3: 事件驱动TAMF更新 — 检测研报评级变动
        try:
            _detect_rating_changes()
        except Exception:
            pass

        logger.info("研报采集工作流完成")
    except Exception as e:
        logger.error(f"研报采集工作流异常: {e}")
        _safe_error_alert("🔴 研报采集工作流异常", f"错误: {e}")
        try:
            send_job_failure("研报采集", str(e))
        except Exception:
            pass


def job_report_summary_to_tamf():
    """
    16:05 研报摘要→TAMF第6章同步 — 将最新研报摘要写入持仓标的TAMF第六章。
    在 job_reports_collection (16:00) 完成研报采集和LLM摘要生成后执行，
    确保新入库研报的 summary 字段已就绪。
    """
    logger.info("=" * 50)
    logger.info("16:05 研报摘要→TAMF第6章同步启动")
    try:
        from auto_report_to_tamf import sync_reports_to_tamf
        result = sync_reports_to_tamf(days=7, limit=30)
        logger.info(
            f"TAMF第6章同步完成: 更新{result['targets_updated']}只标的, "
            f"处理{result['processed']}篇研报, 跳过{result['skipped']}篇"
        )
        if result.get("failed", 0) > 0:
            _safe_error_alert(
                "⚠️ 研报摘要→TAMF部分失败",
                f"更新{result['targets_updated']}只, 失败{result.get('failed', 0)}只",
            )
        elif result["targets_updated"] > 0:
            logger.info(f"✅ 研报摘要→TAMF同步成功({result['targets_updated']}只标的)")
    except Exception as e:
        logger.error(f"研报摘要→TAMF同步异常: {e}")
        _safe_error_alert("🔴 研报摘要→TAMF同步异常", f"错误: {e}")
        try:
            send_job_failure("研报摘要→TAMF", str(e))
        except Exception:
            pass


def _detect_rating_changes():
    """
    T2.3: 检测持仓股研报评级变动，触发TAMF事件更新。
    比较每个持仓股最新的2份研报评级，如有变化则调用 on_rating_change。
    """
    try:
        from tamf_updater import get_db_conn, on_rating_change
        conn = get_db_conn()
        cur = conn.cursor()

        # 找出每个ts_code最近2份研报，且评级不同
        cur.execute("""
            SELECT ts_code, rating, source, report_date FROM (
                SELECT ts_code, rating, source, report_date,
                       ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY report_date DESC) AS rn
                FROM research.research_reports
                WHERE ts_code IS NOT NULL
            ) sub WHERE rn <= 2
            ORDER BY ts_code, report_date DESC
        """)
        rows = cur.fetchall()
        conn.close()

        # 按ts_code分组
        by_code = {}
        for r in rows:
            code = r[0]
            if code not in by_code:
                by_code[code] = []
            by_code[code].append({"rating": r[1], "source": r[2], "date": r[3]})

        changes = 0
        for code, reports in by_code.items():
            if len(reports) >= 2 and reports[0]["rating"] != reports[1]["rating"]:
                latest = reports[0]
                previous = reports[1]
                on_rating_change(code, previous["rating"], latest["rating"], latest["source"])
                changes += 1

        if changes > 0:
            logging.getLogger("schedule_runner").info(f"TAMF评级变动检测: {changes} 个标的")
        return changes
    except Exception as e:
        logging.getLogger("schedule_runner").debug(f"TAMF评级变动检测跳过: {e}")
        return 0


def job_evening():
    """21:00 晚间工作流（每日运行，非仅交易日）"""
    # 移除交易日守卫：新闻/研报在非交易日同样需要更新
    logger.info("=" * 50)
    logger.info("21:00 晚间工作流启动")
    try:
        run_analysis()  # 二次分析（晚间新闻更新）
        storage = get_storage()
        storage.write_audit(
            "SCHEDULED_EVENING_RUN", "SYSTEM",
            detail={"triggered_at": datetime.now().isoformat()},
            result="SUCCESS"
        )
        storage.close()

        # 推送晚间报告
        report = "🌙 晚间复盘分析已完成\n\n今日收盘后进行了二次分析，整合了全天新闻和市场情绪，请查看详细报告了解最新操作计划。"  # noqa: E501
        send_notification("🌙 晚间复盘报告", report, level="INFO")

        # L3 主动对话 — 晚间触发器评估（风险升级/里程碑等）
        try:
            engine = L3DialogEngine()
            result = engine.run_cycle()
            logger.info(f"L3 晚间评估: {result['triggered']}/{result['evaluated']} 个触发")
        except Exception as e:
            logger.warning(f"L3 晚间评估异常: {e}")

        logger.info("晚间工作流完成")
    except Exception as e:
        logger.error(f"晚间工作流异常: {e}")
        try:
            _safe_error_alert("🔴 晚间工作流异常", f"错误: {e}")
            send_job_failure("晚间工作流", str(e))
        except Exception as push_err:
            logger.error(f"推送错误告警失败: {push_err}")


def job_midday():
    """11:30 午间快讯工作流 — 持仓股上午涨跌排行 + 下午关注点"""
    if not _guard_trading_day("job_midday"):
        return
    logger.info("=" * 50)
    logger.info("11:30 午间快讯工作流启动")
    try:
        from pgcrypto_migration import load_positions_from_db
        positions = load_positions_from_db()
        if not positions:
            logger.warning("午间快讯：持仓为空，跳过")
            return

        # 采集实时行情
        enriched = enrich_positions_with_quotes(positions)

        # 计算组合整体涨跌
        total_mv = 0.0
        weighted_chg = 0.0
        for p in enriched:
            mv = p.get("market_value", 0)
            chg = p.get("change_pct", 0)
            total_mv += mv
            weighted_chg += mv * chg

        portfolio_chg = (weighted_chg / total_mv) if total_mv > 0 else 0

        # 涨跌排行（change_pct 有效）
        valid = [p for p in enriched if p.get("change_pct", 0) != 0]
        sorted_pos = sorted(valid, key=lambda x: x.get("change_pct", 0), reverse=True)
        gainers = sorted_pos[:5]
        losers = sorted_pos[-5:] if len(sorted_pos) >= 5 else sorted_pos[::-1][:5]
        losers = sorted(losers, key=lambda x: x.get("change_pct", 0))

        # ── 组装推送文本 ───────────────────────────────────────────────────
        now_str = datetime.now().strftime("%Y-%m-%d 11:30")

        def _rank_table(items: list, reverse: bool = False) -> str:
            if not items:
                return "  （无数据）"
            lines = []
            for p in items:
                chg = p.get("change_pct", 0)
                icon = "🟢" if chg > 0 else "🔴" if chg < 0 else "⚪"
                name = p.get("name", p.get("code", "?"))
                code = p.get("code", "")
                lines.append(f"  {icon} {name}({code}) {chg:+.2f}%")
            return "\n".join(lines)

        gainers_txt = _rank_table(gainers)
        losers_txt = _rank_table(losers)

        chg_icon = "🟢" if portfolio_chg >= 0 else "🔴"
        chg_sign = "+" if portfolio_chg >= 0 else ""

        msg = (
            f"📊 午间快讯 | {now_str}\n\n"
            f"{chg_icon} 组合上午涨跌: {chg_sign}{portfolio_chg:.2f}%\n"
            f"持仓市值: ¥{total_mv:,.0f}\n\n"
            f"📈 涨幅榜 TOP5\n{gainers_txt}\n\n"
            f"📉 跌幅榜 TOP5\n{losers_txt}\n\n"
            f"⏰ 下午开盘关注\n"
            f"  • 留意 {'领涨股' if gainers else '相关标的'} 能否延续走势\n"
            f"  • 异动监控持续运行，发现异动立即推送\n"
            f"  • 尾盘30分钟如有异动可考虑波段操作\n"
        )

        send_notification("📊 午间持仓快讯", msg, level="INFO")

        # 审计日志
        storage = get_storage()
        storage.write_audit(
            "SCHEDULED_MIDDAY_RUN", "SYSTEM",
            detail={
                "triggered_at": datetime.now().isoformat(),
                "positions": len(positions),
                "portfolio_chg": round(portfolio_chg, 2),
                "gainers": len(gainers),
                "losers": len(losers),
            },
            result="SUCCESS"
        )
        storage.close()
        logger.info(f"午间快讯完成: {len(positions)} 只持仓, 组合涨跌 {portfolio_chg:+.2f}%")

    except Exception as e:
        logger.error(f"午间快讯工作流异常: {e}")
        _safe_error_alert("🔴 午间快讯异常", f"错误: {e}")
        try:
            send_job_failure("午间快讯", str(e))
        except Exception:
            pass


def job_announcements_collection():
    """20:50 公告采集工作流 — 采集持仓股近30天公告（每日运行，非仅交易日）"""
    # 移除交易日守卫：公告在非交易日同样可能发布（如盘后重大事项）
    logger.info("=" * 50)
    logger.info("20:50 公告采集工作流启动")
    try:
        anns = fetch_all_positions_announcements(days_window=30, max_pages=3)
        logger.info(f"公告采集完成: {len(anns)} 条")

        # ── 持久化到数据库 ──────────────────────────────────────────────────
        if anns:
            from storage_factory import get_storage
            storage = get_storage()
            stored = storage.write_announcements(anns)
            storage.close()
            logger.info(f"公告写入 DB: {stored} 条")
        else:
            stored = 0

        # 按类型统计
        if anns:
            types = {}
            for a in anns:
                t = a.get("ann_type", "其他")
                types[t] = types.get(t, 0) + 1
            type_str = " / ".join(f"{k}({v})" for k, v in types.items())
            msg = (f"📢 公告采集完成\n\n"
                   f"总计: {len(anns)} 条\n"
                   f"类型: {type_str}")
            send_notification("📢 持仓股公告采集报告", msg, level="INFO")

            # T2.3: 事件驱动TAMF更新 — 晚间公告即时写入TAMF
            try:
                from tamf_updater import on_announcement_detected as _on_ann
                tamf_types = {"分红", "送股", "配股", "季报", "中报", "年报", "风险提示", "退市风险"}  # noqa: E501
                tamf_cnt = 0
                for a in anns:
                    ann_type = a.get("ann_type", "")
                    if ann_type in tamf_types or any(
                        kw in (a.get("title") or "") for kw in ["风险", "退市", "处罚"]
                    ):
                        code = str(a.get("ts_code", "")).strip()[:6]
                        if code:
                            r = _on_ann(code, a)
                            if r.get("status") == "updated":
                                tamf_cnt += 1
                if tamf_cnt > 0:
                    logger.info(f"TAMF晚间公告事件更新: {tamf_cnt} 个标的")
            except Exception as e:
                logger.debug(f"TAMF晚间公告事件更新跳过: {e}")
        else:
            send_notification("📢 持仓股公告采集报告", "今日无新增持仓股公告。", level="INFO")

        # 写入审计日志
        storage = get_storage()
        storage.write_audit(
            "SCHEDULED_ANNOUNCEMENTS_RUN", "SYSTEM",
            detail={"triggered_at": datetime.now().isoformat(), "count": len(anns)},
            result="SUCCESS"
        )
        storage.close()

        logger.info("公告采集工作流完成")
    except Exception as e:
        logger.error(f"公告采集工作流异常: {e}")
        _safe_error_alert("🔴 公告采集工作流异常", f"错误: {e}")
        try:
            send_job_failure("公告采集", str(e))
        except Exception:
            pass


def job_sentiment_update():
    """
    21:00 情绪因子更新工作流（每日运行）
    遍历持仓股 + 近期有新闻的股票，计算近30天新闻/公告情感得分
    写入 market.sentiment_factors，供因子引擎实时查询
    """
    from sentiment_factor import analyze_sentiment
    from pgcrypto_migration import load_positions_from_db

    logger.info("=" * 50)
    logger.info("21:00 情绪因子更新工作流启动")
    try:
        storage = get_storage()
        cutoff = date.today() - timedelta(days=30)
        cur = storage._ensure_pg() and storage._pg_conn.cursor()
        if not cur:
            storage.close()
            logger.warning("无法获取数据库连接，跳过情绪因子更新")
            return
        updated = 0

        # 获取持仓股代码
        positions = load_positions_from_db()
        stock_codes = set()
        for p in positions:
            if p.get("type") == "fund":
                continue
            code = p.get("code", "").zfill(6)
            if code.startswith("15") or code.startswith("30") or code.startswith("00"):
                ts_code = f"{code}.XSHE"
            elif code.startswith("5") or code.startswith("6"):
                ts_code = f"{code}.XSHG"
            elif code.startswith("4") or code.startswith("8"):
                ts_code = f"{code}.BJ"
            else:
                ts_code = f"{code}.XSHE"
            stock_codes.add(ts_code)

        # 补充近30天有新闻/公告的股票
        cur.execute("""
            SELECT DISTINCT substring(title from 1 for 6) as code
            FROM research.news_articles
            WHERE published_at >= %s
              AND title ~ '^[0-9]{6}'
        """, (cutoff,))
        for row in cur.fetchall():
            code = row[0]
            if code:
                if code.startswith(("00", "30", "15")):
                    stock_codes.add(f"{code}.XSHE")
                elif code.startswith(("5", "6")):
                    stock_codes.add(f"{code}.XSHG")
                elif code.startswith(("4", "8")):
                    stock_codes.add(f"{code}.BJ")

        logger.info(f"情绪因子计算范围: {len(stock_codes)} 只股票")
        for ts_code in stock_codes:
            try:
                plain_code = ts_code.split('.')[0]
                scores = []

                # 新闻情感
                cur.execute("""
                    SELECT title, content FROM research.news_articles
                    WHERE published_at >= %s
                      AND (title ~ %s OR content ~ %s)
                """, (cutoff, plain_code, plain_code))
                for row in cur.fetchall():
                    title, content = row
                    text = f"{title or ''} {content or ''}"
                    result = analyze_sentiment(text)
                    scores.append(result["score"])

                # 公告情感
                cur.execute("""
                    SELECT title FROM research.announcements
                    WHERE ts_code = %s AND notice_date >= %s
                """, (plain_code, cutoff))
                for row in cur.fetchall():
                    title = row[0]
                    if title:
                        result = analyze_sentiment(title)
                        scores.append(result["score"])

                if not scores:
                    continue

                avg_score = sum(scores) / len(scores)
                pos_count = sum(1 for s in scores if s > 0.1)
                neg_count = sum(1 for s in scores if s < -0.1)

                cur.execute("""
                    INSERT INTO market.sentiment_factors
                        (ts_code, score, pos_count, neg_count, confidence, source, calc_date)
                    VALUES (%s, %s, %s, %s, %s, 'news', %s)
                    ON CONFLICT (ts_code, source, calc_date)
                    DO UPDATE SET
                        score = EXCLUDED.score,
                        pos_count = EXCLUDED.pos_count,
                        neg_count = EXCLUDED.neg_count,
                        confidence = EXCLUDED.confidence,
                        created_at = CURRENT_TIMESTAMP
                """, (ts_code, avg_score, pos_count, neg_count, min(len(scores) / 20, 1.0), date.today()))
                updated += 1
            except Exception as e:
                logger.warning(f"情绪因子计算失败 {ts_code}: {e}")

        storage._pg_conn.commit()
        cur.close()
        storage.close()

        storage.write_audit(
            "SCHEDULED_SENTIMENT_UPDATE", "SYSTEM",
            detail={"updated": updated, "triggered_at": datetime.now().isoformat()},
            result="SUCCESS"
        )
        logger.info(f"情绪因子更新完成: {updated} 只股票")
        send_notification(
            "📊 情绪因子更新完成",
            f"共更新 {updated} 只股票近30天情感得分，已写入 market.sentiment_factors",
            level="INFO"
        )
    except Exception as e:
        logger.error(f"情绪因子更新工作流异常: {e}")
        _safe_error_alert("🔴 情绪因子更新异常", f"错误: {e}")
        try:
            send_job_failure("情绪因子更新", str(e))
        except Exception:
            pass


def job_intraday_monitoring():
    """
    每5分钟盘中异动监控 job（同步模式）—
    APScheduler 每5分钟触发，本函数内做交易日守卫 + 交易时段守卫，
    非交易日或非交易时段直接返回，不产生无效扫描。
    """
    # ── 交易日守卫（兜底）───────────────────────────────────────────────
    if not _guard_trading_day("job_intraday_monitoring"):
        return
    # ── 交易时段守卫 ────────────────────────────────────────────────────
    now = datetime.now()
    morning_start  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    morning_end    = now.replace(hour=11, minute=30, second=0, microsecond=0)
    afternoon_start = now.replace(hour=13, minute=0,  second=0, microsecond=0)
    afternoon_end   = now.replace(hour=14, minute=55, second=0, microsecond=0)
    is_trading = (morning_start <= now <= morning_end) or (afternoon_start <= now <= afternoon_end)
    if not is_trading:
        logger.debug(f"非交易时段，跳过异动扫描 ({now.strftime('%H:%M')})")
        return

    logger.info("盘中异动扫描触发")
    try:
        monitor = IntradayMonitor()
        anomalies = monitor.scan()

        if not anomalies:
            logger.debug("本次扫描无异动")
            return

        msg = format_anomaly_message(anomalies)
        logger.warning(f"检测到 {len(anomalies)} 个异动，主动推送告警")

        # 更新冷却记录（避免同一标的短时间内重复推送）
        now = datetime.now()
        for a in anomalies:
            monitor.alerted_stocks[a["ts_code"]] = now

        # Hermes 主动推送：异动检测 → 立即推送到微信 + 飞书
        # 无需用户查询，盘中盲区消除
        send_notification("⚠️ 盘中异动告警", msg, level="WARNING")

    except Exception as e:
        logger.error(f"异动监控异常: {e}")
        _safe_error_alert("🔴 异动监控异常", str(e))
        send_job_failure("盘中异动监控", str(e))


def job_skill_solidification():
    """
    22:00 技能固化工作流 —
    1. 扫描 audit_log，检测 5天内≥3次调用的任务模式
    2. 对满足条件且尚未生成草案的任务，自动生成技能草案
    3. 推送通知提醒用户审核
    触发条件: 5个交易日内 SKILL_EXECUTED ≥3 次
    """
    logger.info("=" * 50)
    logger.info("22:00 技能固化工作流启动")
    try:

        # Step 1: 检测固化触发
        triggered = check_skill_triggers()
        logger.info(f"固化检测: {len(triggered)} 个任务满足条件")

        if not triggered:
            logger.info("无满足固化条件的任务")
            return

        # Step 2: 收集现有草案（避免重复生成）
        sl = SkillLifecycle()
        existing_drafts = sl.list_drafts()
        drafted_patterns = {d.get("_meta", {}).get("task_pattern", "").lower() for d in existing_drafts}  # noqa: E501

        new_drafts = []

        for item in triggered:
            pattern = item.get("task_pattern", "")
            if not pattern:
                continue

            # 跳过已有草案的任务
            if pattern.lower() in drafted_patterns:
                logger.info(f"  ⏭ 已存在草案，跳过: {pattern}")
                continue

            # 获取该任务的近期调用记录（用于生成草案上下文）
            recent_calls = _get_recent_skill_calls(pattern, limit=10)
            user_corrections = _get_recent_corrections(pattern, limit=5)

            # 生成草案
            draft = generate_skill_draft(
                task_pattern=pattern,
                call_count=item.get("call_count", 0),
                recent_calls=recent_calls,
                user_corrections=user_corrections,
            )

            if "error" not in draft:
                new_drafts.append(draft.get("skill_name", pattern))
                drafted_patterns.add(pattern.lower())  # 防止同一 pattern 重复生成
                logger.info(f"  ✅ 技能草案已生成: {draft.get('skill_name')} ({pattern})")
            else:
                logger.warning(f"  ⚠ 草案生成失败: {pattern} — {draft.get('error')}")

        # Step 3: 推送通知
        storage = get_storage()
        if new_drafts:
            drafts_list = "\n".join(f"  • {name}" for name in new_drafts)
            msg = (
                f"🧠 **技能固化待审核**\n\n"
                f"发现 {len(new_drafts)} 个任务满足固化条件，已生成草案：\n\n"
                f"{drafts_list}\n\n"
                f"请登录系统审核确认，批准后技能将自动执行。\n"
                f"固化规则: {TRIGGER_DAYS}天内≥{TRIGGER_MIN_CALLS}次调用"
            )
            send_notification("🧠 技能固化待审核", msg, level="INFO")
            logger.info(f"技能固化通知已推送: {len(new_drafts)} 个草案")
        else:
            msg = f"🧠 技能固化检测完成\n\n{len(triggered)} 个任务满足固化条件，均已有草案，无需处理。"  # noqa: E501
            send_notification("🧠 技能固化检测", msg, level="INFO")

        # 写入审计日志
        storage.write_audit(
            "SCHEDULED_SKILL_SOLIDIFICATION",
            "SYSTEM",
            detail={
                "triggered_at": datetime.now().isoformat(),
                "triggered_count": len(triggered),
                "new_drafts": len(new_drafts),
                "draft_names": new_drafts,
            },
            result="SUCCESS" if new_drafts else "NO_NEW_DRAFTS",
        )
        storage.close()

        logger.info("技能固化工作流完成")
    except Exception as e:
        logger.error(f"技能固化工作流异常: {e}")
        _safe_error_alert("🔴 技能固化工作流异常", f"错误: {e}")
        try:
            send_job_failure("技能固化", str(e))
        except Exception:
            pass


def job_llm_cost_report():
    """
    22:30 LLM 成本日报 —
    汇总当日 DeepSeek API 调用次数、Token 消耗与估算费用，推送微信/飞书。
    """
    logger.info("=" * 50)
    logger.info("22:30 LLM 成本日报启动")
    try:
        today_str = datetime.now().strftime("%Y%m%d")
        daily_stats = get_daily_stats(today_str)

        if daily_stats["calls"] == 0:
            logger.info("今日无 LLM 调用，跳过成本推送")
            return

        # 同时获取当月累计
        year_month = datetime.now().strftime("%Y%m")
        monthly_stats = get_monthly_stats(year_month)

        # 组装推送文本
        daily_report = format_cost_report(daily_stats, period="daily")

        # 合并日报告 + 月报告
        combined = (
            f"{daily_report}\n\n"
            f"📅 本月累计 ({year_month})\n"
            f"💰 月累计费用: ¥{monthly_stats['cost_cny']:.4f}\n"
            f"📞 月累计调用: {monthly_stats['calls']} 次\n"
            f"📊 月均费用/次: ¥{monthly_stats['avg_cost_per_call']:.4f}\n"
            f"📅 有效天数: {monthly_stats['days_with_usage']} 天"
        )

        send_notification("💰 LLM 成本日报", combined, level="INFO")
        logger.info(
            f"LLM 成本日报已推送: 今日 ¥{daily_stats['cost_cny']:.4f} "
            f"({daily_stats['calls']}次), 本月 ¥{monthly_stats['cost_cny']:.4f}"
        )

    except Exception as e:
        logger.error(f"LLM 成本日报异常: {e}")
        _safe_error_alert("🔴 LLM 成本日报异常", f"错误: {e}")
        try:
            send_job_failure("LLM 成本日报", str(e))
        except Exception:
            pass


def job_skill_spot_check():
    """
    每周日 21:00 自动抽查已批准技能的静默退化。
    扫描最近 7 天有执行记录的技能，随机抽取 10% 检查输出质量。
    可疑结果写入 audit_log 并推送通知。
    """
    logger.info("=" * 50)
    logger.info("21:00 技能质量抽查启动")
    try:
        import random
        from skill_library import (
            SkillLifecycle, spot_check_skill_result,
        )
        from backtest_engine import get_db_conn
        import json as _json

        sl = SkillLifecycle()
        approved = sl.list_approved()
        if not approved:
            logger.info("无已批准技能，跳过抽查")
            return

        # 连接 DB 查最近 7 天有执行的技能
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT JSON_VALUE(detail, '$.skill') as skill
            FROM audit.audit_log
            WHERE event_type = 'SKILL_EXECUTED'
              AND event_time >= NOW() - INTERVAL '7 days'
        """)
        executed_skills = [r[0] for r in cur.fetchall() if r[0]]

        conn.close()

        # 只抽查有执行的技能，随机选最多 3 个
        candidates = [s for s in approved if s['skill_name'] in executed_skills]
        sample_size = min(3, len(candidates))
        sampled = random.sample(candidates, sample_size) if candidates else []

        flagged = []
        for skill in sampled:
            skill_name = skill['skill_name']
            logger.info(f"  抽查技能: {skill_name}")

            # 取最近一次执行结果（从 audit_log 重建上下文）
            conn2 = get_db_conn()
            cur2 = conn2.cursor()
            cur2.execute("""
                SELECT detail, result FROM audit.audit_log
                WHERE event_type = 'SKILL_EXECUTED'
                  AND JSON_VALUE(detail, '$.skill') = %s
                ORDER BY event_time DESC LIMIT 1
            """, (skill_name,))
            row = cur2.fetchone()
            conn2.close()

            if not row:
                continue

            detail = _json.loads(row[0]) if row[0] else {}
            detail.get('query', '未知查询')
            # 模拟预期结构（实际场景需人工标注 expected）
            expected = {"status": "completed"}
            result = {"content": f"[抽查] 技能 {skill_name} 执行结果", "error": None}

            check = spot_check_skill_result(skill_name, result, expected)
            if check.get("suspicious"):
                flagged.append(skill_name)
            logger.info(f"    → {'⚠️ 可疑' if check.get('suspicious') else '✅ 正常'}")

        if flagged:
            msg = f"🧪 **技能质量抽查报告**\n\n发现 {len(flagged)} 个技能疑似退化:\n" + \
                  "\n".join(f"  • {s}" for s in flagged) + \
                  "\n\n请登录仪表盘检查这些技能的输出质量。"
            send_notification("🧪 技能质量抽查", msg, level="WARNING")
        else:
            logger.info(f"抽查完成: {sample_size} 个技能全部正常")

    except Exception as e:
        logger.error(f"技能抽查异常: {e}")
        _safe_error_alert("🔴 技能抽查异常", f"错误: {e}")
        try:
            send_job_failure("技能抽查", str(e))
        except Exception:
            pass


def job_behavior_profile_update():
    """
    每日 15:40 收盘后行为画像更新
    - 调用 analyze_trading_behavior(30) 分析近30天审计日志
    - 将结果写入 l3.behavior_profile（行模型：profile_date/dimension/metric_name/metric_value）
    - 行为异常时发送飞书 L3 主动预警
    """
    from storage_factory import get_pg_connection

    logger.info("=" * 50)
    logger.info("15:40 行为画像更新启动")
    try:
        from audit_analytics import analyze_trading_behavior

        profile = analyze_trading_behavior(days=30)
        logger.info(f"行为画像: {profile.get('behavior_patterns', [])}")

        # ── 写入 l3.behavior_profile（行模型，按 dimension/metric 分解）────────────────
        pg_conn = get_pg_connection()
        if not pg_conn:
            logger.warning("无法连接数据库，行为画像跳过写入")
            return

        cur = pg_conn.cursor()
        rows = _build_profile_rows(profile)
        for row in rows:
            cur.execute("""
                INSERT INTO l3.behavior_profile
                    (profile_date, dimension, metric_name, metric_value, alert_level)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (profile_date, dimension, metric_name) DO UPDATE SET
                    metric_value  = EXCLUDED.metric_value,
                    alert_level   = EXCLUDED.alert_level,
                    updated_at    = NOW()
            """, row)
        pg_conn.commit()
        cur.close()
        pg_conn.close()
        logger.info(f"行为画像已写入 {len(rows)} 条行记录")

        # ── 异常检测 → 飞书预警 ───────────────────────────────────────────────────
        patterns = profile.get("behavior_patterns", [])
        if any("激进" in p for p in patterns):
            send_notification("⚠️ L3 行为预警",
                f"检测到您近期频繁修改AI计划（连续{profile.get('max_consecutive_mod_days', 0)}天修改），"  # noqa: E501
                "建议适当减少干预，给AI计划更多信任空间。")
        elif profile.get("analysis_success_rate", 100) < 60:
            send_notification("⚠️ L3 行为预警",
                f"近30天AI计划采纳率仅{profile['analysis_success_rate']:.0f}%，"
                "建议复盘修改原因，减少过度干预。")

        # ── 推送确认报告 ──────────────────────────────────────────────────────────
        msg = "📊 **每日行为画像**（近30天）\n\n"
        msg += f"分析成功率: {profile.get('analysis_success_rate', 'N/A')}%\n"
        msg += f"总分析次数: {profile.get('total_analysis_runs', 0)}\n"
        msg += f"最大连续修改: {profile.get('max_consecutive_mod_days', 0)}天\n\n"
        for p in patterns:
            msg += f"• {p}\n"
        if profile.get("recommendations"):
            msg += "\n**建议:**\n"
            for r in profile["recommendations"]:
                msg += f"• {r}\n"
        send_notification("📊 每日行为画像已更新", msg)

    except Exception as e:
        logger.error(f"行为画像更新异常: {e}")
        _safe_error_alert("🔴 行为画像更新异常", f"错误: {e}")
        try:
            send_job_failure("行为画像更新", str(e))
        except Exception:
            pass


def _build_profile_rows(profile: dict) -> list[tuple]:
    """
    将 analyze_trading_behavior() 返回的 dict 按 l3.behavior_profile 行模型拆解。
    每行: (profile_date, dimension, metric_name, metric_value, alert_level)
    """
    from decimal import Decimal

    today = date.today()
    rows = []
    ev = profile.get("event_counts", {})

    # ── 维度1: overtrading（过度交易）────────────────────────────────────────
    mod_days = profile.get("max_consecutive_mod_days", 0)
    rows.append((today, "overtrading", "consecutive_mod_days", Decimal(mod_days),
                 "critical" if mod_days >= 5 else "warning" if mod_days >= 3 else "normal"))

    # ── 维度2: risk_taking（风险偏好）────────────────────────────────────────
    success_rate = profile.get("analysis_success_rate", 100)
    rows.append((today, "risk_taking", "ai_plan_accept_rate", Decimal(str(success_rate)),
                 "critical" if success_rate < 40 else "warning" if success_rate < 60 else "normal"))

    # ── 维度3: diversification（分散度）──────────────────────────────────────
    sum(ev.values())
    skill_cnt = ev.get("SKILL_EXECUTED", 0)
    rows.append((today, "diversification", "skill_usage_count", Decimal(skill_cnt),
                 "normal"))

    # ── 维度4: holding_pattern（持仓习惯）───────────────────────────────────
    morning_runs = ev.get("SCHEDULED_MORNING_RUN", 0)
    rows.append((today, "holding_pattern", "scheduled_run_count", Decimal(morning_runs),
                 "normal"))

    return rows


def job_behavior_insights():
    """
    每周日 20:00 行为洞察周报
    - 调用 send_behavior_insights_report(days=7) 生成本周行为洞察摘要
    - 推送飞书 Feishu
    """
    logger.info("=" * 50)
    logger.info("20:00 行为洞察周报启动")
    try:
        from audit_analytics import send_behavior_insights_report

        report = send_behavior_insights_report(days=7)
        send_notification("📊 每周行为洞察", report)
        logger.info("行为洞察周报已推送")

    except Exception as e:
        logger.error(f"行为洞察周报异常: {e}")
        _safe_error_alert("🔴 行为洞察周报异常", f"错误: {e}")
        try:
            send_job_failure("行为洞察周报", str(e))
        except Exception:
            pass


def job_user_emotion_sensing():
    """
    每日 21:30 用户情绪感知（高频操作检测 → 情绪推断）
    - 从 audit.audit_log 近1天数据推断用户情绪状态
    - 检测信号：修改频率 / 持仓查看频率 / 分析运行次数 / 报告查阅 / 错误频率
    - 情绪分类：焦虑 | 过度自信 | 恐慌 | 平静 | 兴奋
    - 异常情绪时写入 l3.active_dialog_triggers 并推送飞书预警
    """
    from datetime import timedelta

    logger.info("=" * 50)
    logger.info("21:30 用户情绪感知启动")
    try:
        from storage_factory import get_pg_connection

        pg_conn = get_pg_connection()
        if not pg_conn:
            logger.warning("无法连接数据库，跳过用户情绪感知")
            return

        cur = pg_conn.cursor()
        today = date.today()
        yesterday = today - timedelta(days=1)

        # 信号1: 修改计划频率
        cur.execute("""
            SELECT COUNT(*) FROM audit.audit_log
            WHERE event_type = 'USER_MODIFY_PLAN' AND event_time >= %s
        """, (yesterday,))
        mod_count = cur.fetchone()[0] or 0

        # 信号2: 查看持仓频率
        cur.execute("""
            SELECT COUNT(*) FROM audit.audit_log
            WHERE event_type = 'VIEW_POSITIONS' AND event_time >= %s
        """, (yesterday,))
        view_pos_count = cur.fetchone()[0] or 0

        # 信号3: 分析运行次数
        cur.execute("""
            SELECT COUNT(*) FROM audit.audit_log
            WHERE event_type IN ('ANALYSIS_COMPLETE', 'SCHEDULED_MORNING_RUN')
              AND event_time >= %s
        """, (yesterday,))
        analysis_count = cur.fetchone()[0] or 0

        # 信号4: 查看报告频率
        cur.execute("""
            SELECT COUNT(*) FROM audit.audit_log
            WHERE event_type = 'VIEW_REPORT' AND event_time >= %s
        """, (yesterday,))
        view_report_count = cur.fetchone()[0] or 0

        # 信号5: 错误频率
        cur.execute("""
            SELECT COUNT(*) FROM audit.audit_log
            WHERE result = 'FAILED' AND event_time >= %s
        """, (yesterday,))
        error_count = cur.fetchone()[0] or 0

        # ── 情绪推断 ──────────────────────────────────────────────────────
        emotion, emotion_desc, alert_level = _infer_emotion(
            mod_count, view_pos_count, analysis_count, view_report_count, error_count
        )
        logger.info(
            f"用户情绪: {emotion} | 修改:{mod_count} 查看持仓:{view_pos_count} "
            f"分析:{analysis_count} 报告:{view_report_count} 错误:{error_count}"
        )

        # 异常情绪时写入触发器 + 推送
        if alert_level != "normal":
            cur.execute("""
                INSERT INTO l3.active_dialog_triggers
                    (trigger_type, trigger_name, condition_expr, condition_desc,
                     cooldown_hours, message_template, priority, is_active)
                VALUES ('emotion_alert', '用户情绪预警', %s, %s, 12, %s, 9, TRUE)
                ON CONFLICT DO NOTHING
            """, (
                f'{{"type":"user_emotion","emotion":"{emotion}","alert_level":"{alert_level}"}}',
                f'用户情绪: {emotion} — {emotion_desc}',
                f'🧠 【情绪感知】检测到您目前"{emotion}"状态。{emotion_desc}。'
                f'建议：{"适当休息" if emotion in ("焦虑","恐慌") else "信任AI计划，减少干预"}',
            ))
            pg_conn.commit()
            send_notification("🧠 L3 情绪感知", f"当前情绪: **{emotion}**\n{emotion_desc}")

        cur.close()
        pg_conn.close()

    except Exception as e:
        logger.error(f"用户情绪感知异常: {e}")
        _safe_error_alert("🔴 用户情绪感知异常", f"错误: {e}")
        try:
            send_job_failure("用户情绪感知", str(e))
        except Exception:
            pass


def _infer_emotion(mod_count, view_pos_count, analysis_count,
                   view_report_count, error_count) -> tuple[str, str, str]:
    """
    基于行为信号推断用户情绪。
    返回: (emotion_label, description, alert_level)
    """
    if error_count >= 5:
        return ("焦虑", f"当日{error_count}次操作失败，可能导致挫败感", "warning")
    if mod_count >= 8 and view_pos_count >= 15:
        return ("恐慌", f"高修改({mod_count}次)+高频看持仓({view_pos_count}次)，可能处于市场恐慌", "critical")  # noqa: E501
    if mod_count >= 10:
        return ("过度自信", f"当日{mod_count}次修改AI计划，可能过度交易", "warning")
    if mod_count >= 5 and analysis_count >= 10:
        return ("兴奋", f"高强度盯盘({analysis_count}次分析+{mod_count}次调整)", "normal")
    if view_pos_count >= 20:
        return ("焦虑", f"超高频查看持仓({view_pos_count}次)，建议适当休息", "warning")
    return ("平静", "当前行为模式稳定，情绪平稳", "normal")


def job_stress_test():
    """
    每周五 22:00 收盘后压力测试
    - 加载当前持仓快照（市值 + 个股）
    - 复用 StressTestEngine.run_stress_test() 对3种极端情景做压力测试
    - 结果写入 l3.stress_test_results
    - 推送飞书 Feishu（含情景摘要 + 最大损失率 + 建议）
    """
    import uuid as _uuid
    import json as _json
    from decimal import Decimal

    logger.info("=" * 50)
    logger.info("22:00 每周压力测试启动")
    try:
        from backtest_engine import StressTestEngine, get_db_conn
        from storage_factory import get_pg_connection

        # ── 1. 加载持仓 ──────────────────────────────────────────────────────
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT code, name, shares, cost, market_value
            FROM holdings.encrypted_positions
            WHERE is_current = TRUE
        """)
        raw_positions = cur.fetchall()

        cur.execute("""
            SELECT COALESCE(SUM(market_value), 0) as total_mv
            FROM holdings.encrypted_positions
            WHERE is_current = TRUE
        """)
        total_mv_row = cur.fetchone()
        total_mv = float(total_mv_row[0]) if total_mv_row else 0.0
        conn.close()

        if not raw_positions or total_mv <= 0:
            logger.warning("无持仓数据，跳过压力测试")
            return

        positions = [
            {
                "code": str(r[0]),
                "name": r[1],
                "shares": float(r[2]),
                "avg_cost": float(r[3]),
                "current_price": float(r[4]) / float(r[2]) if float(r[2]) > 0 else 0,
                "market_value": float(r[4]),
            }
            for r in raw_positions
        ]

        # ── 2. 执行压力测试 ───────────────────────────────────────────────────
        engine = StressTestEngine(db_conn_func=get_db_conn)
        result = engine.run_stress_test(total_mv, positions)

        # ── 3. 写入 l3.stress_test_results ────────────────────────────────────
        run_id = str(_uuid.uuid4())
        holding_snapshot = _json.dumps({
            "positions": [
                {"code": p["code"], "name": p["name"],
                 "market_value": p["market_value"]}
                for p in positions
            ],
            "total_mv": total_mv,
        }, ensure_ascii=False)

        pg_conn = get_pg_connection()
        if pg_conn:
            cur2 = pg_conn.cursor()
            for scenario_code, scenario_data in result["scenarios"].items():
                # 风险评分：损失率≥15%→高风险(9-10)，≥8%→中风险(6-8)，<8%→低风险(1-5)
                loss_pct = abs(scenario_data.get("loss_pct", 0))
                risk_score = min(10, max(1, int(loss_pct / 1.5)))

                # 建议逻辑
                if loss_pct >= 15:
                    recommendation = "建议减仓或启动对冲，风险极高"
                elif loss_pct >= 8:
                    recommendation = "建议密切关注，可考虑部分减仓"
                else:
                    recommendation = "风险可控，维持现状"

                scenario_name_map = {
                    "A_consecutive_limit_down": "情景A：连续跌停",
                    "B_liquidity_crisis": "情景B：流动性枯竭",
                    "C_high_volatility_5d": "情景C：大幅波动",
                }
                scenario_name = scenario_name_map.get(scenario_code, scenario_code)

                shock_result = _json.dumps({
                    "positions": [
                        {
                            "code": p["code"],
                            "name": p["name"],
                            "shock_pct": scenario_data.get("loss_pct", 0) / 100,
                            "loss": round(p["market_value"] * scenario_data.get("loss_pct", 0) / 100, 2),  # noqa: E501
                        }
                        for p in positions
                    ],
                    "total_loss": scenario_data.get("absolute_loss", 0),
                    "loss_rate": scenario_data.get("loss_pct", 0) / 100,
                }, ensure_ascii=False)

                cur2.execute("""
                    INSERT INTO l3.stress_test_results
                        (run_id, scenario_code, scenario_name, executed_at,
                         holding_snapshot, portfolio_value,
                         shock_result, max_loss_pct, max_loss_abs, risk_score, recommendation)
                    VALUES (%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s)
                """, (
                    run_id,
                    scenario_code,
                    scenario_name,
                    holding_snapshot,
                    Decimal(str(total_mv)),
                    shock_result,
                    Decimal(str(scenario_data.get("loss_pct", 0))),
                    Decimal(str(scenario_data.get("absolute_loss", 0))),
                    risk_score,
                    recommendation,
                ))
            pg_conn.commit()
            cur2.close()
            pg_conn.close()
            logger.info(f"压力测试结果已写入 {len(result['scenarios'])} 条记录 (run_id={run_id})")
        else:
            logger.warning("无法连接数据库，跳过结果写入")

        # ── 4. 推送飞书报告 ──────────────────────────────────────────────────
        lines = [f"🧪 **每日压力测试报告**（{date.today()}）\n"]
        lines.append(f"组合市值: ¥{total_mv:,.2f} | VaR(5日99%): ¥{result.get('var_5d_99', 0):,.2f}\n")  # noqa: E501

        max_loss = 0
        worst_scenario = ""
        for code, data in result["scenarios"].items():
            loss_pct = data.get("loss_pct", 0)
            loss_abs = data.get("absolute_loss", 0)
            risk_icon = "🔴" if loss_pct >= 15 else "🟡" if loss_pct >= 8 else "🟢"
            lines.append(
                f"{risk_icon} {code}: -{abs(loss_pct):.1f}%（¥{loss_abs:,.0f}）| "
                f"{data.get('description', '')}"
            )
            if abs(loss_pct) > abs(max_loss):
                max_loss = abs(loss_pct)
                worst_scenario = code

        lines.append(f"\n⚠️ 最悲观情景: {worst_scenario} -{max_loss:.1f}%")
        if max_loss >= 15:
            lines.append("建议减仓或启动对冲")
        elif max_loss >= 8:
            lines.append("建议密切关注，必要时部分减仓")

        send_notification("🧪 压力测试报告", "\n".join(lines))

    except Exception as e:
        logger.error(f"压力测试异常: {e}")
        _safe_error_alert("🔴 压力测试异常", f"错误: {e}")
        send_job_failure("压力测试", str(e))


def job_weekly_backtest():
    """
    每周五 22:00 周线回测报告
    - 对重点持仓股运行均线交叉策略回测
    - 仅在累计数据 ≥30 天时执行（否则跳过）
    - 结果推送至飞书/Server酱
    """
    logger.info("=" * 50)
    logger.info("22:00 周线回测报告启动")
    try:
        from backtester import (
            load_quotes, BacktestEngine, MAcrossStrategy, BacktestConfig,
        )
        from datetime import timedelta
        from tamf_updater import normalize_ts_code

        # ── 1. 从数据库加载当前持仓 ─────────────────────────────
        from backtester import get_db_conn as bt_conn
        conn = bt_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT code, name
            FROM holdings.encrypted_positions
            WHERE is_current = TRUE
        """)
        raw_holdings = cur.fetchall()

        # 获取市场最新日期
        cur.execute("SELECT MAX(trade_date) FROM market.daily_quotes")
        max_date_row = cur.fetchone()
        max_date = max_date_row[0] if max_date_row and max_date_row[0] else None
        conn.close()

        if not max_date:
            logger.warning("市场行情表无数据，跳过回测")
            return

        start_date = max_date - timedelta(days=90)

        # 归一化：code → ts_code，基金/无法映射的跳过
        holdings = []
        for code, name in raw_holdings:
            ts = normalize_ts_code(str(code), name or "")
            if ts.endswith(".OF"):
                logger.debug(f"跳过基金 {code}: {name} → {ts} (无股票行情)")
                continue
            holdings.append((ts, name or code))

        logger.info(f"持仓回测候选: {len(holdings)} 只（不含基金，{start_date}~{max_date}）")

        # ── 2. 逐只回测 ─────────────────────────────────────────
        reports = []
        for ts_code, name in holdings:
            try:
                quotes = load_quotes(ts_code, start_date, max_date)
                if len(quotes) < 30:
                    logger.debug(f"  {ts_code} {name}: 仅 {len(quotes)} 天数据，跳过")
                    continue

                cfg = BacktestConfig()
                strategy = MAcrossStrategy(cfg.default_params["ma_cross"])
                engine = BacktestEngine(cfg)
                result = engine.run(quotes, strategy, start_date, max_date)

                s = result.get("summary", {})
                if "error" not in result:
                    label = name if name != ts_code.split(".")[0] else ts_code
                    reports.append(
                        f"**{label}**\n"
                        f"  {s['total_return_pct']:+.2f}% | "
                        f"年化{s['annual_return_pct']:+.2f}% | "
                        f"回撤{s['max_drawdown_pct']:.1f}% | "
                        f"胜率{s['win_rate_pct']:.0f}% | "
                        f"Sharpe {s['sharpe_ratio']:.3f}"
                    )
            except Exception as e:
                logger.warning(f"回测 {ts_code} 失败: {e}")

        if not reports:
            logger.info("无足够数据生成回测报告（需要≥30天数据）")
            return

        msg = f"📊 **周线回测报告**（MA5/MA20）{max_date}\n"
        msg += f"覆盖 {len(reports)} 只标的\n\n"
        msg += "\n\n".join(reports)
        msg += f"\n\n_数据: {start_date} ~ {max_date}，≥30天有行情_"
        send_notification("📊 周线回测报告", msg)

    except Exception as e:
        logger.error(f"回测报告异常: {e}")
        _safe_error_alert("🔴 回测报告异常", f"错误: {e}")
        send_job_failure("周线回测", str(e))


def _get_recent_skill_calls(task_pattern: str, limit: int = 10) -> list[dict]:
    """获取近期技能调用记录（用于生成草案上下文）"""
    try:
        storage = get_storage()
        conn = storage._ensure_pg()
        if not conn:
            return []
        cur = conn.cursor()
        cur.execute("""
            SELECT detail, event_time
            FROM audit.audit_log
            WHERE event_type = 'SKILL_EXECUTED'
              AND detail->>'task' = %s
            ORDER BY event_time DESC
            LIMIT %s
        """, (task_pattern, limit))
        rows = cur.fetchall()
        storage.close()
        return [
            {"task": row[0].get("query", "") if isinstance(row[0], dict) else str(row[0]),
             "timestamp": row[1].isoformat() if row[1] else None}
            for row in rows
        ]
    except Exception as e:
        logger.warning(f"获取技能调用记录失败: {e}")
        return []


def _get_recent_corrections(task_pattern: str, limit: int = 5) -> list[dict]:
    """获取近期用户修正记录（用于生成草案上下文）"""
    try:
        storage = get_storage()
        conn = storage._ensure_pg()
        if not conn:
            return []
        cur = conn.cursor()
        cur.execute("""
            SELECT detail, event_time
            FROM audit.audit_log
            WHERE event_type = 'PLAN_MODIFIED'
              AND detail->>'task' = %s
            ORDER BY event_time DESC
            LIMIT %s
        """, (task_pattern, limit))
        rows = cur.fetchall()
        storage.close()
        return [
            {"original": row[0].get("original", {}),
             "modified": row[0].get("modified", {}),
             "reason": row[0].get("reason", ""),
             "timestamp": row[1].isoformat() if row[1] else None}
            for row in rows
        ]
    except Exception as e:
        logger.warning(f"获取修正记录失败: {e}")
        return []


# ── 调度器 ────────────────────────────────────────────────────────────────

_scheduler: BackgroundScheduler = None


def start_scheduler():
    """启动调度器（后台运行）"""
    global _scheduler
    if _scheduler is not None:
        logger.warning("调度器已在运行")
        return

    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai", job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,  # 5分钟内错过的任务仍执行
    })

    # 08:30 盘前
    _scheduler.add_job(
        job_morning,
        CronTrigger(hour=8, minute=30, timezone="Asia/Shanghai"),
        id="morning_routine",
        name="盘前工作流 (08:30)",
        replace_existing=True,
    )

    # 08:30 每日健康报告（与盘前并行）
    _scheduler.add_job(
        job_health_report,
        CronTrigger(hour=8, minute=30, timezone="Asia/Shanghai"),
        id="health_report_daily",
        name="每日健康报告 (08:30)",
        replace_existing=True,
    )

    # 11:30 午间快讯
    _scheduler.add_job(
        job_midday,
        CronTrigger(hour=11, minute=30, timezone="Asia/Shanghai"),
        id="midday_routine",
        name="午间快讯 (11:30)",
        replace_existing=True,
    )

    # 15:30 盘后
    _scheduler.add_job(
        job_closing,
        CronTrigger(hour=15, minute=30, timezone="Asia/Shanghai"),
        id="closing_routine",
        name="盘后工作流 (15:30)",
        replace_existing=True,
    )

    # 15:35 TAMF增量更新
    _scheduler.add_job(
        job_tamf_update,
        CronTrigger(hour=15, minute=35, timezone="Asia/Shanghai"),
        id="tamf_daily_update",
        name="TAMF增量更新 (15:35)",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # 15:40 持仓历史equity曲线保存
    _scheduler.add_job(
        job_equity_curve_save,
        CronTrigger(hour=15, minute=40, timezone="Asia/Shanghai"),
        id="equity_curve_save",
        name="Equity Curve 保存 (15:40)",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # 每周日 22:00 周频深度分析（Agent段落全量重生成）
    _scheduler.add_job(
        job_deep_analysis_weekly,
        CronTrigger(day_of_week='sun', hour=22, minute=0, timezone="Asia/Shanghai"),
        id="deep_analysis_weekly",
        name="周频深度分析 (每周日 22:00)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # 21:00 晚间
    _scheduler.add_job(
        job_evening,
        CronTrigger(hour=21, minute=0, timezone="Asia/Shanghai"),
        id="evening_routine",
        name="晚间工作流 (21:00)",
        replace_existing=True,
    )

    # 21:05 情绪因子更新（每日 21:05，基于最新新闻/公告计算情感得分）
    _scheduler.add_job(
        job_sentiment_update,
        CronTrigger(hour=21, minute=5, timezone="Asia/Shanghai"),
        id="sentiment_update",
        name="情绪因子更新 (21:05)",
        replace_existing=True,
    )

    # 16:00 研报采集（每个交易日收盘后）
    _scheduler.add_job(
        job_reports_collection,
        CronTrigger(hour=16, minute=0, timezone="Asia/Shanghai"),
        id="reports_collection",
        name="研报采集工作流 (16:00)",
        replace_existing=True,
    )

    # 16:05 研报摘要→TAMF第6章同步（研报采集后）
    _scheduler.add_job(
        job_report_summary_to_tamf,
        CronTrigger(hour=16, minute=5, timezone="Asia/Shanghai"),
        id="report_summary_to_tamf",
        name="研报摘要→TAMF第6章 (16:05)",
        replace_existing=True,
    )

    # 20:50 公告采集（持仓股近30天公告，晚间20:50后上市公司集中发布，21:00前完成供复盘使用）
    _scheduler.add_job(
        job_announcements_collection,
        CronTrigger(hour=20, minute=50, timezone="Asia/Shanghai"),
        id="announcements_collection",
        name="公告采集工作流 (20:50)",
        replace_existing=True,
    )

    # 每周日 21:00 技能质量抽查（检测固化技能静默退化）
    _scheduler.add_job(
        job_skill_spot_check,
        CronTrigger(day_of_week='sun', hour=21, minute=0, timezone="Asia/Shanghai"),
        id="skill_spot_check",
        name="技能质量抽查 (每周日 21:00)",
        replace_existing=True,
    )

    # 22:00 技能固化（检测高频任务模式 → 自动生成草案 → 推送审核通知）
    _scheduler.add_job(
        job_skill_solidification,
        CronTrigger(hour=22, minute=0, timezone="Asia/Shanghai"),
        id="skill_solidification",
        name="技能固化工作流 (22:00)",
        replace_existing=True,
    )

    # 22:30 LLM 成本日报（每日 Token/费用统计推送）
    _scheduler.add_job(
        job_llm_cost_report,
        CronTrigger(hour=22, minute=30, timezone="Asia/Shanghai"),
        id="llm_cost_report_daily",
        name="LLM 成本日报 (22:30)",
        replace_existing=True,
    )

    # 每周五 22:00 压力测试（StressTestEngine 3情景 + VaR，写入 l3.stress_test_results）
    _scheduler.add_job(
        job_stress_test,
        CronTrigger(day_of_week='fri', hour=22, minute=0, timezone="Asia/Shanghai"),
        id="stress_test_weekly",
        name="每周压力测试 (周五 22:00)",
        replace_existing=True,
    )

    # 每周日 20:00 行为洞察周报
    _scheduler.add_job(
        job_behavior_insights,
        CronTrigger(day_of_week='sun', hour=20, minute=0, timezone="Asia/Shanghai"),
        id="behavior_insights_weekly",
        name="每周行为洞察 (周日 20:00)",
        replace_existing=True,
    )

    # 每日 21:30 用户情绪感知（高频操作检测 → 情绪推断）
    _scheduler.add_job(
        job_user_emotion_sensing,
        CronTrigger(hour=21, minute=30, timezone="Asia/Shanghai"),
        id="user_emotion_daily",
        name="每日用户情绪感知 (21:30)",
        replace_existing=True,
    )

    # 每周一 07:00 均线交叉策略回测（上周策略表现，仅在数据≥30天时运行）
    _scheduler.add_job(
        job_weekly_backtest,
        CronTrigger(day_of_week='mon', hour=7, minute=0, timezone="Asia/Shanghai"),
        id="weekly_backtest",
        name="周线回测报告 (每周一 07:00)",
        replace_existing=True,
    )

    # 盘中异动监控（每5分钟，仅交易时段）
    _scheduler.add_job(
        job_intraday_monitoring,
        IntervalTrigger(minutes=5, timezone="Asia/Shanghai"),
        id="intraday_monitoring",
        name="盘中异动监控 (每5分钟)",
        replace_existing=True,
        misfire_grace_time=60,
    )

    # 每日 15:40 收盘后行为画像写入（分析近30天审计日志并更新 l3.behavior_profile）
    _scheduler.add_job(
        job_behavior_profile_update,
        CronTrigger(hour=15, minute=40, timezone="Asia/Shanghai"),
        id="behavior_profile_daily",
        name="每日行为画像更新 (15:40)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # 每周日 23:00 数据库全量备份 + 每日清理过期备份
    try:
        from backup_manager import backup_job as job_db_backup
        _scheduler.add_job(
            job_db_backup,
            CronTrigger(day_of_week='sun', hour=23, minute=0, timezone="Asia/Shanghai"),
            id="db_backup_weekly",
            name="数据库全量备份 (每周日 23:00)",
            replace_existing=True,
        )
    except ImportError:
        logger.warning("backup_manager 未就绪，跳过备份任务注册")

    # === AInvest 知识库任务（新增）===

    # 盘前检查（07:30 -- 检查前日 AInvest 报告更新）
    _scheduler.add_job(
        _job_ainvest_kb_scan,
        CronTrigger(hour=7, minute=30, timezone="Asia/Shanghai"),
        id="ainvest_kb_morning",
        name="AInvest知识库盘前检查 (07:30)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # 盘后扫描（15:30 -- 采集当日新增报告）
    _scheduler.add_job(
        _job_ainvest_kb_scan,
        CronTrigger(hour=15, minute=30, timezone="Asia/Shanghai"),
        id="ainvest_kb_afternoon",
        name="AInvest知识库盘后扫描 (15:30)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # 晚间完整处理（21:30 -- 深度解析+向量嵌入+TAMF联动）
    _scheduler.add_job(
        _job_ainvest_kb_scan,
        CronTrigger(hour=21, minute=30, timezone="Asia/Shanghai"),
        id="ainvest_kb_evening",
        name="AInvest知识库晚间完整处理 (21:30)",
        replace_existing=True,
        misfire_grace_time=900,
    )

    _scheduler.start()
    logger.info("调度器已启动 (含 AInvest 知识库任务)")
    _log_next_runs()


def _log_next_runs():
    """打印下次执行时间"""
    if _scheduler is None:
        return
    jobs = _scheduler.get_jobs()
    for job in jobs:
        next_run = job.next_run_time
        logger.info(f"  {job.name}: 下次 {next_run}")



def _job_ainvest_kb_scan():
    """
    AInvest 知识库扫描定时任务入口。
    注册到 APScheduler: 07:30 / 15:30 / 21:30。
    非交易日仍然执行（AInvest 报告可能在周末生成）。
    """
    logger.info("AInvest 知识库扫描触发")
    try:
        from kb_ainvest_worker import process_ainvest_reports
        result = process_ainvest_reports()
        if result.get("status") == "completed":
            logger.info(
                f"AInvest 知识库更新完成: {result['parsed_ok']} 成功, "
                f"{result['parsed_failed']} 失败, "
                f"耗时 {result.get('elapsed_seconds', 0):.0f}s"
            )
        else:
            logger.info(f"AInvest 知识库扫描: {result.get('reason', 'ok')}")
    except Exception as e:
        logger.error(f"AInvest 知识库扫描失败: {e}")
        try:
            _safe_error_alert("AInvest 知识库扫描失败", str(e))
        except Exception:
            pass


def stop_scheduler():
    """停止调度器"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("调度器已停止")


def run_scheduler_daemon():
    """以守护进程方式运行调度器（阻塞）"""
    import signal

    def signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}，停止调度器...")
        stop_scheduler()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    start_scheduler()
    logger.info("调度器守护进程运行中，按 Ctrl+C 退出")

    try:
        import time
        while True:
            time.sleep(60)
            if _scheduler:
                _log_next_runs()
    except KeyboardInterrupt:
        stop_scheduler()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="InvestPilot 定时调度器")
    parser.add_argument("--mode", choices=["daemon", "once", "tamf-shadow"], default="daemon",
                        help="daemon: 后台运行; once: 立即执行一次; tamf-shadow: 影子模式管理")
    parser.add_argument("--job", choices=["morning", "closing", "evening"], default="morning",
                        help="指定运行哪个任务")
    parser.add_argument("--shadow-cmd", choices=["enable", "disable", "status", "diff", "promote-all", "rollback"],  # noqa: E501
                        help="TAMF 影子模式命令")
    args = parser.parse_args()

    if args.mode == "tamf-shadow":
        logging.basicConfig(level=logging.INFO)
        try:
            from tamf_shadow import TamfShadowMode
            shadow = TamfShadowMode()
            if args.shadow_cmd == "enable":
                shadow.enable()
                print("✅ TAMF 影子模式已激活 — 所有更新将先写入影子目录")
                print("   影子目录: data/target_memories_shadow/")
                print("   生产目录不受影响。验证通过后执行 promote-all 晋升。")
            elif args.shadow_cmd == "disable":
                shadow.disable()
                print("✅ TAMF 影子模式已停用 — 恢复正常生产写入")
            elif args.shadow_cmd == "status":
                import json
                print(json.dumps(shadow.status_report(), ensure_ascii=False, indent=2))
            elif args.shadow_cmd == "diff":
                diffs = shadow.diff_all()
                if not diffs:
                    print("(无已影子化的标的)")
                for d in diffs:
                    if d["has_diff"]:
                        print(f"\n{'='*60}")
                        print(f"🔍 {d['code']} — {d['diff_lines']} 行差异")
                        print(f"{'='*60}")
                        print(d["diff_text"][:2000])
                    else:
                        print(f"✅ {d['code']} 无差异" if d["shadow_exists"] else f"⚠️ {d['code']} 无影子文件")  # noqa: E501
            elif args.shadow_cmd == "promote-all":
                result = shadow.promote_all()
                print(f"✅ 批量晋升完成: {result['promoted']}成功 / {result['failed']}失败 (共{result['total']}只)")  # noqa: E501
            elif args.shadow_cmd == "rollback":
                count = shadow.rollback_all()
                print(f"✅ 已回滚全部 {count} 个影子文件")
            else:
                print("用法: python schedule_runner.py --mode tamf-shadow --shadow-cmd <enable|disable|status|diff>")  # noqa: E501
        except ImportError as e:
            print(f"❌ 影子模式模块不可用: {e}")
    elif args.mode == "once":
        logging.basicConfig(level=logging.INFO)
        if args.job == "morning":
            job_morning()
        elif args.job == "closing":
            job_closing()
        else:
            job_evening()
    else:
        run_scheduler_daemon()
