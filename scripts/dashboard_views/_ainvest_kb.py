"""
_ainvest_kb.py — AInvest 深度知识库仪表盘视图

功能:
  - 按类型/日期筛选报告列表
  - 查看报告详情与结构化提取结果
  - 按标的查看关联知识图谱
  - 手动触发知识库更新同步
  - 最近扫描审计记录
"""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta


def render_ainvest_kb():
    """AInvest 知识库主视图"""
    st.markdown("## 📚 AInvest 深度知识库")
    st.caption("从 C:\\PythonProject\\AInvest\\reports 自动解析的投资分析报告知识库")

    tab_overview, tab_by_stock, tab_signals, tab_semantic, tab_settings = st.tabs([
        "📋 报告总览", "🏷️ 按标的查询", "📊 信号提取", "🔍 语义搜索", "⚙️ 同步设置"
    ])

    with tab_overview:
        _render_report_overview()

    with tab_by_stock:
        _render_by_stock()

    with tab_signals:
        _render_signals()

    with tab_semantic:
        _render_semantic_search()

    with tab_settings:
        _render_settings()


@st.cache_resource(ttl=3600)
def _get_db_conn():
    """获取数据库连接"""
    from storage_factory import get_pg_connection
    conn = get_pg_connection()
    if conn is None:
        st.warning("⚠️ 数据库不可用")
    return conn


@st.cache_data(ttl=300)
def report_count_by_type():
    """按类型统计报告数量"""
    conn = _get_db_conn()
    if conn is None:
        return {}
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT report_type, COUNT(*) as cnt
            FROM ainvest_kb.parsed_reports
            GROUP BY report_type
        """)
        rows = cur.fetchall()
        cur.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


@st.cache_data(ttl=300)
def recent_scan_stats():
    """获取最近扫描统计"""
    conn = _get_db_conn()
    if conn is None:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT scan_start, total_files, new_files, changed_files,
                   parsed_ok, parsed_failed
            FROM ainvest_kb.scan_audit
            ORDER BY id DESC LIMIT 5
        """)
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception:
        return []
    finally:
        conn.close()


