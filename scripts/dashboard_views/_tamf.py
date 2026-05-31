"""
Dashboard sub-module — TAMF Memory / History Calendar
Generated from dashboard.py (928-1396 lines)
Each function accesses streamlit via st (passed through from main module).
"""

from datetime import datetime
import sys
import streamlit as st
import pandas as pd
from ._shared import get_db_connection

# ── 视图 3：历史决策日历 ────────────────────────────────────────────────────

def _get_calendar_data(days: int = 60) -> list[dict]:
    """从 audit_log 拉取日历所需的数据"""
    conn = get_db_connection()
    if conn is None:
        return []

    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                DATE(event_time)                       AS day,
                event_type,
                operator,
                result,
                detail,
                trace_id
            FROM audit.audit_log
            WHERE event_time >= CURRENT_DATE - INTERVAL '%s days'
              AND event_type NOT IN ('SKILL_EXECUTED', 'SKILL_SPOT_CHECK',
                                     'SKILL_VALIDATED', 'SKILL_APPROVED', 'SKILL_REJECTED')
            ORDER BY event_time DESC
        """, (days,))
        rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "day": r[0],
            "event_type": r[1],
            "operator": r[2],
            "result": r[3],
            "detail": r[4],
            "trace_id": r[5],
        }
        for r in rows
    ]


def _enrich_calendar_entry(entry: dict) -> dict:
    """为单条日历条目补充展示用字段"""
    import json as _json

    detail = entry.get("detail", {})
    if isinstance(detail, str):
        try:
            detail = _json.loads(detail)
        except Exception:
            detail = {}
    entry["_detail"] = detail

    # 运行阶段图标
    phase_icons = {
        "morning": "🌅",
        "closing": "🌆",
        "evening": "🌙",
        "MVP_ANALYSIS_RUN": "🚀",
        "ANALYSIS_COMPLETE": "📋",
        "DAILY_REFLECTION": "🔍",
    }
    entry["_phase_emoji"] = next(
        (v for k, v in phase_icons.items() if k.lower() in entry["event_type"].lower()),
        "📌"
    )

    # 运行阶段中文名
    phase_names = {
        "morning": "盘前",
        "closing": "盘后",
        "evening": "晚间",
        "MVP_ANALYSIS_RUN": "分析运行",
        "ANALYSIS_COMPLETE": "计划生成",
        "DAILY_REFLECTION": "每日复盘",
    }
    entry["_phase_name"] = next(
        (v for k, v in phase_names.items() if k.lower() in entry["event_type"].lower()),
        entry["event_type"]
    )

    # 结果颜色
    entry["_ok"] = entry["result"] == "SUCCESS"

    # 操作计划数（从 detail 中提取）
    plans = detail.get("plans_count") or detail.get("plans") or 0
    if isinstance(plans, list):
        plans = len(plans)
    entry["_plans_count"] = plans

    # 置信度
    entry["_confidence"] = detail.get("confidence", "N/A")

    # 修改比例（来自复盘）
    mod_ratio = detail.get("attribution", {}).get("modification_ratio", None)
    entry["_mod_ratio"] = f"{mod_ratio:.0f}%" if mod_ratio is not None else None

    # 洞察摘要
    insights = detail.get("attribution", {}).get("insights", [])
    entry["_insight"] = (insights[0][:60] + "…") if insights else None

    return entry


def _build_month_grid(year: int, month: int, entries_by_date: dict) -> pd.DataFrame:
    """构建单个月份的日历网格 DataFrame"""
    import calendar
    from datetime import date

    cal = calendar.Calendar(firstweekday=6)  # 周日开局
    weeks = cal.monthdatescalendar(year, month)

    rows = []
    for week in weeks:
        week_cells = []
        for day in week:
            if day.month != month:
                week_cells.append({"date": "", "day": "", "emoji": "", "events": "", "status": ""})
            else:
                date_str = day.strftime("%Y-%m-%d")
                evts = entries_by_date.get(date_str, [])
                if not evts:
                    status = "empty"
                    emoji = "　"  # 空日期用空格
                    events = ""
                else:
                    # 取最重要的一条记录决定颜色
                    top = evts[0]
                    if top["result"] == "SUCCESS":
                        status = "success"
                    else:
                        status = "failed"

                    # 拼装当日事件摘要
                    summaries = []
                    for e in evts[:3]:
                        phase = e.get("_phase_name", e["event_type"])
                        plans = e.get("_plans_count", 0)
                        conf = e.get("_confidence", "N/A")
                        summaries.append(f"{phase}({plans}计划, {conf})")

                    events = "\n".join(summaries)
                    emoji = top.get("_phase_emoji", "📌")

                week_cells.append({
                    "date": date_str,
                    "day": str(day.day),
                    "emoji": emoji,
                    "events": events,
                    "status": status,
                })
        rows.append(week_cells)

    return rows


def render_history():
    import calendar

    st.markdown("## 📅 历史决策日历")

    # ── 顶部筛选器 ──────────────────────────────────────────────────────────
    col_filter1, col_filter2, col_filter3 = st.columns([1, 1, 2])
    with col_filter1:
        view_month = st.selectbox(
            "查看月份",
            options=list(range(1, 13)),
            index=list(range(1, 13)).index(datetime.now().month) if datetime.now().month <= 12 else 0,
            format_func=lambda m: f"{datetime.now().year}-{m:02d}",
        )
    with col_filter2:
        view_year = st.selectbox("年份", list(range(datetime.now().year - 2, datetime.now().year + 1))[::-1])
    with col_filter3:
        days_range = st.selectbox("时间范围", [30, 60, 90, 180], index=1,
                                  format_func=lambda d: f"近{d}天")

    # ── 拉取数据 ───────────────────────────────────────────────────────────
    raw_entries = _get_calendar_data(days=days_range)
    for e in raw_entries:
        _enrich_calendar_entry(e)

    # 按日期分组
    entries_by_date = {}
    for e in raw_entries:
        day_str = e["day"].strftime("%Y-%m-%d") if e["day"] else ""
        if day_str:
            entries_by_date.setdefault(day_str, []).append(e)

    # ── 月度日历网格 ─────────────────────────────────────────────────────
    st.markdown(f"#### 📆 {view_year} 年 {view_month:02d} 月")
    weeks_data = _build_month_grid(view_year, view_month, entries_by_date)

    # 星期标题
    weekday_labels = ["日", "一", "二", "三", "四", "五", "六"]
    header_cols = st.columns([1, 1, 1, 1, 1, 1, 1])
    for i, label in enumerate(weekday_labels):
        with header_cols[i]:
            color = "#e8f4fd" if i in (0, 6) else "#f8f9fa"
            st.markdown(
                f"<div style='background:{color}; padding:6px 0; text-align:center; "
                f"border-radius:4px; font-weight:bold; font-size:13px'>{label}</div>",
                unsafe_allow_html=True,
            )

    # 日历格子
    for week_cells in weeks_data:
        cols = st.columns([1, 1, 1, 1, 1, 1, 1])
        for i, cell in enumerate(week_cells):
            with cols[i]:
                if not cell["date"]:
                    st.markdown(
                        "<div style='height:90px; background:#fafafa; border-radius:4px;'></div>",
                        unsafe_allow_html=True,
                    )
                    continue

                # 背景色：成功=浅绿，失败=浅红，空=白
                bg_map = {"success": "#e8f5e9", "failed": "#ffebee", "empty": "#ffffff"}
                bg = bg_map.get(cell["status"], "#ffffff")

                # 边框色
                border_map = {"success": "#4caf50", "failed": "#f44336", "empty": "#e0e0e0"}
                border = border_map.get(cell["status"], "#e0e0e0")

                day_num = cell["day"]
                events_md = ""
                if cell["events"]:
                    for line in cell["events"].split("\n"):
                        events_md += f"<div style='font-size:10px; line-height:1.3; color:#555;'>{line}</div>"

                # 关键修复：calendar.Calendar 返回的 day_num 已经是 str，这里取 week_cells[i]["day"]
                st.markdown(
                    f"<div style='background:{bg}; border-left:3px solid {border}; "
                    f"padding:6px 8px; height:90px; overflow:hidden; border-radius:0 4px 4px 0;'>"
                    f"<div style='font-size:16px; font-weight:bold; margin-bottom:2px;'>{day_num}</div>"
                    f"<div style='font-size:14px; margin-bottom:2px;'>{cell['emoji']}</div>"
                    f"{events_md}"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    st.divider()

    # ── 月度统计摘要 ──────────────────────────────────────────────────────
    st.markdown("#### 📊 本月运行摘要")

    month_key = f"{view_year}-{view_month:02d}"
    month_entries = [
        e for e in raw_entries
        if e["day"] and e["day"].strftime("%Y-%m") == month_key
    ]

    if month_entries:
        col_stat1, col_stat2, col_stat3, col_stat4 = st.columns(4)
        total_runs = len(month_entries)
        success = sum(1 for e in month_entries if e["result"] == "SUCCESS")
        plans_total = sum(e.get("_plans_count", 0) for e in month_entries)
        with col_stat1:
            st.metric("运行次数", total_runs)
        with col_stat2:
            st.metric("成功率", f"{success/total_runs*100:.0f}%",
                      delta="✅" if success == total_runs else "⚠️")
        with col_stat3:
            st.metric("生成计划数", plans_total)
        with col_stat4:
            days_with_runs = len({e["day"].strftime("%Y-%m-%d") for e in month_entries if e["day"]})
            st.metric("活跃天数", f"{days_with_runs}/{calendar.monthrange(view_year, view_month)[1]}")
    else:
        st.info("本月暂无运行记录")

    st.divider()

    # ── 每日明细列表 ──────────────────────────────────────────────────────
    st.markdown("#### 📋 每日明细")

    selected_date = st.selectbox(
        "选择日期查看详情",
        options=sorted(entries_by_date.keys(), reverse=True),
        format_func=lambda d: d,
    )

    if selected_date and selected_date in entries_by_date:
        day_entries = entries_by_date[selected_date]
        for entry in day_entries:
            phase = entry.get("_phase_emoji", "📌") + " " + entry.get("_phase_name", entry["event_type"])
            result_icon = "✅ 成功" if entry["_ok"] else "❌ 失败"
            conf = entry.get("_confidence", "N/A")
            plans = entry.get("_plans_count", 0)
            insight = entry.get("_insight")
            mod = entry.get("_mod_ratio")

            with st.expander(f"{phase} — {result_icon} | 置信度:{conf} | {plans}计划"
                             + (f" | 修改:{mod}" if mod else "")):
                st.markdown(f"**操作者**: {entry['operator']}")
                st.markdown(f"**结果**: {entry['result']}")

                detail = entry.get("_detail", {})
                if detail:
                    st.markdown("**详情**:")
                    for k, v in detail.items():
                        if k not in ("plans", "insights"):
                            st.markdown(f"  - {k}: `{v}`")

                if insight:
                    st.markdown(f"**洞察**: {insight}")

    # ── 行为洞察报告入口 ─────────────────────────────────────────────────
    st.divider()
    st.markdown("#### 🧠 行为洞察报告")

    col_ai1, col_ai2 = st.columns(2)
    with col_ai1:
        if st.button("📊 近7天行为分析"):
            from audit_analytics import analyze_trading_behavior
            result = analyze_trading_behavior(7)
            st.success(f"运行次数: {result['total_analysis_runs']} | AI采纳率: {result['analysis_success_rate']}%")
            for p in result.get("behavior_patterns", []):
                st.markdown(f"- {p}")
            for r in result.get("recommendations", []):
                st.markdown(f"💡 {r}")

    with col_ai2:
        if st.button("📅 月度报告"):
            from audit_analytics import monthly_report
            result = monthly_report()
            st.metric("活跃天数", result["active_days"])
            st.metric("定时运行", result["scheduled_runs"])
            st.metric("AI自动采纳率", f"{result['auto_adoption_rate']}%")


# ── 视图 4：设置 ────────────────────────────────────────────────────────────

# ── 视图 7：计划审核 ─────────────────────────────────────────────────────

def ensure_plan_review_table(conn):
    """确保 analysis schema 和 plan_review 表存在"""
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA IF NOT EXISTS analysis")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS analysis.plan_reviews (
            id SERIAL PRIMARY KEY,
            run_id TEXT NOT NULL,
            plan_index INTEGER NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
            position_pct INTEGER NOT NULL,
            reason TEXT DEFAULT '',
            reviewed_by TEXT DEFAULT 'manual',
            reviewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, plan_index)
        )
    """)
    conn.commit()


