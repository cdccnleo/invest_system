"""
Dashboard sub-module — L3 投资伙伴状态面板
展示 L3 主动对话引擎的能力激活状态、数据积累进度、触发器健康度及压力测试结果。
"""

import streamlit as st
import pandas as pd
from datetime import datetime


def render_l3_status():
    """L3 投资伙伴状态面板主视图"""
    st.markdown("## 🤖 L3 投资伙伴")
    st.caption("主动对话引擎 · 行为画像 · 压力测试 · 情绪感知")

    # ── 从数据库拉取 L3 状态 ──────────────────────────────────────────────
    try:
        from l3_dialog_engine import L3DialogEngine
        engine = L3DialogEngine()
        status = engine.get_l3_status()
    except Exception as e:
        st.error(f"无法获取 L3 状态: {e}")
        st.info("请确认 PostgreSQL 已启动且 l3 schema 已迁移（执行 l3_phase_a.sql）")
        return

    # ── 能力评分仪表 ──────────────────────────────────────────────────────
    col1, col2, col3 = st.columns(3)
    cap = status["capability_score"]
    cap_label = status["capability_label"]
    phase = status["phase"]

    with col1:
        st.metric("能力评分", f"{cap}/5", delta=cap_label)
        st.progress(cap / 5)
        st.caption(f"当前阶段: {phase}")

    with col2:
        records = status["behavior_records"]
        st.metric("行为记录数", records)
        target_tier = "萌芽 (≥10条)" if records < 10 else "激活 (≥50条)" if records < 50 else "成熟 (≥100条)"  # noqa: E501
        st.progress(min(records / 100, 1.0))
        st.caption(f"下一阶段: {target_tier}")

    with col3:
        stress_count = len(status["stress_tests"])
        st.metric("压力测试次数", stress_count)
        if stress_count > 0:
            last_st = status["stress_tests"][0]
            st.caption(f"最近测试: {last_st.get('executed_at', '?')[:10]} {last_st.get('scenario', '?')}")  # noqa: E501

    # ── 数据积累进度 ──────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 📊 数据积累进度")

    # 行为记录 vs 阶段解锁条件
    unlock_items = [
        ("Phase A 主动对话", 0, "触发器注册 + 调度就绪", "✅ 已完成"),
        ("Phase B 行为基线", 50, f"行为画像 ≥ 50 条（当前 {records} 条）",
         "🟢 已解锁" if records >= 50 else "⏳ 待积累"),
        ("Phase C 深度洞察", 100, f"连续 30 天行为记录（当前 {records} 条）",
         "🟢 已解锁" if records >= 100 else "⏳ 持续积累"),
    ]
    unlock_df = pd.DataFrame(unlock_items, columns=["阶段", "所需记录", "条件", "状态"])
    st.dataframe(unlock_df, hide_index=True, use_container_width=True)

    # ── 触发器状态 ────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### ⚡ 主动对话触发器")

    triggers = status.get("triggers", [])
    if triggers:
        tdata = []
        for t in triggers:
            last = t.get("last_triggered", "从未触发")
            if last and last != "None":
                try:
                    dt = datetime.fromisoformat(last.replace("Z", "+00:00") if "Z" in last else last)  # noqa: E501
                    last = dt.strftime("%m-%d %H:%M")
                except ValueError:
                    pass
            else:
                last = "—"
            icon = "🟢" if t["active"] else "⚫"
            tdata.append({
                "状态": icon,
                "触发器类型": t["type"],
                "激活": "是" if t["active"] else "否",
                "上次触发": last,
                "累计触发": t["count"],
            })
        st.dataframe(pd.DataFrame(tdata), hide_index=True, use_container_width=True)
    else:
        st.info("暂无触发器数据，请确认 l3.active_dialog_triggers 表已初始化")

    # ── 最近压力测试 ──────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 🧪 最近压力测试")

    stress_tests = status.get("stress_tests", [])
    if stress_tests:
        sdata = []
        for st_item in stress_tests:
            dt_str = st_item.get("executed_at", "?")
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00") if "Z" in dt_str else dt_str)  # noqa: E501
                dt_str = dt.strftime("%m-%d %H:%M")
            except ValueError:
                pass
            sdata.append({
                "时间": dt_str,
                "情景": st_item.get("scenario", "?"),
                "最大损失": f"{st_item.get('loss_pct', 0):.2f}%",
                "风险评分": f"{st_item.get('risk_score', 0)}/10",
            })
        st.dataframe(pd.DataFrame(sdata), hide_index=True, use_container_width=True)
    else:
        st.info("尚未执行压力测试。调度任务每周五 22:00 自动执行，也可在设置页面手动触发。")

    # ── 调度任务概览 ──────────────────────────────────────────────────────
    st.divider()
    st.markdown("### ⏰ 自动化调度")
    schedule_items = [
        {"任务": "行为画像更新", "频率": "每日 15:40 (收盘后)", "状态": "✅ 已注册"},
        {"任务": "压力测试", "频率": "每周五 22:00", "状态": "✅ 已注册"},
        {"任务": "行为洞察报告", "频率": "每周日 20:00", "状态": "✅ 已注册"},
        {"任务": "用户情绪感知", "频率": "每日 21:30", "状态": "✅ 已注册"},
    ]
    st.dataframe(pd.DataFrame(schedule_items), hide_index=True, use_container_width=True)

    # ── 手动操作 ──────────────────────────────────────────────────────────
    st.divider()
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("🔄 立即运行触发器评估", help="手动触发一轮 L3 触发器评估",
                     key="btn_run_l3_cycle"):
            # 关键: 先把结果存到 session_state, 再 toast + rerun
            # 否则 rerun 立刻销毁 spinner/success 容器, 用户看不到任何反馈
            try:
                with st.spinner("正在评估触发器..."):
                    result = engine.run_cycle()
                st.session_state["_l3_cycle_result"] = {
                    "ok": True,
                    "evaluated": result["evaluated"],
                    "triggered": result["triggered"],
                    "details": result["details"],
                }
            except Exception as e:
                st.session_state["_l3_cycle_result"] = {
                    "ok": False, "error": str(e),
                }
            st.rerun()

        # 显示上次结果 (rerun 之后页面顶部会重画 status, 这里再补一块 detail 卡片)
        prev = st.session_state.get("_l3_cycle_result")
        if prev:
            st.divider()
            st.markdown("##### 📋 最近一次手动评估")
            if prev.get("ok"):
                st.success(f"评估完成: {prev['evaluated']} 个触发器, "
                           f"{prev['triggered']} 个触发")
                for d in prev["details"]:
                    icon = {"triggered": "✅", "not_triggered": "➖",
                            "push_failed": "⚠️", "error": "❌"}.get(d["status"], "?")
                    st.caption(f"  {icon} [{d.get('trigger_type', '?')}] "
                               f"{d.get('trigger_name', d.get('trigger_type', '?'))}: "
                               f"{d['status']}")
            else:
                st.error(f"评估失败: {prev.get('error')}")

    with col_b:
        if st.button("🧪 立即运行压力测试", help="执行 5 种情景压力测试",
                     key="btn_run_l3_stress"):
            # 同 L134 按钮: 用 session_state 保存结果再 rerun, 否则反馈被销毁
            try:
                with st.spinner("正在执行压力测试..."):
                    run_id = engine.run_stress_test()
                st.session_state["_l3_stress_result"] = {"ok": True, "run_id": run_id}
            except Exception as e:
                st.session_state["_l3_stress_result"] = {"ok": False, "error": str(e)}
            st.rerun()

        prev = st.session_state.get("_l3_stress_result")
        if prev:
            st.divider()
            st.markdown("##### 📋 最近一次压力测试")
            if prev.get("ok"):
                st.success(f"压力测试完成: {prev['run_id']}")
            else:
                st.error(f"压力测试失败: {prev.get('error')}")