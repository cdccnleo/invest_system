"""
Dashboard sub-module — News / Reports / Announcements
Generated from dashboard.py (624-928 lines)
Each function accesses streamlit via st (passed through from main module).
"""

import streamlit as st
from datetime import datetime
from ._shared import get_db_connection, get_sync_status, set_sync_status


# ── 手动同步按钮通用组件 ────────────────────────────────────────────────────

def _render_sync_button(data_type: str, label: str, sync_func, help_text: str = ""):
    """
    渲染手动同步按钮组件，包含防重复点击、状态反馈和异常处理。

    Args:
        data_type: 数据类型标识（news/reports/announcements）
        label: 按钮文本
        sync_func: 同步执行函数，返回 {"status": ..., "total": ..., "saved": ..., "error": ...}
        help_text: 按钮提示文本
    """
    status = get_sync_status(data_type)
    last_sync = status.get("last_sync")
    is_syncing = status.get("syncing", False)

    col_info, col_btn = st.columns([2, 1])
    with col_info:
        if last_sync:
            st.caption(f"最后同步: {last_sync.strftime('%m-%d %H:%M:%S')}")
        else:
            st.caption("尚未手动同步过")

    with col_btn:
        disabled = is_syncing
        if st.button(label, help=help_text, disabled=disabled, key=f"sync_{data_type}_btn"):
            set_sync_status(data_type, syncing=True)
            with st.spinner(f"正在采集{data_type}数据..."):
                try:
                    result = sync_func()
                    set_sync_status(data_type, last_sync=datetime.now(), syncing=False)
                    if result["status"] == "ok":
                        st.success(f"同步完成：采集 {result['total']} 条，新增 {result['saved']} 条")
                        st.rerun()
                    elif result["status"] == "empty":
                        st.info("数据已是最新，无新增内容")
                    else:
                        st.error(f"同步失败: {result.get('error', '未知错误')}")
                except Exception as e:
                    set_sync_status(data_type, syncing=False)
                    st.error(f"同步异常: {e}")


# ── 视图 2：新闻摘要 ────────────────────────────────────────────────────────

def render_news_summary():
    st.markdown("## 📰 新闻摘要（近7日）")

    # ── 手动同步按钮 ──
    def _sync_news():
        from fetch_news import collect_and_save_news
        return collect_and_save_news()

    _render_sync_button("news", "🔄 同步新闻", _sync_news, "手动触发新闻数据采集")

    st.divider()

    conn = get_db_connection()
    if conn is None:
        st.warning("无法连接数据库")
        return

    cur = conn.cursor()
    try:
        # ── Tab 1: 全部新闻 ────────────────────────────────────────────────
        cur.execute("""
            SELECT title, content, source, published_at, severity
            FROM research.news_articles
            WHERE published_at >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY published_at DESC
            LIMIT 80
        """)
        all_rows = cur.fetchall()

        # ── Tab 2: 国际投行研究 ────────────────────────────────────────────
        cur.execute("""
            SELECT title, content, source, published_at,
                   cited_institutions, article_type, is_bank_related
            FROM research.international_bank_research
            WHERE published_at >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY is_bank_related DESC, published_at DESC
            LIMIT 40
        """)
        intl_rows = cur.fetchall()

        if not all_rows and not intl_rows:
            st.info("暂无新闻数据")
            return

        # Tab 布局
        tab1, tab2 = st.tabs(["📋 全部新闻", "🏦 国际投行研究"])

        # ── Tab 1: 全部新闻 ───────────────────────────────────────────────
        with tab1:
            st.markdown(f"**共 {len(all_rows)} 条**")
            for title, content, source, published_at, severity in all_rows:
                date_str = published_at.strftime("%m-%d %H:%M") if published_at else "未知"
                sev_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "⚪"}.get(severity, "⚪")
                with st.expander(f"{sev_emoji} [{date_str}] {source} - {title}"):
                    st.markdown(content[:400] if content else "无正文")

        # ── Tab 2: 国际投行研究 ───────────────────────────────────────────
        with tab2:
            if not intl_rows:
                st.info("暂无国际投行研究数据（定时任务 21:00 采集）")
            else:
                # 统计
                [r for r in intl_rows if r[6]]
                cur.execute("""
                    SELECT COUNT(*) FROM research.international_bank_research
                    WHERE published_at >= CURRENT_DATE - INTERVAL '7 days'
                      AND is_bank_related = TRUE
                """)
                bank_cnt = cur.fetchone()[0] or 0

                col1, col2, col3 = st.columns(3)
                col1.metric("总条数", f"{len(intl_rows)} 条")
                col2.metric("投行相关", f"{bank_cnt} 条")
                col3.metric("来源数", f"{len(set(r[2] for r in intl_rows))} 个")

                st.divider()

                # 筛选：只看投行相关
                show_bank_only = st.checkbox("🔴 仅显示投行相关", value=False, key="intl_bank_only")
                display_rows = [r for r in intl_rows if r[6]] if show_bank_only else intl_rows

                for row in display_rows:
                    title, content, source, published_at, cited_inst, art_type, is_bank = row
                    date_str = published_at.strftime("%m-%d %H:%M") if published_at else "未知"
                    inst_str = ', '.join(cited_inst) if cited_inst else ''
                    bank_tag = "🔴" if is_bank else "⚪"

                    header = (f"{bank_tag} [{date_str}] {source}"
                              + (f" | 涉及: {inst_str}" if inst_str else "")
                              + f" | {title}")
                    with st.expander(header):
                        st.markdown(f"**类型**: {art_type or '一般'}")
                        if inst_str:
                            st.markdown(f"**涉及机构**: {inst_str}")
                        st.markdown(content[:500] if content else "无摘要")

    except Exception as e:
        st.error(f"加载新闻失败: {e}")
    finally:
        conn.close()


