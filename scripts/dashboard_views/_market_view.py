"""行情概览视图 — 涨跌停、热点板块"""
import streamlit as st
import pandas as pd


def get_active_view_name() -> str:
    return "market"


def render():
    """渲染行情概览页面 — 涨跌停、热点板块"""
    st.header("📈 行情概览")

    # ── 子页面选择 ────────────────────────────────────────────────
    sub_tab = st.radio(
        "子页面",
        ["📋 持仓总览", "🔄 涨跌停", "🔥 热点板块"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if sub_tab == "📋 持仓总览":
        _render_portfolio_overview()
    elif sub_tab == "🔄 涨跌停":
        _render_limit_up_down()
    elif sub_tab == "🔥 热点板块":
        _render_hot_sectors()


def _render_portfolio_overview():
    """持仓总览 — 调用现有持仓仪表板"""
    from ._portfolio import render_portfolio_dashboard
    render_portfolio_dashboard()


def _render_limit_up_down():
    """涨跌停榜 — 全市场涨跌停股票"""
    st.subheader("🔄 涨跌停榜")

    try:
        from storage_factory import get_storage
        storage = get_storage()
        conn = storage._pg_conn
        if conn is None:
            st.warning("无法连接数据库")
            return
        cur = conn.cursor()
        try:
            # 涨停榜
            st.markdown("### 🟢 涨停股票")
            cur.execute("""
                SELECT ts_code, name, close_price, change_pct, amplitude_pct, reason
                FROM market.limit_up_quotes
                WHERE trade_date = CURRENT_DATE
                ORDER BY change_pct DESC, amplitude_pct DESC
                LIMIT 20
            """)
            up_rows = cur.fetchall()

            if up_rows:
                df_up = pd.DataFrame(
                    up_rows,
                    columns=["代码", "名称", "现价", "涨幅%", "振幅%", "涨停原因"]
                )
                st.dataframe(df_up, use_container_width=True, hide_index=True)
            else:
                st.info("今日暂无涨停数据")

            st.divider()

            # 跌停榜
            st.markdown("### 🔴 跌停股票")
            cur.execute("""
                SELECT ts_code, name, close_price, change_pct, amplitude_pct, reason
                FROM market.limit_down_quotes
                WHERE trade_date = CURRENT_DATE
                ORDER BY change_pct ASC, amplitude_pct DESC
                LIMIT 20
            """)
            down_rows = cur.fetchall()

            if down_rows:
                df_down = pd.DataFrame(
                    down_rows,
                    columns=["代码", "名称", "现价", "跌幅%", "振幅%", "跌停原因"]
                )
                st.dataframe(df_down, use_container_width=True, hide_index=True)
            else:
                st.info("今日暂无跌停数据")

        except Exception as e:
            st.error(f"加载涨跌停数据失败: {e}")
        finally:
            cur.close()
            storage.close()
    except Exception as e:
        st.error(f"数据库连接失败: {e}")


def _render_hot_sectors():
    """热点板块 — 行业/概念涨跌排名"""
    st.subheader("🔥 热点板块")

    try:
        from storage_factory import get_storage
        storage = get_storage()
        conn = storage._pg_conn
        if conn is None:
            st.warning("无法连接数据库")
            return
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT industry_code, industry_name, avg_change_pct, stock_count,
                       lead_stocks, change_rank
                FROM market.industry_heatmap
                WHERE trade_date = CURRENT_DATE
                ORDER BY avg_change_pct DESC
                LIMIT 30
            """)
            rows = cur.fetchall()

            if not rows:
                st.info("暂无板块行情数据")
                return

            df = pd.DataFrame(
                rows,
                columns=["板块代码", "板块名称", "平均涨幅%", "成分股数", "领涨股", "排名"]
            )

            def color_change(val):
                if val > 3:
                    return "🟢🟢"
                elif val > 1:
                    return "🟢"
                elif val < -3:
                    return "🔴🔴"
                elif val < -1:
                    return "🔴"
                return "⚪"

            df["涨跌"] = df["平均涨幅%"].apply(color_change)

            st.dataframe(
                df[["排名", "板块名称", "涨跌", "平均涨幅%", "成分股数", "领涨股"]],
                use_container_width=True,
                hide_index=True,
            )

        except Exception as e:
            st.error(f"加载热点板块失败: {e}")
        finally:
            cur.close()
            storage.close()
    except Exception as e:
        st.error(f"数据库连接失败: {e}")