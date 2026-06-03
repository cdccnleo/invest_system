"""
Dashboard sub-module — Plan Review / Settings
Generated from dashboard.py (1397-1636 lines)
Each function accesses streamlit via st (passed through from main module).
"""

import streamlit as st
from pathlib import Path as _Path
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).parent.parent))
from dashboard import ensure_plan_review_table, POSITIONS_CSV
from ._shared import get_db_connection

def render_plan_review():
    """计划审核页面：读取历史分析中的 plans，滑块+勾选批准/否决，写入 plan_reviews 并记录到 audit_log"""
    st.markdown("## 📝 计划审核")
    st.caption("查看近期分析生成的交易计划，逐项审批执行额度")

    # 初始化 session_state
    if "plan_reviews" not in st.session_state:
        st.session_state["plan_reviews"] = {}

    conn = get_db_connection()
    if not conn:
        st.error("无法连接数据库")
        return
    ensure_plan_review_table(conn)

    # 读取近7日有 plans 的分析记录（只看 plans 非空且非 [] 的记录）
    cur = conn.cursor()
    cur.execute("""
        SELECT run_id, started_at, detail, plans, confidence
        FROM analysis.analysis_runs
        WHERE started_at >= CURRENT_DATE - INTERVAL '7 days'
          AND (plans IS NOT NULL AND plans::text != 'null' AND plans::text != '[]')
        ORDER BY started_at DESC
        LIMIT 20
    """)
    rows = cur.fetchall()

    if not rows:
        st.info("近7日无含计划的分析记录（或计划正文未存储，请确认 run_analysis.py 已更新）")
        conn.close()
        return

    # 构建 run_id → 分析详情的映射
    runs = []
    import json as _json
    for run_id, started_at, detail_raw, plans_raw, confidence in rows:
        try:
            detail = detail_raw if isinstance(detail_raw, dict) else (_json.loads(detail_raw) if detail_raw else {})
        except Exception:
            detail = {}
        plans = plans_raw if isinstance(plans_raw, list) else (_json.loads(plans_raw) if plans_raw else [])
        if plans:
            runs.append({
                "run_id": run_id,
                "started_at": started_at,
                "plans": plans,
                "confidence": confidence or detail.get("confidence", "N/A"),
            })

    if not runs:
        st.info("近期分析无交易计划")
        conn.close()
        return

    st.divider()

    # 读取已有的审核记录
    cur.execute("SELECT run_id, plan_index, decision, position_pct, reason FROM analysis.plan_reviews")
    reviewed = {}
    for row in cur.fetchall():
        reviewed[(row[0], row[1])] = {
            "decision": row[2], "position_pct": row[3], "reason": row[4]
        }

    plan_reviews = st.session_state["plan_reviews"]
    changed = False

    for run in runs:
        run_id = run["run_id"]
        started_at = str(run["started_at"])[:16]
        confidence = run["confidence"]

        with st.expander(f"📌 {started_at}  |  {len(run['plans'])} 项计划  |  置信度: {confidence}", expanded=False):
            for i, plan in enumerate(run["plans"]):
                plan_id = f"{run_id}_{i}"
                key = (run_id, i)
                existing = reviewed.get(key, {})
                default_decision = existing.get("decision", "")
                default_pct = existing.get("position_pct", 50)
                default_reason = existing.get("reason", "")

                # 当前会话中的审核决策
                current_decision = plan_reviews.get(plan_id, default_decision)

                col_label, col_action = st.columns([0.7, 0.3])
                with col_label:
                    st.markdown(f"**[{i+1}] {plan.get('action', 'N/A')}** "
                                    f"`{plan.get('ts_code', '')}` {plan.get('name', '')}")
                    st.caption(f"入场 {plan.get('limit_price', 'N/A')} | 仓位 {plan.get('position_pct', 'N/A')}%")

                # 批准/否决勾选框
                approve_key = f"approve_{plan_id}"
                reject_key = f"reject_{plan_id}"

                col_cb1, col_cb2, col_cb3 = st.columns([0.15, 0.15, 0.7])
                with col_cb1:
                    # 已审核的显示标签，不重复勾选
                    if existing and existing.get("decision"):
                        st.caption(f"{'✅ 已批准' if existing['decision'] == 'approved' else '❌ 已否决'}")
                    else:
                        is_approved = st.checkbox("✅ 批准", value=(current_decision == "approved"),
                                                  key=approve_key)
                        if is_approved:
                            plan_reviews[plan_id] = "approved"
                            changed = True
                        elif plan_reviews.get(plan_id) == "approved":
                            del plan_reviews[plan_id]
                            changed = True

                with col_cb2:
                    if not (existing and existing.get("decision")):
                        is_rejected = st.checkbox("❌ 否决", value=(current_decision == "rejected"),
                                                  key=reject_key)
                        if is_rejected:
                            plan_reviews[plan_id] = "rejected"
                            changed = True
                        elif plan_reviews.get(plan_id) == "rejected":
                            del plan_reviews[plan_id]
                            changed = True

                with col_cb3:
                    if current_decision in ("approved", "rejected"):
                        pos_pct = st.slider(
                            "执行仓位%",
                            min_value=10, max_value=100,
                            value=default_pct,
                            step=10,
                            key=f"pct_{plan_id}",
                        )
                        reason = st.text_input(
                            "备注",
                            value=default_reason,
                            key=f"reason_{plan_id}",
                            placeholder="同意/否决原因...",
                        )
                    elif existing:
                        st.caption(f"已审核: {'✅ 批准' if default_decision == 'approved' else '❌ 否决'} ({default_pct}%) | {default_reason or '无备注'}")

    # ── 审核提交按钮 ─────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 📋 审核汇总")

    pending_approvals = {k: v for k, v in plan_reviews.items() if v in ("approved", "rejected")}
    approved_count = sum(1 for v in pending_approvals.values() if v == "approved")
    rejected_count = sum(1 for v in pending_approvals.values() if v == "rejected")

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        st.metric("待提交-已批准", f"{approved_count} 条")
    with col_s2:
        st.metric("待提交-已否决", f"{rejected_count} 条")

    if st.button("✅ 确认审核（写入 audit_log）", key="submit_plan_reviews", type="primary"):
        if not pending_approvals:
            st.warning("暂无待提交的审核")
        else:
            from storage_factory import get_storage
            storage = get_storage()

            # 汇总写入 audit_log
            summary = {
                "approved": approved_count,
                "rejected": rejected_count,
                "plans": [
                    {"plan_id": pid, "decision": dec}
                    for pid, dec in pending_approvals.items()
                ]
            }
            ok = storage.write_audit(
                event_type="PLAN_REVIEWED",
                operator="manual",
                detail=summary,
                result="SUCCESS"
            )
            if ok:
                # 同步写入 plan_reviews 表（每条）
                for run in runs:
                    run_id = run["run_id"]
                    for i, plan in enumerate(run["plans"]):
                        plan_id = f"{run_id}_{i}"
                        dec = pending_approvals.get(plan_id)
                        if not dec:
                            continue
                        pct = st.session_state.get(f"pct_{plan_id}", 50)
                        reason = st.session_state.get(f"reason_{plan_id}", "") or ""
                        cur.execute("""
                            INSERT INTO analysis.plan_reviews (run_id, plan_index, decision, position_pct, reason)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (run_id, plan_index)
                            DO UPDATE SET decision = EXCLUDED.decision,
                                          position_pct = EXCLUDED.position_pct,
                                          reason = EXCLUDED.reason,
                                          reviewed_at = CURRENT_TIMESTAMP
                        """, (run_id, i, dec, pct, reason))
                conn.commit()
                st.success(f"已批准 {approved_count} 条 / 已否决 {rejected_count} 条 — 已写入 audit.audit_log")
                # 清空已提交
                for pid in pending_approvals:
                    del plan_reviews[pid]
                st.rerun()
            else:
                st.error("写入 audit_log 失败")

    conn.close()


