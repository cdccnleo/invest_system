"""
schedule_runner.py — Phase 2 定时调度模块
APScheduler 驱动 08:30 / 15:30 / 21:00 三个工作流
完成后通过 Server酱(微信) + 飞书机器人推送报告
"""

import os, sys, logging
from pathlib import Path
from datetime import datetime, date

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

# ── 工作流定义 ────────────────────────────────────────────────────────────
from fetch_reports import collect_reports
from skill_library import check_skill_triggers, generate_skill_draft, SkillLifecycle
from skill_library import DRAFT_DIR, APPROVED_DIR, TRIGGER_DAYS, TRIGGER_MIN_CALLS
from notification import send_notification, send_error_alert
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
            except:
                detail = {}
            positions_count = detail.get("positions", "?")
            quotes_count = detail.get("quotes", "?")
            news_count = detail.get("news", "?")
            ts = event_time.strftime("%H:%M") if event_time else ""
            return (f"✅ 盘前分析完成 | {ts}\n"
                    f"📊 持仓: {positions_count} 只 | 行情: {quotes_count} 条 | 新闻: {news_count} 条\n"
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
            except:
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


def job_closing():
    """15:30 盘后工作流"""
    if not _guard_trading_day("job_closing"):
        return
    logger.info("=" * 50)
    logger.info("15:30 盘后工作流启动")
    try:
        run_analysis()
        daily_embedding_job()  # 向量化当日新闻

        # 采集持仓股票财务数据
        try:
            import csv
            pos_file = "/mnt/d/Hold/invest-data/positions.csv"
            with open(pos_file) as f:
                reader = csv.DictReader(f)
                positions = list(reader)
            # 提取股票代码（去掉基金ETF）
            stock_codes = []
            for p in positions:
                code = (p.get("code") or "").strip()
                # 排除基金代码（5开头、15开头、51开头、56开头、58开头）
                if code and not code.startswith(("5", "15", "51", "56", "58")) and len(code) == 6:
                    stock_codes.append(code)
            if stock_codes:
                fin_results = collect_financial_for_positions(stock_codes)
                saved = sum(v.get("saved", 0) for v in fin_results.values())
                logger.info(f"财务数据采集: {saved} 条记录")
        except Exception as e:
            logger.warning(f"财务数据采集异常: {e}")

        # 采集持仓股公告（新浪财经网页版）
        try:
            anns = fetch_all_positions_announcements(days_window=30, max_pages=3)
            logger.info(f"公告采集: {len(anns)} 条")
            # 持久化到数据库
            if anns:
                ann_storage = get_storage()
                stored = ann_storage.write_announcements(anns)
                ann_storage.close()
                logger.info(f"公告写入 DB: {stored} 条")
            # 公司行为成本调整（分红除权、送股）
            if anns:
                from pgcrypto_migration import process_corp_actions
                action_result = process_corp_actions(anns)
                if action_result["processed"] > 0:
                    msg = (f"🏢 公司行为调整: 分红{action_result['dividend']}笔 "
                           f"送股{action_result['bonus']}笔 已更新持仓成本/份额")
                    send_notification("🏢 持仓成本自动调整", msg, level="INFO")
                    logger.info(f"公司行为调整完成: {action_result}")
        except Exception as e:
            logger.warning(f"公告采集异常: {e}")

        storage = get_storage()
        storage.write_audit(
            "SCHEDULED_CLOSING_RUN", "SYSTEM",
            detail={"triggered_at": datetime.now().isoformat()},
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


def job_tamf_update():
    """15:35 TAMF增量更新 — 盘后数据到达后更新所有持仓标的分析记忆文件"""
    if not _guard_trading_day("job_tamf_update"):
        return
    logger.info("=" * 50)
    logger.info("15:35 TAMF增量更新启动")
    try:
        from tamf_updater import scheduled_update_all_holdings
        result = scheduled_update_all_holdings()
        logger.info(f"TAMF更新完成: 更新{result['updated']}个, 跳过{result['skipped']}个, 失败{result['failed']}个")
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


def job_deep_analysis_weekly():
    """周日22:00 周频深度分析 — 强制重生成所有持仓标的的Agent段落"""
    logger.info("=" * 50)
    logger.info("周日22:00 周频深度分析启动")
    try:
        from tamf_updater import scheduled_deep_analysis_weekly
        result = scheduled_deep_analysis_weekly()
        logger.info(f"深度分析完成: 深度更新{result['deep_updated']}个, 跳过{result['skipped']}个, 失败{result['failed']}个")
        if result["failed"] > 0:
            _safe_error_alert("⚠️ 周频深度分析部分失败",
                f"深度更新{result['deep_updated']}个, 失败{result['failed']}个\n" +
                "\n".join(result["errors"][:3]))
        elif result["deep_updated"] > 0:
            logger.info(f"✅ 周频深度分析成功({result['deep_updated']}个标的)")
    except Exception as e:
        logger.error(f"周频深度分析异常: {e}")
        _safe_error_alert("🔴 周频深度分析异常", f"错误: {e}")


def job_reports_collection():
    """16:00 研报复盘工作流 — 采集当日研报"""
    if not _guard_trading_day("job_reports_collection"):
        return
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

        logger.info("研报采集工作流完成")
    except Exception as e:
        logger.error(f"研报采集工作流异常: {e}")
        _safe_error_alert("🔴 研报采集工作流异常", f"错误: {e}")


def job_evening():
    """21:00 晚间工作流"""
    if not _guard_trading_day("job_evening"):
        return
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
        report = "🌙 晚间复盘分析已完成\n\n今日收盘后进行了二次分析，整合了全天新闻和市场情绪，请查看详细报告了解最新操作计划。"
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


def job_announcements_collection():
    """20:50 公告采集工作流 — 采集持仓股近30天公告（晚间集中发布后采集，21:00前完成供复盘使用）"""
    if not _guard_trading_day("job_announcements_collection"):
        return
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
    from datetime import time as dtime
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
        import json

        # Step 1: 检测固化触发
        triggered = check_skill_triggers()
        logger.info(f"固化检测: {len(triggered)} 个任务满足条件")

        if not triggered:
            logger.info("无满足固化条件的任务")
            return

        # Step 2: 收集现有草案（避免重复生成）
        sl = SkillLifecycle()
        existing_drafts = sl.list_drafts()
        drafted_patterns = {d.get("_meta", {}).get("task_pattern", "").lower() for d in existing_drafts}

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
            msg = f"🧠 技能固化检测完成\n\n{len(triggered)} 个任务满足固化条件，均已有草案，无需处理。"
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
            APPROVED_DIR, _log_skill_execution,
        )
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
            last_query = detail.get('query', '未知查询')
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


def job_behavior_profile_update():
    """
    每日 15:40 收盘后行为画像更新
    - 调用 analyze_trading_behavior(30) 分析近30天审计日志
    - 将结果写入 l3.behavior_profile（upsert）
    - 触发 l3_dialog_engine 做对话式风险提醒（如需）
    """
    logger.info("=" * 50)
    logger.info("15:40 行为画像更新启动")
    try:
        from audit_analytics import analyze_trading_behavior
        from datetime import date
        import json as _json
        from storage_factory import get_pg_connection
        from pgcrypto_migration import get_credential

        # 分析近30天行为
        profile = analyze_trading_behavior(days=30)
        logger.info(f"行为画像: {profile.get('behavior_patterns', [])}")

        # 写入 l3.behavior_profile（使用 SQL 迁移脚本定义的标准表结构）
        # 表结构：l3_phase_a.sql 定义的 behavior_profile
        pg_conn = get_pg_connection()
        if pg_conn:
            cur = pg_conn.cursor()
            profile_date = date.today().isoformat()
            dimensions = {
                "trade_freq": {
                    "metric": "trade_freq_7d",
                    "value": profile.get("total_analysis_runs", 0),
                    "alert": "critical" if any("激进" in p for p in profile.get("behavior_patterns", [])) else "normal",
                },
                "overtrading": {
                    "metric": "max_consecutive_mod_days",
                    "value": profile.get("max_consecutive_mod_days", 0),
                    "alert": "warning" if profile.get("max_consecutive_mod_days", 0) >= 3 else "normal",
                },
            }
            for dim, info in dimensions.items():
                cur.execute("""
                    INSERT INTO l3.behavior_profile
                        (profile_date, dimension, metric_name, metric_value, alert_level)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (profile_date, dimension, metric_name)
                    DO UPDATE SET
                        metric_value = EXCLUDED.metric_value,
                        alert_level = EXCLUDED.alert_level,
                        updated_at = NOW()
                """, (profile_date, dim, info["metric"], info["value"], info["alert"]))
            pg_conn.commit()
            cur.close()
            pg_conn.close()
            logger.info("行为画像已写入 l3.behavior_profile")
        else:
            logger.warning("无法连接数据库，行为画像跳过写入")

        # 触发 L3 主动提醒（如行为异常，通过飞书推送预警）
        patterns = profile.get("behavior_patterns", [])
        if any("激进" in p for p in patterns):
            send_notification("⚠️ L3 行为预警",
                f"检测到您近期频繁修改AI计划（连续{profile.get('max_consecutive_mod_days',0)}天修改），"
                "建议适当减少干预，给AI计划更多信任空间。")
        elif profile.get("analysis_success_rate", 100) < 60:
            send_notification("⚠️ L3 行为预警",
                f"近30天AI计划采纳率仅{profile['analysis_success_rate']:.0f}%，"
                "建议复盘修改原因，减少过度干预。")

        # 推送确认
        try:
            msg = f"📊 **每日行为画像**（近30天）\n\n"
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
            logger.warning(f"行为画像推送异常: {e}")

    except Exception as e:
        logger.error(f"行为画像更新异常: {e}")
        _safe_error_alert("🔴 行为画像更新异常", f"错误: {e}")


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


def job_weekly_stress_test():
    """
    每周一 07:00 盘前压力测试
    - 调用 L3DialogEngine.run_stress_test() 执行5种极端情景分析
    - 结果写入 l3.stress_test_results
    - 推送最坏情景告警至飞书/Server酱
    """
    logger.info("=" * 50)
    logger.info("07:00 周压力测试启动")
    try:
        from l3_dialog_engine import L3DialogEngine

        engine = L3DialogEngine()
        run_id = engine.run_stress_test()
        if run_id:
            status = engine.get_l3_status()
            worst_test = status["stress_tests"][0] if status.get("stress_tests") else None
            if worst_test:
                msg = (
                    f"📊 **周压力测试完成**\n\n"
                    f"最坏情景: {worst_test['scenario']}\n"
                    f"最大损失: {worst_test['loss_pct']:.2f}%\n"
                    f"风险评分: {worst_test['risk_score']}/10\n"
                    f"运行编号: {run_id[:8]}..."
                )
                send_notification("📊 周压力测试完成", msg)
            logger.info(f"压力测试完成: run_id={run_id}")
        else:
            logger.info("压力测试跳过（无持仓数据）")
    except Exception as e:
        logger.error(f"压力测试异常: {e}")
        _safe_error_alert("🔴 压力测试异常", f"错误: {e}")


def job_behavior_insights():
    """
    每周日 20:00 行为洞察周报推送
    - 调用 audit_analytics.send_behavior_insights_report(7) 分析近7天行为
    - 推送行为洞察至飞书/Server酱
    """
    logger.info("=" * 50)
    logger.info("20:00 行为洞察周报启动")
    try:
        from audit_analytics import send_behavior_insights_report

        report = send_behavior_insights_report(days=7)
        if report:
            send_notification("📊 周行为洞察报告", report)
            logger.info("行为洞察周报已推送")
        else:
            logger.warning("行为洞察周报为空")
    except Exception as e:
        logger.error(f"行为洞察推送异常: {e}")
        _safe_error_alert("🔴 行为洞察推送异常", f"错误: {e}")


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

    # 16:00 研报采集（每个交易日收盘后）
    _scheduler.add_job(
        job_reports_collection,
        CronTrigger(hour=16, minute=0, timezone="Asia/Shanghai"),
        id="reports_collection",
        name="研报采集工作流 (16:00)",
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

    # 每周五 22:00 周线回测报告（上周策略表现，仅在数据≥30天时运行）
    _scheduler.add_job(
        job_weekly_backtest,
        CronTrigger(day_of_week='fri', hour=22, minute=0, timezone="Asia/Shanghai"),
        id="weekly_backtest",
        name="周线回测报告 (每周五 22:00)",
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

    # 每周一 07:00 盘前压力测试（基于回测引擎的极端情景分析）
    _scheduler.add_job(
        job_weekly_stress_test,
        CronTrigger(day_of_week='mon', hour=7, minute=0, timezone="Asia/Shanghai"),
        id="weekly_stress_test",
        name="周压力测试 (每周一 07:00)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # 每周日 20:00 行为洞察周报推送
    _scheduler.add_job(
        job_behavior_insights,
        CronTrigger(day_of_week='sun', hour=20, minute=0, timezone="Asia/Shanghai"),
        id="weekly_behavior_insights",
        name="行为洞察周报 (每周日 20:00)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    _scheduler.start()
    logger.info("调度器已启动")
    _log_next_runs()


def _log_next_runs():
    """打印下次执行时间"""
    if _scheduler is None:
        return
    jobs = _scheduler.get_jobs()
    for job in jobs:
        next_run = job.next_run_time
        logger.info(f"  {job.name}: 下次 {next_run}")


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
    parser.add_argument("--mode", choices=["daemon", "once"], default="daemon",
                        help="daemon: 后台运行; once: 立即执行一次")
    parser.add_argument("--job", choices=["morning", "closing", "evening"], default="morning",
                        help="指定运行哪个任务")
    args = parser.parse_args()

    if args.mode == "once":
        logging.basicConfig(level=logging.INFO)
        if args.job == "morning":
            job_morning()
        elif args.job == "closing":
            job_closing()
        else:
            job_evening()
    else:
        run_scheduler_daemon()
