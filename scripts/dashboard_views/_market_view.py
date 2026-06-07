"""行情概览视图 — 持仓标的涨跌幅、热点板块、接近涨跌停"""
import streamlit as st
import pandas as pd


def get_active_view_name() -> str:
    return "market"


def render():
    """渲染行情概览页面 — 行情数据可视化

    子页面（与 sidebar 持仓仪表板不重复）：
      🔥 持仓涨幅 — 持仓标的当日/近 N 日涨跌幅
      🚀 接近涨停 — 持仓中 change_pct >= 8% 的标的
      💥 接近跌停 — 持仓中 change_pct <= -8% 的标的
    """
    st.header("📈 行情概览")
    st.caption("聚焦持仓标的的行情数据 · 完整持仓分析见侧边栏'📋 持仓仪表板'")

    # ── 子页面选择 ────────────────────────────────────────────────
    sub_tab = st.radio(
        "子页面",
        ["🔥 持仓涨幅", "🚀 接近涨停", "💥 接近跌停"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if sub_tab == "🔥 持仓涨幅":
        _render_holdings_changes()
    elif sub_tab == "🚀 接近涨停":
        _render_near_limit(threshold=8.0, label="接近涨停 (≥8%)")
    else:
        _render_near_limit(threshold=-8.0, label="接近跌停 (≤-8%)")


def _strip_ts_suffix(code: str) -> str:
    """ts_code '600183.XSHG' / '562500.OF' → 6 位代码 '600183' / '562500'"""
    if not isinstance(code, str):
        return str(code) if code is not None else ""
    return code.split(".")[0] if "." in code else code


def _build_name_map() -> dict:
    """{6位代码: 名称}"""
    try:
        from _shared import load_positions
        return {str(p["代码"]): p["名称"] for p in load_positions()}
    except Exception:
        return {}


def _attach_names(df: pd.DataFrame) -> pd.DataFrame:
    """按 6 位代码（去掉 ts_code 后缀）匹配持仓名称，未匹配保留原代码"""
    if df.empty or "代码" not in df.columns:
        return df
    name_map = _build_name_map()
    keys = df["代码"].astype(str).map(lambda c: _strip_ts_suffix(c))
    names = keys.map(lambda k: name_map.get(k, k))
    df.insert(0, "名称", names)
    return df


def _get_quotes_df() -> pd.DataFrame:
    """从 PostgreSQL 读取最新一日的持仓行情"""
    try:
        from storage_factory import get_storage
        storage = get_storage()
        if not storage._ensure_pg() or storage._pg_conn is None:
            return pd.DataFrame()
        conn = storage._pg_conn
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT ts_code, trade_date, close_price, change_pct,
                       high_price, low_price, volume
                FROM market.daily_quotes
                WHERE trade_date = (SELECT MAX(trade_date) FROM market.daily_quotes)
                ORDER BY change_pct DESC
            """)
            rows = cur.fetchall()
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(
                rows,
                columns=["代码", "日期", "现价", "涨幅%", "最高", "最低", "成交量"],
            )
            # psycopg2 把 NUMERIC 返回为 Decimal，全部转 float 避免后续算术 TypeError
            for col in ["现价", "涨幅%", "最高", "最低"]:
                df[col] = df[col].astype(float)
            df["成交量"] = df["成交量"].astype("int64")
            return df
        finally:
            cur.close()
            storage.close()
    except Exception as e:
        st.error(f"行情加载失败: {e}")
        return pd.DataFrame()


def _render_holdings_changes():
    """持仓涨幅榜 — 持仓标的按当日涨跌幅排序"""
    st.subheader("🔥 持仓涨幅榜")

    df = _get_quotes_df()
    if df.empty:
        st.info("暂无行情数据")
        return

    # 关联持仓名称（去掉 ts_code 后缀做匹配）
    _attach_names(df)

    # 涨幅颜色标记
    def color_arrow(val):
        if pd.isna(val):
            return "⚪"
        if val >= 9:
            return "🚀"
        if val >= 5:
            return "🔥"
        if val >= 1:
            return "🟢"
        if val <= -9:
            return "💀"
        if val <= -5:
            return "🔴🔴"
        if val <= -1:
            return "🔴"
        return "⚪"

    df["状态"] = df["涨幅%"].apply(color_arrow)

    # KPI 卡片
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("持仓标的", f"{len(df)} 只")
    with col2:
        up = (df["涨幅%"] > 0).sum()
        st.metric("上涨家数", f"{up} 只", delta=f"+{up}/{len(df)}")
    with col3:
        down = (df["涨幅%"] < 0).sum()
        st.metric("下跌家数", f"{down} 只", delta=f"-{down}/{len(df)}")
    with col4:
        avg = df["涨幅%"].mean()
        st.metric("平均涨幅", f"{avg:+.2f}%")

    st.divider()

    # 数据源 ulist.np/get 不返回 high/low/volume → 用 0 占位
    # 表格里改用 "—" 表示无数据，避免误导
    df_display = df[["状态", "名称", "代码", "现价", "涨幅%", "最高", "最低"]].copy()
    for col in ["最高", "最低"]:
        df_display[col] = df_display[col].apply(
            lambda v: f"{v:.2f}" if v and v > 0 else "—"
        )
    st.caption("💡 最高/最低暂为 0 是因行情采集源（东财 ulist.np/get）未返回该字段，待切换数据源后可显示。")
    st.dataframe(
        df_display,
        width="stretch",
        hide_index=True,
    )


def _render_near_limit(threshold: float, label: str):
    """接近涨跌停 — 持仓中涨跌幅接近阈值的标的"""
    direction = "涨" if threshold > 0 else "跌"
    st.subheader(f"{'🚀' if threshold > 0 else '💥'} 持仓{label}")

    df = _get_quotes_df()
    if df.empty:
        st.info("暂无行情数据")
        return

    if threshold > 0:
        sub = df[df["涨幅%"] >= threshold].sort_values("涨幅%", ascending=False)
    else:
        sub = df[df["涨幅%"] <= threshold].sort_values("涨幅%", ascending=True)

    if sub.empty:
        st.info(f"持仓中暂无{label}的标的")
        return

    # 关联持仓名称（去掉 ts_code 后缀做匹配）
    _attach_names(sub)

    st.dataframe(
        sub[["名称", "代码", "现价", "涨幅%", "成交量"]],
        width="stretch",
        hide_index=True,
    )

    # 距离阈值的距离（涨幅% 来自 psycopg2 Numeric/Decimal，需先转 float）
    sub["涨幅%"] = sub["涨幅%"].astype(float)
    if threshold > 0:
        sub["距涨停"] = (10.0 - sub["涨幅%"]).round(2)
        st.caption(f"💡 距 10% 涨停平均还需 +{sub['距涨停'].mean():.2f}%")
    else:
        sub["距跌停"] = (sub["涨幅%"] - (-10.0)).round(2)
        st.caption(f"💡 距 -10% 跌停平均还需 {sub['距跌停'].mean():.2f}%")