# ── 视图 8：设置 ───────────────────────────────────────────────────────

def render_settings():
    st.markdown("## ⚙️ 系统设置")
    st.info("配置管理面板（Phase 2 v0.1）")

    st.markdown("### 数据源")
    st.markdown(f"- 持仓文件: `{POSITIONS_CSV}`")
    st.markdown(f"- 数据库: `postgresql://invest_admin@localhost:5432/investpilot`")
    st.markdown(f"- 行情: 东方财富基金 API + 新浪财经")
    st.markdown(f"- 新闻: 同花顺快讯 + 新浪财经 + 金十数据（财联社已停用）")
    st.markdown(f"- 研报: 东方财富研报 API（16:00 每日采集）")

    st.markdown("### 定时任务")
    st.markdown("- 08:30 盘前工作流")
    st.markdown("- 15:30 盘后工作流")
    st.markdown("- 16:00 研报采集工作流")
    st.markdown("- 21:00 晚间工作流")
    st.markdown("- 每日向量嵌入任务（新闻 + 研报）")

    if st.button("🔄 手动触发盘前分析"):
        with st.spinner("运行中..."):
            from schedule_runner import job_morning
            job_morning()
        st.success("盘前分析完成！")

    if st.button("📊 手动触发向量化"):
        with st.spinner("向量化新闻..."):
            from embedding_service import daily_embedding_job
            daily_embedding_job()
        st.success("向量化完成！")


