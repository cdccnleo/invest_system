#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V24-C1-T5: 持仓风险 Streamlit Dashboard (方案 9)
===============================================

实现 v24_implementation_plan.md 中的 **任务 V24-C1-T5 (UI)**：

> 4 区域: Portfolio 总览 + 单标风险表格 + 触发器状态 + 中报季倒计时

**集成路径**:
```
Streamlit Dashboard (port 8501)
  → render_risk_dashboard(user_id)  # 本模块
  → 4 区域渲染
  → 复用 position_risk_manager.analyze_portfolio()
  → 复用 position_risk_triggers.run_triggers()
```

**用法** (在 Streamlit 页面):
```python
from position_risk_dashboard import render_risk_dashboard
render_risk_dashboard("aileo")
```

Author: Hermes Agent × aileo
Date: 2026-06-13
Version: V24-C1-T5
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
for _p in [str(_SCRIPT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 真实依赖
try:
    import streamlit as st
    _HAS_STREAMLIT = True
except ImportError:
    _HAS_STREAMLIT = False

try:
    import pandas as pd
    _HAS_PD = True
except ImportError:
    _HAS_PD = False

try:
    from position_risk_manager import (
        analyze_portfolio, fetch_current_positions, analyze_position,
        get_pg_connection, ensure_pg_tables,
        MAX_SINGLE_WEIGHT_PCT, MAX_DAILY_LOSS_PCT, MIN_CASH_RATIO_PCT,
    )
    _HAS_MANAGER = True
except ImportError:
    _HAS_MANAGER = False

LOG = logging.getLogger("position_risk_dashboard")
if not LOG.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


# ====================================================================
# 1. 4 区域渲染
# ====================================================================

def render_risk_dashboard(user_id: str = "aileo") -> None:
    """Streamlit 持仓风险面板主入口"""
    if not _HAS_STREAMLIT:
        LOG.warning("[render] streamlit not available")
        return
    if not _HAS_MANAGER:
        st.error("⚠️ position_risk_manager 不可用, 请检查 sys.path")
        return

    ensure_pg_tables()

    st.markdown("## 🛡️ 持仓风险预算 (V24-C1 新增)")
    st.caption("方案 9: 5 大风险指标 + 3 类触发器 + 中报季倒计时")

    # 拿数据
    positions = fetch_current_positions()
    portfolio = analyze_portfolio(positions)
    position_risks = [analyze_position(p, positions, portfolio.total_market_value) for p in positions]

    # 区域 1: Portfolio 总览
    _render_portfolio_overview(portfolio)

    # 区域 2: 单标风险表格
    _render_position_table(position_risks)

    # 区域 3: 触发器状态
    _render_triggers_status(portfolio, position_risks)

    # 区域 4: 中报季倒计时
    _render_earnings_countdown()


def _render_portfolio_overview(portfolio) -> None:
    """区域 1: Portfolio 总览"""
    st.markdown("### 📊 Portfolio 总览")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("总市值", f"¥{portfolio.total_market_value:,.0f}")
    with c2:
        var_pct = (portfolio.total_var_1d / max(portfolio.total_market_value, 1)) * 100
        st.metric("1d VaR (95%)", f"¥{portfolio.total_var_1d:,.0f}",
                  delta=f"{var_pct:.2f}%", delta_color="inverse")
    with c3:
        st.metric("最大单日亏损 (2%)", f"¥{portfolio.max_daily_loss:,.0f}")
    with c4:
        st.metric("持仓数", portfolio.position_count)

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        st.metric("严重持仓 (P0)", portfolio.critical_risk_count)
    with c6:
        st.metric("高风险 (P1)", portfolio.high_risk_count - portfolio.critical_risk_count)
    with c7:
        st.metric("最大权重", f"{portfolio.max_single_weight_pct:.2f}%",
                  delta=f"{portfolio.max_position_code}")
    with c8:
        st.metric("触发规则", portfolio.total_triggers)


def _render_position_table(position_risks) -> None:
    """区域 2: 单标风险表格"""
    st.markdown("### 📋 单标风险表格 (按权重排序)")
    if not position_risks:
        st.info("无持仓数据")
        return

    if not _HAS_PD:
        st.warning("pandas 不可用, 表格降级")
        return

    # 转 DataFrame
    rows = []
    for pr in sorted(position_risks, key=lambda x: -x.weight_pct):
        risk_emoji = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢",
        }.get(pr.risk_level, "⚪")
        rows.append({
            "风险": risk_emoji,
            "代码": pr.code,
            "名称": pr.name,
            "类型": pr.position_type,
            "权重%": f"{pr.weight_pct:.2f}",
            "市值¥": f"{pr.market_value:,.0f}",
            "盈亏%": f"{pr.profit_pct:.2f}",
            "VaR¥": f"{pr.var_1d:,.0f}",
            "止损价": f"{pr.stop_loss_price:.2f}" if pr.stop_loss_price else "-",
            "触发数": len(pr.triggers),
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True, height=400)

    # 展开看触发详情
    triggered = [pr for pr in position_risks if pr.triggers]
    if triggered:
        with st.expander(f"📍 触发详情 ({len(triggered)} 个持仓)", expanded=False):
            for pr in sorted(triggered, key=lambda x: -len(x.triggers))[:15]:
                st.markdown(f"**{pr.code} {pr.name}** ({pr.risk_level})")
                for t in pr.triggers:
                    st.markdown(f"  - {t}")


def _render_triggers_status(portfolio, position_risks) -> None:
    """区域 3: 触发器状态"""
    st.markdown("### 🚨 触发器状态")
    if not _HAS_MANAGER:
        st.warning("manager 不可用")
        return

    # 当前触发
    triggered_risks = [pr for pr in position_risks if pr.triggers]
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("当前触发持仓", len(triggered_risks))
    with c2:
        st.metric("P0 止损", sum(1 for pr in triggered_risks if any("止损" in t for t in pr.triggers)))
    with c3:
        st.metric("P1 止盈/集中度", sum(1 for pr in triggered_risks if any("止盈" in t or "集中" in t for t in pr.triggers)))

    # 历史告警 (PG)
    if st.button("🔄 立即跑触发器"):
        try:
            from position_risk_triggers import run_triggers
            with st.spinner("触发器运行中..."):
                result = run_triggers()
            st.success(f"✅ 触发器完成: 生成 {result.get('generated', 0)}, "
                       f"去重后 {result.get('after_dedup', 0)}, "
                       f"WS {result.get('ws', 0)}, PG {result.get('pg', 0)}")
        except Exception as e:
            st.error(f"触发器失败: {e}")

    # 历史告警 (从 PG 读)
    st.markdown("#### 历史告警 (最近 24h)")
    conn = get_pg_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT code, alert_type, severity, message, created_at, delivered
                FROM l3.risk_alert_log
                WHERE created_at > NOW() - INTERVAL '24 hours'
                ORDER BY created_at DESC
                LIMIT 20
            """)
            rows = cur.fetchall()
            conn.close()
            if rows:
                df_hist = pd.DataFrame(rows, columns=[
                    "code", "alert_type", "severity", "message", "created_at", "delivered"
                ])
                st.dataframe(df_hist, use_container_width=True, hide_index=True)
            else:
                st.info("最近 24h 无告警")
        except Exception as e:
            st.warning(f"读历史告警失败: {e}")


def _render_earnings_countdown() -> None:
    """区域 4: 中报季倒计时"""
    st.markdown("### 📅 关键日期")
    earnings_date = date(2026, 7, 15)
    today = date.today()
    days_left = (earnings_date - today).days

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("中报季 (7/15)", f"还有 {days_left} 天")
    with c2:
        st.metric("今日", today.isoformat())
    with c3:
        if days_left <= 14:
            st.error("⚠️ 距中报季 ≤ 14 天, 建议减仓高弹性持仓")
        elif days_left <= 30:
            st.warning("⚡ 距中报季 ≤ 30 天, 关注持仓业绩预判")
        else:
            st.success("✅ 时间充裕, 持续监控")

    st.caption("中报季策略: 业绩 miss > 20% 自动减仓 50% (per memory)")


# ====================================================================
# 2. CLI / 自测
# ====================================================================

def _selftest():
    """自测 (非 streamlit 环境)"""
    LOG.info("=== V24-C1-T5 Dashboard 自测 (非 streamlit) ===")
    if not _HAS_MANAGER:
        LOG.error("manager 不可用")
        return False
    ensure_pg_tables()
    positions = fetch_current_positions()
    portfolio = analyze_portfolio(positions)
    position_risks = [analyze_position(p, positions, portfolio.total_market_value) for p in positions]
    LOG.info(f"总市值: ¥{portfolio.total_market_value:,.0f}")
    LOG.info(f"持仓: {portfolio.position_count}, 严重: {portfolio.critical_risk_count}, "
             f"高: {portfolio.high_risk_count - portfolio.critical_risk_count}, "
             f"触发: {portfolio.total_triggers}")
    LOG.info(f"1d VaR: ¥{portfolio.total_var_1d:,.0f}")
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="持仓风险 Streamlit Dashboard")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return _selftest()
    if _HAS_STREAMLIT:
        render_risk_dashboard("aileo")
    else:
        print("❌ streamlit 不可用, 请在 streamlit runtime 中运行")


if __name__ == "__main__":
    main()