def _render_report_overview():
    """报告总览：按类型和日期筛选（支持分页）"""
    conn = _get_db_conn()
    if conn is None:
        return

    # 初始化分页状态
    if "kb_page" not in st.session_state:
        st.session_state.kb_page = 1
    if "kb_page_size" not in st.session_state:
        st.session_state.kb_page_size = 20

    col1, col2, col3 = st.columns(3)
    with col1:
        report_type = st.selectbox("报告类型", ["全部", "events", "trackers", "deep-analysis", "daily"])
    with col2:
        date_range = st.selectbox("时间范围", ["最近7天", "最近30天", "最近90天", "全部"])
    with col3:
        keyword = st.text_input("关键词搜索", placeholder="股票代码或关键词")

    # 分页控件行
    col_size, col_nav, col_total = st.columns([1, 3, 1])
    with col_size:
        page_size = st.selectbox(
            "每页条数",
            [10, 20, 50, 100],
            index=[10, 20, 50, 100].index(st.session_state.kb_page_size) if st.session_state.kb_page_size in [10, 20, 50, 100] else 1,
            key="kb_page_size_select",
            label_visibility="collapsed",
        )
        if page_size != st.session_state.kb_page_size:
            st.session_state.kb_page_size = page_size
            st.session_state.kb_page = 1
            st.rerun()

    # 构建查询条件
    try:
        cur = conn.cursor()

        where_clauses = []
        params = []

        if report_type != "全部":
            where_clauses.append("pr.report_type = %s")
            params.append(report_type)

        if date_range == "最近7天":
            where_clauses.append("pr.report_date >= %s")
            params.append((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))
        elif date_range == "最近30天":
            where_clauses.append("pr.report_date >= %s")
            params.append((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
        elif date_range == "最近90天":
            where_clauses.append("pr.report_date >= %s")
            params.append((datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"))

        if keyword:
            where_clauses.append("(pr.title ILIKE %s OR %s = ANY(pr.related_codes))")
            keyword_param = f"%{keyword}%"
            params.extend([keyword_param, keyword.zfill(6)])

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # 总数查询
        cur.execute(f"""
            SELECT COUNT(*) FROM ainvest_kb.parsed_reports pr WHERE {where_sql}
        """, params)
        total_count = cur.fetchone()[0]

        if total_count == 0:
            st.info("暂无匹配报告。请先执行知识库同步。")
            cur.close()
            conn.close()
            return

        # 计算分页
        page_size = st.session_state.kb_page_size
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        current_page = st.session_state.kb_page

        if current_page > total_pages:
            current_page = total_pages
            st.session_state.kb_page = current_page

        offset = (current_page - 1) * page_size

        # 分页导航
        with col_nav:
            c_prev, c_page, c_next = st.columns([1, 2, 1])
            with c_prev:
                if st.button("◀ 上一页", disabled=(current_page <= 1), key="kb_prev"):
                    st.session_state.kb_page -= 1
                    st.rerun()
            with c_page:
                st.markdown(f"<div style='text-align:center;padding-top:5px'>第 <strong>{current_page}</strong> / {total_pages} 页</div>", unsafe_allow_html=True)
            with c_next:
                if st.button("下一页 ▶", disabled=(current_page >= total_pages), key="kb_next"):
                    st.session_state.kb_page += 1
                    st.rerun()

        with col_total:
            st.markdown(f"<div style='text-align:right;padding-top:5px'>共 <strong>{total_count}</strong> 条</div>", unsafe_allow_html=True)

        # 主数据查询（带 LIMIT/OFFSET）
        cur.execute(f"""
            SELECT
                pr.id,
                pr.report_type,
                pr.title,
                pr.report_date,
                pr.primary_stock_code,
                pr.confidence_score,
                pr.summary,
                array_length(pr.related_codes, 1) as code_count,
                pr.parsed_at
            FROM ainvest_kb.parsed_reports pr
            WHERE {where_sql}
            ORDER BY pr.report_date DESC, pr.parsed_at DESC
            LIMIT %s OFFSET %s
        """, params + [page_size, offset])

        rows = cur.fetchall()

        # 统计概览（基于当前页筛选后的内存统计，供参考）
        type_counts = {}
        for r in rows:
            t = r[1]
            type_counts[t] = type_counts.get(t, 0) + 1

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("报告总数", total_count)
        with c2:
            st.metric("事件分析", type_counts.get("events", 0))
        with c3:
            st.metric("个股跟踪", type_counts.get("trackers", 0))
        with c4:
            st.metric("深度分析", type_counts.get("deep-analysis", 0))

        # 报告列表
        st.divider()

        type_icons = {
            "events": "🔴", "trackers": "📌", "deep-analysis": "🔬", "daily": "📅"
        }

        for row in rows:
            rid, rtype, title, rdate, pcode, conf, summary, code_count, parsed_at = row
            icon = type_icons.get(rtype, "📄")
            date_str = rdate.strftime("%Y-%m-%d") if rdate else "?"
            conf_str = f"{conf:.0%}" if conf else "?"

            with st.expander(f"{icon} [{date_str}] {title[:60]}"):
                col_a, col_b = st.columns([3, 1])
                with col_a:
                    st.markdown(f"**摘要**: {summary or '(无摘要)'}")
                    st.markdown(f"**关联股票**: {code_count or 0} 只")
                    if pcode:
                        st.markdown(f"**主标的**: {pcode}")
                with col_b:
                    st.metric("置信度", conf_str)
                    st.caption(f"解析时间: {parsed_at.strftime('%m-%d %H:%M') if parsed_at else '?'}")

                # 查看详细信号
                if st.button("📊 查看提取信号", key=f"sig_{rid}"):
                    _render_report_signals(conn, rid)

                # 查看原始报告
                if st.button("📄 查看原始报告", key=f"raw_{rid}"):
                    _render_raw_report(conn, rid)

        cur.close()
    except Exception as e:
        st.error(f"查询失败: {e}")
    finally:
        conn.close()


def _render_report_signals(conn, report_id: int):
    """显示报告的提取信号"""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT investment_signals, key_judgments, risk_assessment, operation_actions
            FROM ainvest_kb.parsed_reports WHERE id = %s
        """, (report_id,))
        row = cur.fetchone()
        if row:
            import json
            signals = row[0] if isinstance(row[0], list) else (json.loads(row[0]) if row[0] else [])
            judgments = row[1] if isinstance(row[1], list) else (json.loads(row[1]) if row[1] else [])
            risk = row[2]
            actions = row[3] if isinstance(row[3], list) else (json.loads(row[3]) if row[3] else [])

            if risk:
                st.warning(f"⚠️ 风险评估: {risk}")

            if signals:
                st.markdown("**投资信号**:")
                for s in signals:
                    if isinstance(s, dict):
                        st.markdown(f"- [{s.get('type', '?')}] {s.get('content', str(s))[:200]}")
                    else:
                        st.markdown(f"- {str(s)[:200]}")

            if judgments:
                st.markdown("**核心判断**:")
                for j in judgments:
                    st.markdown(f"- {str(j)[:200]}")

            if actions:
                st.markdown("**操作建议**:")
                for a in actions:
                    if isinstance(a, dict):
                        st.markdown(f"- {a.get('content', str(a))[:200]}")
                    else:
                        st.markdown(f"- {str(a)[:200]}")
        cur.close()
    except Exception as e:
        st.error(f"加载信号失败: {e}")


def _render_raw_report(conn, report_id: int):
    """显示原始报告文本"""
    try:
        cur = conn.cursor()
        cur.execute("SELECT raw_text FROM ainvest_kb.parsed_reports WHERE id = %s", (report_id,))
        row = cur.fetchone()
        if row:
            st.text_area("原始文本", row[0] or "", height=300)
        cur.close()
    except Exception as e:
        st.error(f"加载原始报告失败: {e}")


def _render_by_stock():
    """按标的查询关联知识 — 懒加载版本"""
    conn = _get_db_conn()
    if conn is None:
        return

    # 获取持仓列表（轻量查询，先展示）
    try:
        cur = conn.cursor()
        cur.execute("SELECT ts_code, stock_name FROM memory.target_memory_files ORDER BY ts_code")
        stocks = cur.fetchall()
        cur.close()

        if not stocks:
            st.info("未找到持仓标的")
            return

        stock_options = {f"{code} - {name}": code for code, name in stocks}
        selected = st.selectbox("选择持仓标的", list(stock_options.keys()))
        selected_code = stock_options[selected]

        # 懒加载：详情按需查询
        reports_placeholder = st.empty()
        if st.button("📊 加载关联报告", key="load_stock_reports"):
            with reports_placeholder.container():
                cur = conn.cursor()
                cur.execute("""
                    SELECT
                        pr.id, pr.report_type, pr.title, pr.report_date,
                        pr.summary, pr.confidence_score, skl.relevance_score
                    FROM ainvest_kb.stock_kb_links skl
                    JOIN ainvest_kb.parsed_reports pr ON pr.id = skl.report_id
                    WHERE skl.ts_code = %s
                    ORDER BY skl.relevance_score DESC, pr.report_date DESC
                    LIMIT 50
                """, (selected_code,))
                rows = cur.fetchall()
                cur.close()

                if not rows:
                    st.info(f"📭 {selected} 暂无关联知识库报告")
                else:
                    st.markdown(f"**{selected}** 关联 {len(rows)} 份报告")
                    for rid, rtype, title, rdate, summary, conf, relevance in rows:
                        date_str = rdate.strftime("%Y-%m-%d") if rdate else "?"
                        with st.expander(f"[{date_str}] {title[:60]} (关联度: {relevance:.0%})"):
                            st.markdown(f"**摘要**: {summary or '(无摘要)'}")
                            if st.button("📊 查看信号", key=f"stock_sig_{rid}"):
                                _render_report_signals(conn, rid)

    except Exception as e:
        st.error(f"查询失败: {e}")
    finally:
        conn.close()


def _render_signals():
    """信号提取一览 — 懒加载版本"""
    conn = _get_db_conn()
    if conn is None:
        return

    col1, col2 = st.columns(2)
    with col1:
        signal_type = st.selectbox("信号类型", ["全部", "exposure_assessment", "stop_loss_adjustment", "valuation_analysis", "investment_advice"])
    with col2:
        direction = st.selectbox("信号方向", ["全部", "positive", "negative", "neutral"])

    # 懒加载：手动触发按钮 + st.empty() 占位
    signals_placeholder = st.empty()
    if st.button("🔍 加载信号数据", key="load_signals_btn"):
        with signals_placeholder.container():
            _execute_signals_query(conn, signal_type, direction)

    # 初始状态提示
    if not signals_placeholder._parent_block:
        st.caption("💡 点击上方按钮加载信号数据")


def _execute_signals_query(conn, signal_type, direction):
    """执行信号查询（供懒加载调用）"""
    try:
        cur = conn.cursor()

        where = ["1=1"]
        params = []

        if signal_type != "全部":
            where.append("pr.investment_signals::text ILIKE %s")
            params.append(f"%{signal_type}%")

        cur.execute(f"""
            SELECT
                pr.id, pr.report_type, pr.title, pr.report_date,
                pr.primary_stock_code, pr.investment_signals,
                pr.risk_assessment, pr.confidence_score
            FROM ainvest_kb.parsed_reports pr
            WHERE {' AND '.join(where)}
            ORDER BY pr.report_date DESC
            LIMIT 100
        """, params)

        rows = cur.fetchall()
        cur.close()

        shown = 0
        for row in rows:
            rid, rtype, title, rdate, pcode, signals, risk, conf = row
            import json
            signals_list = signals if isinstance(signals, list) else (json.loads(signals) if signals else [])

            # 方向筛选
            if direction != "全部" and signals_list:
                matched = any(
                    s.get("direction") == direction if isinstance(s, dict) else direction in str(s)
                    for s in signals_list
                )
                if not matched:
                    continue

            shown += 1
            date_str = rdate.strftime("%Y-%m-%d") if rdate else "?"
            st.markdown(f"**[{date_str}] {title[:60]}** ({pcode or 'N/A'})")
            if risk:
                st.caption(f"⚠️ {risk[:100]}")
            st.divider()

        if shown == 0:
            st.info("暂无匹配信号")

    except Exception as e:
        st.error(f"查询失败: {e}")
    finally:
        conn.close()


def _render_semantic_search():
    """语义搜索面板 — 基于向量相似度的 AInvest 知识检索"""
    st.markdown("### 🔍 语义知识搜索")

    col_search, col_k = st.columns([4, 1])
    with col_search:
        query = st.text_input(
            "搜索查询",
            placeholder="例如：非农数据对半导体行业的影响、AI算力需求趋势",
            label_visibility="collapsed",
        )
    with col_k:
        top_k = st.number_input("返回条数", min_value=1, max_value=20, value=5, label_visibility="collapsed")

    if st.button("🔍 搜索", type="primary", use_container_width=True):
        if not query.strip():
            st.warning("请输入搜索关键词")
            return

        with st.spinner("正在检索语义相似报告..."):
            try:
                from kb_ainvest_worker import search_ainvest_knowledge
                results = search_ainvest_knowledge(query, top_k=top_k)
            except Exception as e:
                st.error(f"搜索失败: {e}")
                return

        if not results:
            st.info("未找到相关报告，请尝试其他关键词")
            return

        st.success(f"找到 {len(results)} 条相关知识")

        for i, r in enumerate(results):
            with st.expander(f"[{r['date']}] {r['title'][:60]} (相似度: {r['similarity']:.1%})"):
                st.markdown(f"**报告**: {r['title']}")
                st.markdown(f"**类型**: {r.get('report_type', 'N/A')}")
                st.markdown(f"**相似度**: {r['similarity']:.1%}")
                st.markdown("**内容摘要**:")
                st.info(r["content"][:500] if r["content"] else "(无内容)")

                # 关联冲突检测
                if r.get("related_codes"):
                    try:
                        from kb_ainvest_worker import detect_knowledge_conflict
                        for code in r["related_codes"][:3]:
                            sigs = r.get("signals", [])
                            conflicts = detect_knowledge_conflict(code, sigs)
                            if conflicts:
                                for c in conflicts:
                                    st.warning(
                                        f"⚠️ 冲突检测 [{code}]: {c['type']} — "
                                        f"AInvest: {c.get('ainvest', '')} vs TAMF: {c.get('tamf', '')}"
                                    )
                    except Exception:
                        pass
    else:
        st.caption("💡 输入自然语言查询，语义搜索将找到最相关的报告片段")



def _render_settings():
    """同步设置面板"""
    col1, col2 = st.columns([2, 1])

    with col2:
        if st.button("🔄 立即同步", type="primary", use_container_width=True):
            with st.spinner("正在扫描 AInvest 报告目录并解析..."):
                try:
                    from kb_ainvest_worker import process_ainvest_reports
                    result = process_ainvest_reports()
                    if result.get("status") == "completed":
                        st.success(
                            f"✅ 扫描 {result['total_scanned']} 份 → "
                            f"新增 {result['new']} / 变更 {result['changed']} / "
                            f"成功 {result['parsed_ok']} / 失败 {result['parsed_failed']}"
                        )
                    else:
                        st.info(result.get("reason", "跳过"))
                except Exception as e:
                    st.error(f"同步失败: {e}")

    with col1:
        st.markdown("### 定时同步配置")
        st.caption("以下任务已注册到 APScheduler，自动执行：")
        st.markdown("""
        | 时间 | 任务 | 说明 |
        |:---:|------|------|
        | 07:30 | 盘前检查 | 检查前日 AInvest 报告更新 |
        | 15:30 | 盘后扫描 | 采集当日新增报告 |
        | 21:30 | 晚间完整处理 | 深度解析 + 向量嵌入 + TAMF 联动 |
        """)

        # 缓存管理
        st.markdown("### 缓存管理")
        col_clear, col_spacer = st.columns([1, 2])
        with col_clear:
            if st.button("🗑️ 清除缓存", use_container_width=True):
                st.cache_data.clear()
                st.success("缓存已清除")
        st.caption("知识库连接缓存 (1小时) | 统计数据缓存 (5分钟)")

        # 显示最近扫描审计（懒加载）
        audit_placeholder = st.empty()
        if st.button("📋 加载扫描记录", key="load_audit_btn"):
            with audit_placeholder.container():
                try:
                    conn = _get_db_conn()
                    if conn:
                        cur = conn.cursor()
                        cur.execute("""
                            SELECT scan_start, total_files, new_files, changed_files,
                                   parsed_ok, parsed_failed
                            FROM ainvest_kb.scan_audit
                            ORDER BY id DESC LIMIT 5
                        """)
                        audits = cur.fetchall()
                        cur.close()
                        conn.close()

                        if audits:
                            st.markdown("### 最近扫描记录")
                            audit_data = []
                            for a in audits:
                                audit_data.append({
                                    "时间": a[0].strftime("%m-%d %H:%M") if a[0] else "?",
                                    "扫描文件": a[1],
                                    "新增": a[2],
                                    "变更": a[3],
                                    "成功": a[4],
                                    "失败": a[5],
                                })
                            st.dataframe(pd.DataFrame(audit_data), use_container_width=True)
                except Exception:
                    pass