# ── 视图 2.5：研报 ─────────────────────────────────────────────────────────

def render_reports():
    """研报展示页面 — 近30天研报，支持按股票/评级/来源筛选"""
    st.markdown("## 📋 研报（近30天）")

    # ── 手动同步按钮 ──
    def _sync_reports():
        from fetch_reports import collect_reports
        try:
            reports = collect_reports(days_back=7, save_to_db=True)
            return {"status": "ok", "total": len(reports), "saved": len(reports), "error": None}
        except Exception as e:
            return {"status": "error", "total": 0, "saved": 0, "error": str(e)}

    _render_sync_button("reports", "🔄 同步研报", _sync_reports, "手动触发研报数据采集（近7天）")

    st.divider()

    conn = get_db_connection()
    if conn is None:
        st.warning("无法连接数据库")
        return

    cur = conn.cursor()
    try:
        # 汇总统计
        cur.execute("""
            SELECT COUNT(*),
                   COUNT(DISTINCT source),
                   COUNT(DISTINCT ts_code),
                   COUNT(DISTINCT report_date)
            FROM research.research_reports
            WHERE report_date >= CURRENT_DATE - INTERVAL '30 days'
        """)
        total, org_count, stock_count, day_count = cur.fetchone()

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("研报总数", f"{total} 份")
        col2.metric("覆盖机构", f"{org_count} 家")
        col3.metric("涉及股票", f"{stock_count} 只")
        col4.metric("覆盖天数", f"{day_count} 天")

        st.divider()

        # 筛选器
        col_a, col_b = st.columns(2)
        with col_a:
            rating_filter = st.selectbox(
                "按评级筛选",
                ["全部", "买入", "增持", "中性", "减持", "卖出", "无评级"],
                index=0,
            )
        with col_b:
            # 加载前10热门来源
            cur.execute("""
                SELECT source, COUNT(*) as cnt
                FROM research.research_reports
                WHERE report_date >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY source
                ORDER BY cnt DESC LIMIT 10
            """)
            source_options = ["全部"] + [r[0] for r in cur.fetchall()]
            source_filter = st.selectbox("按来源筛选", source_options, index=0)

        # 查询研报列表
        query = """
            SELECT report_date, source, ts_code, rating, title, summary, url
            FROM research.research_reports
            WHERE report_date >= CURRENT_DATE - INTERVAL '30 days'
        """
        params = []
        if rating_filter != "全部":
            query += " AND rating = %s"
            params.append(rating_filter)
        if source_filter != "全部":
            query += " AND source = %s"
            params.append(source_filter)
        query += " ORDER BY report_date DESC, id DESC LIMIT 100"

        cur.execute(query, params)
        rows = cur.fetchall()

        st.markdown(f"**{len(rows)} 份研报**")

        # 评级颜色映射
        rating_colors = {
            "买入": "🟢", "增持": "🟢", "强烈推荐": "🟢",
            "中性": "🟡", "谨慎": "🟡",
            "减持": "🔴", "卖出": "🔴",
        }

        for report_date, source, ts_code, rating, title, summary, url in rows:
            date_str = report_date.strftime("%Y-%m-%d") if report_date else "未知"
            r_emoji = rating_colors.get(str(rating).strip(), "⚪")

            # 重建东方财富研报原文 URL（基于 info_code）
            info_code_val = None
            # 从 title+date 查找 info_code（通过额外查询）
            cur.execute("""
                SELECT info_code FROM research.research_reports
                WHERE title = %s AND report_date = %s
            """, (title, report_date))
            row_info = cur.fetchone()
            info_code_val = row_info[0] if row_info else None

            if info_code_val:
                em_url = f"https://data.eastmoney.com/report/zw_industry.jshtml?infocode={info_code_val}"
            else:
                em_url = url if url else None

            label = f"{r_emoji} [{date_str}] {source or '未知来源'} | {str(rating or '无评级'):8s} | {ts_code or ''} {title}"

            with st.expander(label[:120]):
                st.markdown(f"**{title}**")
                col_info1, col_info2 = st.columns(2)
                with col_info1:
                    st.markdown(f"📅 **{date_str}**")
                    st.markdown(f"🏢 **{source or '未知来源'}**")
                    st.markdown(f"📊 **{ts_code or 'N/A'}**")
                with col_info2:
                    st.markdown(f"⭐ **{rating or '无评级'}**")
                    if em_url:
                        st.markdown(f"[🔗 查看原文]({em_url})")
                if summary:
                    with st.expander("📝 摘要", expanded=True):
                        st.markdown(summary[:500] if summary else "无摘要")

    except Exception as e:
        st.error(f"加载研报失败: {e}")
    finally:
        conn.close()


