"""分析决策视图 — 选股、异动分析"""
import streamlit as st
import pandas as pd

from ._shared import load_positions, get_db_connection


def get_active_view_name() -> str:
    return "analysis"


def render():
    """渲染分析决策页面 — 选股、异动分析"""
    st.header("📊 分析决策")

    # ── 子页面选择 ────────────────────────────────────────────────
    sub_tab = st.radio(
        "子页面",
        ["📈 策略回测", "🔍 多因子评分", "⚠️ 异动监控", "🔎 选股工具"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if sub_tab == "📈 策略回测":
        _render_strategy_backtest()
    elif sub_tab == "🔍 多因子评分":
        _render_factor_analysis()
    elif sub_tab == "⚠️ 异动监控":
        _render_anomaly_detection()
    elif sub_tab == "🔎 选股工具":
        _render_stock_selector()


def _render_strategy_backtest():
    """策略回测 — 调用现有策略对比面板"""
    from ._strategies import render_strategy_comparison
    render_strategy_comparison()


def _render_factor_analysis():
    """多因子评分 — 调用现有因子分析面板"""
    from ._factors import render_factor_analysis
    render_factor_analysis()


def _render_anomaly_detection():
    """异动监控 — 持仓股异常波动检测"""
    st.subheader("⚠️ 持仓股异动监控")

    positions = load_positions()
    if not positions:
        st.warning("暂无持仓数据")
        return

    stocks = [p for p in positions if p.get("类型") != "fund"]
    if not stocks:
        st.info("当前无个股持仓")
        return

    conn = get_db_connection()
    if conn is None:
        st.warning("无法连接数据库")
        return

    cur = conn.cursor()
    try:
        # 取持仓股近5日行情
        stock_codes = [f"{p['代码']}.XSHE" if not p['代码'].startswith(('5', '6'))
                       else f"{p['代码']}.XSHG"
                       for p in stocks]

        placeholders = ",".join(["%s"] * len(stock_codes))
        cur.execute(f"""
            SELECT ts_code, trade_date, close_price, change_pct, volume
            FROM market.daily_quotes
            WHERE ts_code IN ({placeholders})
              AND trade_date >= CURRENT_DATE - INTERVAL '5 days'
            ORDER BY ts_code, trade_date DESC
        """, stock_codes)
        rows = cur.fetchall()

        if not rows:
            st.info("暂无近期行情数据")
            return

        # 转换为DataFrame分析
        df = pd.DataFrame(rows, columns=["代码", "日期", "收盘价", "涨跌幅%", "成交量"])
        df["代码"] = df["代码"].str.replace(".XSHE", "").str.replace(".XSHG", "")

        # 计算各股近5日波动情况
        stock_stats = []
        for code in df["代码"].unique():
            code_df = df[df["代码"] == code].head(5)
            if len(code_df) < 2:
                continue
            changes = code_df["涨跌幅%"].tolist()
            max_up = max(changes) if changes else 0
            max_down = min(changes) if changes else 0
            volatility = (max(changes) - min(changes)) if changes else 0
            name = next((p["名称"] for p in stocks if p["代码"] == code), code)
            stock_stats.append({
                "代码": code,
                "名称": name,
                "最大涨幅%": round(max_up, 2),
                "最大跌幅%": round(max_down, 2),
                "波动范围%": round(volatility, 2),
                "近5日涨跌": "📈" if sum(changes) > 0 else "📉",
            })

        stats_df = pd.DataFrame(stock_stats)
        stats_df = stats_df.sort_values("波动范围%", ascending=False)

        # 标记异常波动
        def flag_anomaly(vol):
            if vol > 15:
                return "🚨 异常波动"
            elif vol > 10:
                return "⚠️ 高波动"
            return "✅ 正常"

        stats_df["状态"] = stats_df["波动范围%"].apply(flag_anomaly)

        st.dataframe(stats_df, use_container_width=True, hide_index=True)

        # 异常详情
        anomaly_stocks = stats_df[stats_df["波动范围%"] > 10]
        if not anomaly_stocks.empty:
            st.divider()
            st.markdown("### 🚨 异常波动详情")
            for _, row in anomaly_stocks.iterrows():
                with st.expander(f"{row['名称']} ({row['代码']}) - 波动 {row['波动范围%']}%"):
                    st.markdown(f"- 最大涨幅: **{row['最大涨幅%']}%**")
                    st.markdown(f"- 最大跌幅: **{row['最大跌幅%']}%**")
                    st.markdown(f"- 波动范围: **{row['波动范围%']}%**")

    except Exception as e:
        st.error(f"异动监控加载失败: {e}")
    finally:
        conn.close()


def _render_stock_selector():
    """选股工具 — 根据条件筛选标的"""
    st.subheader("🔎 智能选股")

    st.markdown("根据多维度条件筛选潜在标的")

    col1, col2, col3 = st.columns(3)
    with col1:
        price_range = st.slider("股价范围", 0.0, 200.0, (0.0, 100.0))
    with col2:
        market_cap_range = st.slider("市值范围(亿)", 0.0, 10000.0, (0.0, 5000.0))
    with col3:
        pe_range = st.slider("市盈率(PE)", -50.0, 200.0, (0.0, 50.0))

    col4, col5, col6 = st.columns(3)
    with col4:
        roe_threshold = st.slider("ROE阈值%", 0.0, 50.0, 5.0)
    with col5:
        growth_threshold = st.slider("净利润增长率%", -100.0, 500.0, 0.0)
    with col6:
        sector_filter = st.multiselect(
            "行业板块",
            ["银行", "证券", "保险", "医药", "科技", "消费", "工业", "能源", "房地产"],
            default=[],
        )

    if st.button("🔍 开始筛选", type="primary", use_container_width=True):
        with st.spinner("正在筛选标的..."):
            conn = get_db_connection()
            if conn is None:
                st.warning("无法连接数据库")
                return

            cur = conn.cursor()
            try:
                cur.execute("""
                    SELECT ts_code, name, close_price, market_cap, pe_ttm,
                           roe, revenue_growth, industry
                    FROM market.stock_basics
                    WHERE close_price BETWEEN %s AND %s
                      AND market_cap BETWEEN %s AND %s
                      AND (pe_ttm BETWEEN %s AND %s OR pe_ttm < 0)
                      AND roe >= %s
                      AND revenue_growth >= %s
                    ORDER BY roe DESC, market_cap DESC
                    LIMIT 50
                """, (*price_range, *market_cap_range, *pe_range,
                      roe_threshold, growth_threshold))
                rows = cur.fetchall()

                if not rows:
                    st.info("暂无符合条件标的")
                    return

                df = pd.DataFrame(
                    rows,
                    columns=["代码", "名称", "现价", "市值(亿)", "PE", "ROE%", "净利润增长%", "行业"]
                )

                if sector_filter:
                    df = df[df["行业"].isin(sector_filter)]

                st.success(f"筛选出 {len(df)} 只标的")
                st.dataframe(df, use_container_width=True, hide_index=True)

            except Exception as e:
                st.error(f"选股失败: {e}")
            finally:
                conn.close()