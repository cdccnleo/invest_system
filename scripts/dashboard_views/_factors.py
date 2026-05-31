"""
dashboard_views._factors — 多因子评分面板

集成 factor_engine.py，提供持仓标的的多因子评分对比视图。
"""
import streamlit as st
import pandas as pd
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def render_factor_analysis():
    """渲染多因子评分对比面板"""
    st.header("📊 多因子评分分析")
    st.markdown("基于 6 因子模型对持仓个股进行综合评分排名，因子权重可自定义调整。")

    from _shared import load_positions
    positions = load_positions()

    if not positions:
        st.warning("暂无持仓数据")
        return

    stocks = [p for p in positions if p.get("type") != "fund"]
    if not stocks:
        st.info("当前无个股持仓")
        return

    # 权重配置
    with st.expander("⚖️ 因子权重配置", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            w_value = st.slider("价值因子 (PE/PB)", 0.0, 1.0, 0.20, 0.05, key="w_value")
            w_quality = st.slider("质量因子 (ROE/增长率)", 0.0, 1.0, 0.25, 0.05, key="w_quality")
        with col2:
            w_momentum = st.slider("动量因子 (涨跌幅)", 0.0, 1.0, 0.20, 0.05, key="w_momentum")
            w_volatility = st.slider("波动率因子", 0.0, 1.0, 0.10, 0.05, key="w_volatility")
        with col3:
            w_technical = st.slider("技术因子 (RSI/MA)", 0.0, 1.0, 0.15, 0.05, key="w_technical")
            w_size = st.slider("规模因子 (市值)", 0.0, 1.0, 0.10, 0.05, key="w_size")

        total_w = w_value + w_quality + w_momentum + w_volatility + w_technical + w_size
        st.caption(f"权重合计: {total_w:.0%} {'✅' if 0.99 <= total_w <= 1.01 else '⚠️ 将自动归一化'}")

    if st.button("🔍 运行因子评分", type="primary", use_container_width=True):
        with st.spinner("正在计算多因子评分..."):
            from factor_engine import FactorEngine

            weights = {
                "value": w_value, "quality": w_quality,
                "momentum": w_momentum, "volatility": w_volatility,
                "technical": w_technical, "size": w_size,
            }
            engine = FactorEngine(weights=weights)

            stock_codes = []
            price_map = {}
            name_map = {}
            for p in stocks:
                code = p.get("code", "").zfill(6)
                if code.startswith("15") or code.startswith("30") or code.startswith("00"):
                    ts_code = f"{code}.XSHE"
                elif code.startswith("5") or code.startswith("6"):
                    ts_code = f"{code}.XSHG"
                elif code.startswith("4") or code.startswith("8"):
                    ts_code = f"{code}.BJ"
                else:
                    ts_code = f"{code}.XSHE"
                stock_codes.append(ts_code)
                price_map[ts_code] = p.get("close", p.get("cost", 0))
                name_map[ts_code] = p.get("name", code)

            results = engine.score_batch(stock_codes, price_map)

        # 构建展示表格
        rows = []
        for r in results:
            code_short = r["ts_code"].split(".")[0]
            name = name_map.get(r["ts_code"], code_short)
            raw = r.get("raw_scores", {})
            rows.append({
                "排名": r["rank"],
                "代码": code_short,
                "名称": name,
                "综合得分": r["total_score"],
                "Z-Score": r["z_score"],
                "价值": raw.get("value", 0),
                "质量": raw.get("quality", 0),
                "动量": raw.get("momentum", 0),
                "波动率": raw.get("volatility", 0),
                "技术": raw.get("technical", 0),
                "规模": raw.get("size", 0),
            })

        df = pd.DataFrame(rows)

        # 颜色映射
        def color_score(val):
            if val > 0:
                return "color: #00aa00"
            elif val < 0:
                return "color: #cc0000"
            return ""

        st.subheader("评分排名")
        styled = df.style.map(color_score, subset=["综合得分", "价值", "质量", "动量", "波动率", "技术", "规模"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # 因子贡献图
        st.subheader("因子贡献分布")
        chart_data = pd.DataFrame([
            {"股票": f"{r['代码']} {r['名称']}", "价值": r["价值"], "质量": r["质量"],
             "动量": r["动量"], "波动率": r["波动率"], "技术": r["技术"], "规模": r["规模"]}
            for r in rows
        ])
        import plotly.express as px
        fig = px.bar(
            chart_data.melt(id_vars=["股票"], var_name="因子", value_name="得分"),
            x="股票", y="得分", color="因子",
            title="各因子得分分布",
            barmode="group",
            color_discrete_sequence=px.colors.qualitative.Set2,
        )
        st.plotly_chart(fig, use_container_width=True)

        # 下载
        csv_data = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 导出评分 CSV",
            data=csv_data,
            file_name="investpilot_factor_scores.csv",
            mime="text/csv",
            use_container_width=True,
        )