def render_tamf_memory():
    """📊 TAMF分析记忆视图 — 标的级分析记忆文件浏览器"""
    import streamlit as st
    from pathlib import Path

    TAMF_DIR = Path(__file__).parent.parent.parent / "data" / "target_memories"

    st.markdown("## 📊 TAMF 投资标的分析记忆")

    # 加载持仓列表
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from pgcrypto_migration import load_positions_from_db
        positions = load_positions_from_db()
        codes = [p["code"] for p in positions]
        names = {p["code"]: p["name"] for p in positions}
        anon_map = {p["code"]: p.get("code", p["code"]) for p in positions}
    except Exception as e:
        st.error(f"加载持仓失败: {e}")
        return

    col_sel, col_view = st.columns([1, 3])

    with col_sel:
        st.markdown("### 选择标的")
        code_options = [f"{c} {names.get(c, '')}" for c in codes]
        selected_label = st.selectbox("持仓标的", code_options)
        selected_code = selected_label.split(" ")[0].strip()

        # 元数据卡片
        meta_q = """
            SELECT version_major, version_minor, analysis_status, last_updated, data_snapshot
            FROM memory.target_memory_files WHERE ts_code = %s
        """
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(meta_q, (selected_code,))
            row = cur.fetchone()
            conn.close()
            if row:
                vmaj, vmin, status, lupdated, snapshot = row
                snap = snapshot if isinstance(snapshot, dict) else {}
                st.markdown(f"**{names.get(selected_code, selected_code)}**（{selected_code}）")
                st.caption(f"版本 v{vmaj}.{vmin} | 状态 {status} | 更新 {str(lupdated)[:16]}")
                if snap:
                    st.caption(f"行情: {snap.get('last_quote_date','—')} | 公告: {snap.get('last_ann_date','—')}")
            else:
                st.warning("无TAMF元数据")
        except Exception as e:
            st.error(f"查询元数据失败: {e}")

    with col_view:
        tamf_path = TAMF_DIR / f"{selected_code}.md"
        if not tamf_path.exists():
            st.warning(f"TAMF文件不存在: {tamf_path}")
            return

        content = tamf_path.read_text(encoding="utf-8")

        # 子Tab视图
        tabs = st.tabs(["📋 完整文件", "📊 基本面", "📈 技术面", "📰 消息面", "🧠 监控"])

        with tabs[0]:
            st.markdown(content)

        with tabs[1]:
            import re
            # 提取章节一和三
            m = re.search(r"(## 一、标的基本画像.*?)(?=^## |$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")
            m = re.search(r"(## 三、基本面趋势.*?)(?=^## 四|$$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")

        with tabs[2]:
            m = re.search(r"(## 四、技术面与市场表现.*?)(?=^## 五|$$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")

        with tabs[3]:
            m = re.search(r"(## 五、消息面追踪.*?)(?=^## 六|$$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")

        with tabs[4]:
            m = re.search(r"(## 七、跟踪状态与预警.*?)(?=^## 八|$$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")

    # 底部时间线
    st.divider()
    st.markdown("### 📅 时间线事件（近30条）")
    tl_q = """
        SELECT event_time, event_type, severity, title, description
        FROM memory.target_timeline_events
        WHERE ts_code = %s
        ORDER BY event_time DESC
        LIMIT 30
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(tl_q, (selected_code,))
        rows = cur.fetchall()
        conn.close()
        if not rows:
            st.info("暂无时间线事件")
        else:
            for r in rows:
                evt_time, evt_type, sev, title, desc = r
                icon = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}.get(sev, "⚪")
                st.markdown(f"{icon} **{evt_type}** `{str(evt_time)[:16]}` {title or ''}")
                if desc:
                    st.caption(f"  {str(desc)[:100]}")
    except Exception as e:
        st.error(f"加载时间线失败: {e}")


