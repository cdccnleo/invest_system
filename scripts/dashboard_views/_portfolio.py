"""
Dashboard sub-module — Portfolio Dashboard
Generated from dashboard.py (411-623 lines)
Each function accesses streamlit via st (passed through from main module).
"""

from ._shared import get_db_connection, load_positions, get_latest_quotes_from_db

# ── 视图 1：持仓仪表板 ──────────────────────────────────────────────────────

def render_portfolio_dashboard():
    positions = load_positions()
    if not positions:
        st.error("无持仓数据，请检查 positions.csv")
        return

    df = pd.DataFrame(positions)
    total_mv = df["市值"].sum()

    # 顶部 KPI 卡片
    st.markdown("## 📋 持仓仪表板")
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)

    # 估算总盈亏（需要成本）
    total_cost = (df["份额"] * df["成本"]).sum()
    total_pnl = total_mv - total_cost
    pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    with kpi1:
        st.metric(
            "💰 总市值",
            f"¥{total_mv:,.0f}",
            delta=f"{pnl_pct:+.1f}%" if pnl_pct != 0 else None,
        )
    with kpi2:
        st.metric("📈 总盈亏", f"{pnl_pnl_str(total_pnl)}")
    with kpi3:
        fund_count = len(df[df["类型"] == "fund"])
        st.metric("📊 基金数", fund_count)
    with kpi4:
        stock_count = len(df[df["类型"] == "stock"])
        st.metric("🏦 股票数", stock_count)

    st.divider()

    # 初始化持仓调整 session_state
    if "holdings_adjustments" not in st.session_state:
        st.session_state["holdings_adjustments"] = {}

    # 主内容区
    col_left, col_right = st.columns([2, 1])

    with col_left:
        # 持仓调整滑块交互区
        st.markdown("### 📐 持仓模拟调整")

        # 调整模式开关
        adjust_mode = st.checkbox("🔧 开启持仓模拟调整", value=False, key="adjust_mode_toggle")

        if adjust_mode:
            st.caption("💡 拖动滑块可模拟50%~150%仓位变化，仅用于交互预览，不实际修改持仓")

            # 按代码建索引便于查找
            df["代码"] = df["代码"].astype(str)
            adj_state = st.session_state["holdings_adjustments"]

            for idx, row in df.iterrows():
                code = row["代码"]
                name = row["名称"]
                current_shares = row["份额"]
                current_mv = row["市值"]
                cost = row["成本"]
                current_pct = row["仓位%"]

                # 默认调整比例 = 100%（不变）
                default_adj = adj_state.get(code, 100)
                adj_pct = st.slider(
                    f"**{name}** (`{code}`)",
                    min_value=50,
                    max_value=150,
                    value=default_adj,
                    step=10,
                    key=f"adj_{code}",
                    help=f"当前市值: ¥{current_mv:,.2f} | 仓位: {current_pct:.2f}%"
                )

                # 计算模拟市值
                simulated_shares = current_shares * adj_pct / 100
                simulated_mv = simulated_shares * cost  # 按成本价估算
                mv_delta = simulated_mv - current_mv
                delta_pct = ((adj_pct / 100 - 1) * 100)

                # 颜色标注模拟结果
                if adj_pct > 100:
                    delta_color = "📈"
                    delta_str = f"+¥{mv_delta:,.2f} (+{delta_pct:.0f}%)"
                elif adj_pct < 100:
                    delta_color = "📉"
                    delta_str = f"-¥{abs(mv_delta):,.2f} ({delta_pct:.0f}%)"
                else:
                    delta_color = "➖"
                    delta_str = "±¥0 (0%)"

                st.markdown(
                    f"　　模拟市值: **{delta_color} ¥{simulated_mv:,.2f}** "
                    f"　变化: {delta_str}"
                )

                # 保存到 session_state
                adj_state[code] = adj_pct

            # 汇总模拟调整结果
            total_simulated = sum(
                df.loc[df["代码"] == code, "份额"].values[0] * adj_state.get(code, 100) / 100
                * df.loc[df["代码"] == code, "成本"].values[0]
                for code in adj_state
                if code in df["代码"].values
            )
            total_original = df["市值"].sum()
            total_change = total_simulated - total_original
            st.divider()
            col_sum1, col_sum2, col_sum3 = st.columns(3)
            with col_sum1:
                st.metric("原始总市值", f"¥{total_original:,.2f}")
            with col_sum2:
                st.metric("模拟总市值", f"¥{total_simulated:,.2f}",
                          delta=f"{total_change:+,.2f}" if total_change != 0 else None)
            with col_sum3:
                change_pct = (total_change / total_original * 100) if total_original > 0 else 0
                st.metric("模拟变化率", f"{change_pct:+.1f}%")

            # 提交模拟记录
            if st.button("📝 提交模拟记录到审核日志", key="submit_simulation"):
                from storage_factory import StorageFactory
                storage = StorageFactory()
                storage.write_audit(
                    event_type="SIMULATION_SUBMITTED",
                    operator="manual",
                    detail={
                        "adjustments": adj_state,
                        "total_original": total_original,
                        "total_simulated": total_simulated,
                    },
                    result="SUCCESS"
                )
                st.success("模拟记录已写入审核日志")

            st.divider()

        # 持仓明细表
        st.markdown("### 持仓明细")

        # 计算盈亏列（市值 - 份额 × 成本）
        df["盈亏"] = (df["市值"] - df["份额"] * df["成本"]).round(2)
        df["盈亏%"] = (((df["市值"] / (df["份额"] * df["成本"])) - 1) * 100).round(2).replace([float("inf"), float("-inf")], 0).fillna(0)

        # 类型映射
        type_icon = {"fund": "📊", "stock": "🏦", "etf": "📈"}
        df["类型图标"] = df["类型"].map(type_icon).fillna("📋")

        display_df = df[["代码", "名称", "类型图标", "成本", "市值", "仓位%", "盈亏"]].copy()
        display_df["成本"] = display_df["成本"].map(lambda x: f"¥{x:.4f}" if x < 100 else f"¥{x:.2f}")
        display_df["市值"] = display_df["市值"].map(lambda x: f"¥{x:,.2f}")
        display_df["仓位%"] = display_df["仓位%"].map(lambda x: f"{x:.2f}%")
        display_df["盈亏"] = display_df["盈亏"].map(lambda x: f"{'+¥' if x >= 0 else '-¥'}{abs(x):,.2f}")

        st.dataframe(
            display_df,
            width="stretch",
            hide_index=True,
        )

    with col_right:
        # 仓位饼图
        st.markdown("### 仓位分布")
        if not df.empty:
            pie = px.pie(
                df,
                values="仓位%",
                names="名称",
                hole=0.4,
                title="仓位占比",
            )
            pie.update_layout(height=300, margin=dict(t=30, b=0, l=0, r=0))
            st.plotly_chart(pie, width="stretch")

        # 行业分布（简化版）
        st.markdown("### 行业分布（估算）")
        sector_map = {
            "00": "主板/中小", "30": "创业板", "15": "ETF",
            "51": "ETF/主板", "58": "ETF/主板", "56": "ETF",
            "59": "ETF/科创", "68": "科创板", "002": "中小板",
            "300": "创业板", "600": "主板", "601": "主板",
        }
        df["行业"] = df["代码"].str[:2].map(sector_map).fillna("其他")
        sector_df = df.groupby("行业")["仓位%"].sum().reset_index()
        sector_df = sector_df.sort_values("仓位%", ascending=False)
        bar = px.bar(sector_df, x="行业", y="仓位%", title="行业暴露", color="仓位%")
        bar.update_layout(height=250, margin=dict(t=30, b=0, l=0, r=0))
        st.plotly_chart(bar, width="stretch")

    # 盈亏排行
    st.divider()
    st.markdown("### 🏆 盈亏排行榜")

    if "盈亏" in df.columns and "盈亏%" in df.columns:
        top_df = df.sort_values("盈亏", ascending=False).head(10)[
            ["名称", "代码", "盈亏", "盈亏%", "仓位%"]
        ]
        top_df["盈亏%"] = top_df["盈亏%"].map(lambda x: f"{x:+.1f}%")
        top_df["仓位%"] = top_df["仓位%"].map(lambda x: f"{x:.1f}%")
        top_df["盈亏"] = top_df["盈亏"].map(
            lambda x: f"{'+¥' if x >= 0 else '-¥'}{abs(x):,.0f}"
        )
        st.dataframe(top_df, width="stretch", hide_index=True)


def pnl_pnl_str(pnl: float) -> str:
    return f"{'+¥' if pnl >= 0 else '-¥'}{abs(pnl):,.0f}"


