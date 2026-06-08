"""
Dashboard sub-module — Portfolio Dashboard
Generated from dashboard.py (411-623 lines)
Each function accesses streamlit via st (passed through from main module).
"""

from _shared import get_latest_quotes_from_db, load_positions
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date
from pathlib import Path as _Path
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).parent.parent))


# ── Excel 报告缓存 ────────────────────────────────────────────────────────────
# generate_excel_report 单次跑 60-90s（含因子评分 + openpyxl 写盘），
# 持仓变化时（用户调整 / 行情更新）才需重算。
# 缓存键绑定 positions 的标识（账号/代码/份额/成本），相同则复用。
@st.cache_data(ttl=300, show_spinner="正在生成 Excel 报告…")
def _cached_excel_report(positions_key: str, positions_json: str) -> bytes:
    """positions 序列化为 JSON 后作为缓存 key；返回 Excel 字节流"""
    import json
    from report_generator import generate_excel_report
    positions = json.loads(positions_json)
    path = generate_excel_report(positions)
    with open(path, "rb") as f:
        return f.read()


def _positions_cache_key(positions: list[dict]) -> str:
    """生成 positions 的稳定指纹（账号+代码+份额+成本+市值）"""
    sig = "|".join(
        f"{p.get('账号','main')}:{p.get('代码','')}:{p.get('份额',0)}:{p.get('成本',0)}:{p.get('市值',0)}"
        for p in positions
    )
    return sig[:200]  # 太长截断即可


# ── 视图 1：持仓仪表板 ──────────────────────────────────────────────────────