# ── 视图 2.6：公告 ─────────────────────────────────────────────────────────

def render_announcements():
    """公告展示页面 — 近30天持仓股公告，支持按类型筛选"""
    st.markdown("## 📢 持仓股公告（近30天）")

    # ── 手动同步按钮 ──
    def _sync_announcements():
        from fetch_announcements import fetch_all_positions_announcements
        from storage_factory import get_storage
        try:
            anns = fetch_all_positions_announcements(days_window=1, max_pages=2)
            if anns:
                storage = get_storage()
                saved = storage.write_announcements(anns)
                storage.close()
                return {"status": "ok", "total": len(anns), "saved": saved, "error": None}
            return {"status": "empty", "total": 0, "saved": 0, "error": None}
        except Exception as e:
            return {"status": "error", "total": 0, "saved": 0, "error": str(e)}

    _render_sync_button("announcements", "🔄 同步公告", _sync_announcements,
                        "手动触发持仓股公告采集（今日，若需更多日期请使用定时任务）")

    st.divider()

    conn = get_db_connection()
    if conn is None:
        st.warning("数据库未连接")
        return

    try:
        # 统计指标
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*), COUNT(DISTINCT ts_code), COUNT(DISTINCT ann_type)
            FROM research.announcements
            WHERE notice_date >= CURRENT_DATE - INTERVAL '30 days'
        """)
        row = cur.fetchone()
        total, stocks, types = row[0] or 0, row[1] or 0, row[2] or 0

        col1, col2, col3 = st.columns(3)
        col1.metric("公告总数", f"{total} 条")
        col2.metric("涉及股票", f"{stocks} 只")
        col3.metric("类型分布", f"{types} 种")

        st.divider()

        # 筛选
        cur.execute("SELECT DISTINCT ann_type FROM research.announcements ORDER BY ann_type")
        all_types = [r[0] for r in cur.fetchall()]
        selected_types = st.multiselect(
            "筛选公告类型", all_types, default=all_types[:6] if len(all_types) > 6 else all_types,
            format_func=lambda x: f"{x}",
            key="ann_type_filter"
        )

        cur.execute("""
            SELECT notice_date, ts_code, title, ann_type, url, ann_id
            FROM research.announcements
            WHERE notice_date >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY notice_date DESC, ts_code
        """)
        rows = cur.fetchall()
        cur.close()

        # 应用类型筛选
        if selected_types:
            rows = [r for r in rows if r[3] in selected_types]

        st.markdown(f"**共 {len(rows)} 条公告**")

        # 类型颜色映射
        TYPE_COLORS = {
            "年度报告": "🔵", "半年度报告": "🟦", "一季报": "🔷", "三季报": "🔶",
            "季报": "🟣", "业绩预告": "🟡", "董事会决议": "🟠", "股东大会": "🟧",
            "分红公告": "💰", "回购公告": "🔄", "增持公告": "📈", "减持公告": "📉",
            "股权激励": "🎯", "审计报告": "📋", "法律意见书": "⚖️",
            "核查意见": "✅", "监管措施": "🔴", "债券发行": "🏦",
            "重要事项": "⭐", "资质认定": "🏅", "投资者关系": "🤝",
            "退市风险": "⚠️", "一般公告": "📌",
        }
        def TYPE_EMOJI(t):
            return TYPE_COLORS.get(t, "📌")

        # 按日期分组展示
        current_date = None
        for notice_date, ts_code, title, ann_type, url, ann_id in rows:
            date_str = str(notice_date)

            if date_str != current_date:
                st.markdown(f"**📅 {date_str}**")
                current_date = date_str

            emoji = TYPE_EMOJI(ann_type)
            st.markdown(
                f"{emoji} `[{ts_code}]` **{title}**  "
                f"<span style='color:gray;font-size:0.85em'>[{ann_type}]</span> "
                f"<a href='{url}' target='_blank'>🔗 查看原文</a>",
                unsafe_allow_html=True,
            )

    except Exception as e:
        st.error(f"加载公告失败: {e}")
    finally:
        conn.close()


# ── 视图 3：历史决策日历 ────────────────────────────────────────────────────