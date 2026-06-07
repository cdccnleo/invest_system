"""
dashboard_views._strategies — 策略对比面板

集成 strategy_engine.py，提供多策略回测对比视图。
"""

import streamlit as st
import pandas as pd
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from strategy_engine import (
    MACrossoverStrategy,
    MomentumStrategy,
    MeanReversionStrategy,
    GridSearchOptimizer,
    StrategyRunner,
)


def render_strategy_comparison():
    """渲染策略对比面板"""
    st.header("策略回测对比")

    st.markdown("""
    对持仓标的运行多策略回测，对比不同策略的历史表现。
    策略引擎基于 [strategy_engine.py](file:///scripts/strategy_engine.py)。
    """)

    col1, col2, col3 = st.columns(3)
    with col1:
        ts_code = st.text_input("股票代码", value="000977", max_chars=6,
                                help="输入 6 位股票代码")
    with col2:
        enable_ma = st.checkbox("均线交叉策略", value=True)
        enable_momentum = st.checkbox("动量策略", value=True)
        enable_reversion = st.checkbox("均值回归策略", value=True)
    with col3:
        run_btn = st.button("运行回测", type="primary", use_container_width=True)

    if run_btn:
        with st.spinner("正在加载行情数据并运行策略回测..."):
            try:
                from audit_analytics import get_db_conn
                conn = get_db_conn()
                cur = conn.cursor()
                cur.execute("""
                    SELECT trade_date, close_price, high_price, low_price, volume
                    FROM market.daily_quotes
                    WHERE ts_code = %s
                    ORDER BY trade_date ASC
                """, (f"{ts_code}.XSHE",))
                rows = cur.fetchall()
                conn.close()

                if len(rows) < 30:
                    st.warning(f"数据不足：仅 {len(rows)} 个交易日（需要 ≥30）")
                    return

                data = pd.DataFrame(rows, columns=["trade_date", "close", "high", "low", "volume"])
                data.set_index("trade_date", inplace=True)

                strategies = []
                if enable_ma:
                    strategies.append(MACrossoverStrategy())
                if enable_momentum:
                    strategies.append(MomentumStrategy())
                if enable_reversion:
                    strategies.append(MeanReversionStrategy())

                if not strategies:
                    st.warning("请至少选择一种策略")
                    return

                results = StrategyRunner.compare(strategies, data)

                st.subheader("策略绩效对比")
                st.dataframe(
                    results[["strategy", "total_return_pct", "sharpe_ratio",
                             "volatility_pct", "max_drawdown"]],
                    use_container_width=True,
                    column_config={
                        "total_return_pct": st.column_config.NumberColumn("总收益%", format="%.2f%%"),  # noqa: E501
                        "sharpe_ratio": st.column_config.NumberColumn("夏普比率", format="%.2f"),
                        "volatility_pct": st.column_config.NumberColumn("年化波动%", format="%.2f%%"),  # noqa: E501
                    }
                )

                st.caption(f"回测数据范围: {data.index[0]} ~ {data.index[-1]}，共 {len(data)} 个交易日")  # noqa: E501

            except Exception as e:
                st.error(f"回测失败: {e}")

    st.divider()

    st.subheader("参数优化")
    st.markdown("网格搜索最优策略参数组合")

    optimize_btn = st.button("运行参数优化", type="secondary")
    if optimize_btn:
        with st.spinner("正在优化参数..."):
            try:
                from audit_analytics import get_db_conn
                conn = get_db_conn()
                cur = conn.cursor()
                cur.execute("""
                    SELECT trade_date, close_price, high_price, low_price, volume
                    FROM market.daily_quotes
                    WHERE ts_code = %s
                    ORDER BY trade_date ASC
                """, (f"{ts_code}.XSHE",))
                rows = cur.fetchall()
                conn.close()

                if len(rows) < 30:
                    st.warning("数据不足")
                    return

                data = pd.DataFrame(rows, columns=["trade_date", "close", "high", "low", "volume"])
                data.set_index("trade_date", inplace=True)

                strategy = MACrossoverStrategy()
                grid = strategy.get_param_grid()
                result = GridSearchOptimizer.optimize(
                    MACrossoverStrategy, grid, data, metric="sharpe_ratio"
                )

                st.success(f"最优参数: {result['best_params']}（夏普比率: {result['best_score']:.2f}）")  # noqa: E501

                if result["all_results"]:
                    param_df = pd.DataFrame(result["all_results"])
                    param_df["params_str"] = param_df["params"].apply(
                        lambda p: f"short={p['short_window']}, long={p['long_window']}"
                    )
                    st.dataframe(
                        param_df[["params_str", "score", "total_return_pct"]],
                        use_container_width=True,
                        column_config={
                            "params_str": "参数组合",
                            "score": st.column_config.NumberColumn("夏普比率", format="%.2f"),
                            "total_return_pct": st.column_config.NumberColumn("总收益%", format="%.2f%%"),  # noqa: E501
                        }
                    )
            except Exception as e:
                st.error(f"优化失败: {e}")