def render_portfolio_dashboard():
    # 账号选择器
    from account_manager import get_account_summary, get_active_accounts
    account_summaries = get_account_summary()
    active_accounts = get_active_accounts()
    
    col_acct_label, col_acct_select = st.columns([1, 3])
    with col_acct_label:
        st.markdown("**📂 账号**")
    with col_acct_select:
        if len(active_accounts) > 1:
            account_options = ["全部"] + [f"{cfg.get('name', aid)} ({aid})" for aid, cfg in active_accounts]
            account_keys = ["all"] + [aid for aid, _ in active_accounts]
            selected_label = st.selectbox("选择账号", account_options, label_visibility="collapsed")
            selected_account = account_keys[account_options.index(selected_label)]
        else:
            selected_account = "all"
            if account_summaries:
                st.caption(f"📂 {account_summaries[0]['name']}")

    positions = load_positions()
    if not positions:
        st.error("无持仓数据，请检查 positions.csv")
        return

    df = pd.DataFrame(positions)
    
    # 按账号筛选
    if selected_account != "all":
        df = df[df.get("账号", "main") == selected_account]
    
    if df.empty:
        st.warning(f"账号「{selected_account}」无持仓数据")
        return

    total_mv = df["市值"].sum()

    # ── 自动刷新 + 手动同步 ────────────────────────────────────────
    col_title, col_refresh, col_sync = st.columns([3, 1, 1])
    with col_title:
        st.markdown("## 📋 持仓仪表板")
    with col_refresh:
        auto_refresh = st.checkbox("🔄 自动刷新", value=False,
                                   help="每60秒自动刷新数据")
    with col_sync:
        # 每次 render 都重新读 CSV 拿 mtime（render 流程每次都跑这一段）
        # 这样能保证用户从 sidebar 切回本页时看到的是最新 mtime
        from account_manager import get_active_accounts as _gaa
        from pathlib import Path as _P
        from datetime import datetime as _dt
        csv_paths = []
        for _, cfg in _gaa():
            p = cfg.get("positions_csv")
            if p:
                csv_paths.append(str(p))
        mtimes = []
        for p in csv_paths:
            try:
                mtimes.append((_P(p).stat().st_mtime, p))
            except OSError:
                pass
        latest_mt = max((t for t, _ in mtimes), default=0.0)
        if mtimes:
            st.caption(f"📁 CSV: {_dt.fromtimestamp(latest_mt):%m-%d %H:%M}")

        # 同步按钮：st.toast + session_state 时间戳
        # 关键: 把"刚才同步时 CSV 状态"也存到 session_state，下次 render 时对照
        # 用 sum() 后 .item() 把 Series 转 Python float（Pyright 静态分析 Series.sum()
        # 返回 Series，实际 pandas 单 group 求和返回 scalar，此处强行 cast）
        total_mv_series = df["市值"].sum()  # type: ignore[reportAttributeAccessIssue]
        total_mv_now = float(total_mv_series.item() if hasattr(total_mv_series, "item") else total_mv_series)
        if st.button("🔄 同步持仓", use_container_width=True,
                     help="汇总 D:\\Hold 4 个券商/基金持仓 CSV → D:\\Hold\\invest-data\\positions.csv，"
                          "然后刷新仪表板。",
                     key="_sync_btn"):
            # 1) 调汇总脚本 (D:\Hold\*.csv → D:\Hold\invest-data\positions.csv)
            merge_msg = ""
            try:
                import subprocess as _sp
                from pathlib import Path as _Pa
                merge_script = _Pa(__file__).parent.parent / "merge_holdings.py"
                result = _sp.run(
                    [str(_Pa.home() / "invest_system" / ".venv" / "bin" / "python3.11"),
                     str(merge_script)],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    import json as _json
                    summary = _json.loads(result.stdout)
                    merge_msg = (f"汇总: {summary['total']} 只 (国金{summary['sources']['国金证券']['count']} "
                                 f"广发{summary['sources']['广发基金']['count']} "
                                 f"天天{summary['sources']['天天基金']['count']} "
                                 f"汇添富{summary['sources']['汇添富基金']['count']})")
                else:
                    merge_msg = f"汇总脚本失败: {result.stderr[:200]}"
            except Exception as e:
                merge_msg = f"汇总异常: {e}"

            # 2) 清缓存 + rerun
            st.cache_data.clear()
            st.session_state["_last_sync_at"] = _dt.now()
            st.session_state["_last_sync_csv_mtime"] = latest_mt
            st.session_state["_last_sync_positions_count"] = len(positions)
            st.session_state["_last_sync_total_mv"] = total_mv_now
            st.session_state["_last_sync_merge_msg"] = merge_msg
            st.toast(
                f"✅ 已同步 | {len(positions)} 只 | 总市值 ¥{total_mv_now:,.0f}\n{merge_msg}",
                icon="🔄",
            )
            st.rerun()

        if "_last_sync_at" in st.session_state:
            ts = st.session_state["_last_sync_at"]
            n = st.session_state.get("_last_sync_positions_count", "?")
            mv = st.session_state.get("_last_sync_total_mv", 0)
            merge_msg = st.session_state.get("_last_sync_merge_msg", "")
            st.caption(f"⏱ {ts:%H:%M:%S} | {n}只 | ¥{mv:,.0f}")
            if merge_msg:
                st.caption(merge_msg)

    # 顶部 KPI 卡片（6列）
    kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)

    total_cost = (df["份额"] * df["成本"]).sum()
    total_pnl = total_mv - total_cost
    pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    # 计算当日涨跌（基于最新行情）
    today_pnl = 0.0
    today_pnl_pct = 0.0
    try:
        quotes = get_latest_quotes_from_db()
        if quotes:
            quote_map = {q[0]: q for q in quotes}
            for _, row in df.iterrows():
                code = row["代码"]
                if code in quote_map:
                    _, _, change_pct, _ = quote_map[code]
                    today_pnl += row["市值"] * (change_pct / 100) if change_pct else 0
            if total_mv > 0:
                today_pnl_pct = (today_pnl / (total_mv - today_pnl)) * 100
    except Exception:
        pass

    with kpi1:
        st.metric("💰 总市值", f"¥{total_mv:,.0f}",
                  delta=f"{pnl_pct:+.1f}%" if pnl_pct != 0 else None)
    with kpi2:
        st.metric("📈 累计盈亏", f"{pnl_pnl_str(total_pnl)}")
    with kpi3:
        st.metric("📊 今日涨跌", f"{today_pnl:+,.0f}",
                  delta=f"{today_pnl_pct:+.2f}%" if today_pnl_pct != 0 else None)
    with kpi4:
        stock_count = len(df[df["类型"] == "stock"])
        st.metric("🏦 股票", stock_count)
    with kpi5:
        fund_count = len(df[df["类型"].isin(["fund", "etf"])])
        st.metric("📊 基金/ETF", fund_count)
    with kpi6:
        # 亏损标的数
        loss_count = len(df[df["市值"] < df["份额"] * df["成本"]])
        st.metric("🔴 浮亏标的", loss_count,
                  delta=f"-{loss_count}" if loss_count > 0 else "0")

    st.divider()

    # ── 筛选器 ──────────────────────────────────────────
    filter_col1, filter_col2, filter_col3 = st.columns([2, 2, 1])
    with filter_col1:
        type_filter = st.multiselect(
            "🏷 类型筛选",
            options=["stock", "fund", "etf"],
            default=["stock", "fund", "etf"],
            format_func=lambda x: {"stock": "🏦 股票", "fund": "📊 基金", "etf": "📈 ETF"}[x]
        )
    with filter_col2:
        sort_by = st.selectbox(
            "🔽 排序方式",
            options=["仓位%", "市值", "盈亏", "盈亏%", "名称"],
            index=0
        )
    with filter_col3:
        sort_order = st.radio("排序", ["↓ 降序", "↑ 升序"], horizontal=True,
                              label_visibility="collapsed")

    # 应用筛选
    df_filtered = df[df["类型"].isin(type_filter)] if type_filter else df

    # 应用排序
    ascending = sort_order == "↑ 升序"
    if sort_by == "仓位%":
        df_filtered = df_filtered.sort_values("仓位%", ascending=ascending)
    elif sort_by == "市值":
        df_filtered = df_filtered.sort_values("市值", ascending=ascending)
    elif sort_by == "盈亏":
        df_filtered["盈亏"] = df_filtered["市值"] - df_filtered["份额"] * df_filtered["成本"]
        df_filtered = df_filtered.sort_values("盈亏", ascending=ascending)
    elif sort_by == "盈亏%":
        df_filtered["盈亏%"] = ((df_filtered["市值"] / (df_filtered["份额"] * df_filtered["成本"])) - 1) * 100  # noqa: E501
        df_filtered = df_filtered.sort_values("盈亏%", ascending=ascending)
    else:
        df_filtered = df_filtered.sort_values("名称", ascending=ascending)

    # ── 导出按钮 ─────────────────────────────────────────
    export_col1, export_col2, export_col3 = st.columns([1, 1, 1])
    with export_col1:
        csv_data = df_filtered[["代码", "名称", "成本", "市值", "仓位%", "份额"]].copy()
        csv_data["盈亏"] = csv_data["市值"] - csv_data["份额"] * csv_data["成本"]
        csv_data["盈亏%"] = ((csv_data["市值"] / (csv_data["份额"] * csv_data["成本"])) - 1) * 100
        csv_data["盈亏%"] = csv_data["盈亏%"].round(2)
        st.download_button(
            "📥 导出 CSV",
            data=csv_data.to_csv(index=False).encode("utf-8-sig"),
            file_name="investpilot_portfolio.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with export_col2:
        # 懒生成：用户点按钮才生成 Excel（首次 60-90s，之后走 streamlit cache）
        if st.button("📊 准备 Excel 报告", key="prep_excel", use_container_width=True):
            try:
                import json
                positions_key = _positions_cache_key(positions)
                positions_json = json.dumps(positions, ensure_ascii=False, default=str)
                with st.spinner("正在生成 Excel 报告（首次 60-90 秒）…"):
                    st.session_state["excel_bytes"] = _cached_excel_report(positions_key, positions_json)
                st.session_state["excel_filename"] = f"portfolio_{date.today().strftime('%Y%m%d')}.xlsx"
                st.success("Excel 已就绪，点击下方按钮下载")
            except Exception as e:
                st.error(f"生成失败: {e}")

        if st.session_state.get("excel_bytes"):
            st.download_button(
                "📥 下载 Excel",
                data=st.session_state["excel_bytes"],
                file_name=st.session_state.get("excel_filename", "portfolio.xlsx"),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

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
        df["盈亏%"] = (((df["市值"] / (df["份额"] * df["成本"])) - 1) * 100).round(2).replace([float("inf"), float("-inf")], 0).fillna(0)  # noqa: E501

        # 类型映射
        type_icon = {"fund": "📊", "stock": "🏦", "etf": "📈"}
        df["类型图标"] = df["类型"].map(type_icon).fillna("📋")

        display_df = df[["代码", "名称", "类型图标", "成本", "市值", "仓位%", "盈亏"]].copy()
        display_df["成本"] = display_df["成本"].map(lambda x: f"¥{x:.4f}" if x < 100 else f"¥{x:.2f}")  # noqa: E501
        display_df["市值"] = display_df["市值"].map(lambda x: f"¥{x:,.2f}")
        display_df["仓位%"] = display_df["仓位%"].map(lambda x: f"{x:.2f}%")
        display_df["盈亏"] = display_df["盈亏"].map(lambda x: f"{'+¥' if x >= 0 else '-¥'}{abs(x):,.2f}")  # noqa: E501

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


# ── 视图 2：Equity Curve 历史曲线 ───────────────────────────────────────────────

def _render_equity_curve():
    """显示组合equity历史曲线（近90天）"""
    from equity_curve_tracker import get_equity_curve
    import streamlit as st

    st.markdown("### 📈 持仓历史 Equity Curve")

    data = get_equity_curve(days=90)
    if not data:
        st.info("暂无 equity curve 数据，将于今日 15:40 首次记录")
        return

    df = pd.DataFrame(data)
    df["calc_date"] = pd.to_datetime(df["calc_date"])
    df = df.set_index("calc_date")

    # 显示最新值和变化
    latest_value = df["total_value"].iloc[-1]
    first_value = df["total_value"].iloc[0]
    total_return = ((latest_value - first_value) / first_value * 100) if first_value > 0 else 0
    col1, col2 = st.columns(2)
    with col1:
        st.metric("当前组合市值", f"¥{latest_value:,.2f}")
    with col2:
        st.metric(
            "累计收益",
            f"{total_return:+.2f}%",
            delta=f"{latest_value - first_value:+,.2f}"
        )

    # 折线图
    st.line_chart(df["total_value"], width="stretch", height=300)


