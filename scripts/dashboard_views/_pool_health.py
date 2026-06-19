"""
Dashboard sub-module — PG 连接池健康监控页 (PIT #148)

显示 audit.pool_health_metrics 趋势 + 池满持续时长 (PIT #148 实战落地):
- 当前快照 (latest health_check)
- 24h in_use/maxconn 比例曲线
- 24h saturated_duration_s 持续时长曲线 + 600s 阈值线
- 关键告警事件列表 (CRITICAL/WARNING)

PIT #148 实战集成:
- saturated_duration_s 字段 (PIT #12 铁律, ALTER TABLE 2026-06-19)
- 600s (10min) CRITICAL 阈值线
- 三种状态颜色: 🟢 HEALTHY / 🟡 WARNING 临时并发 / 🔴 CRITICAL 真泄漏

PIT 防御集成:
- 走 PIT #143 storage_factory.get_db_conn + release_db_conn (避免 PIT #138 漏还)
- 用 _safe_num 防御 None (PIT #117)
- st.cache_data 60s 缓存避免重复查询 (PIT #148 dashboard 不应触发高频 cron)
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "dashboard_views"))

from _shared import get_db_connection, release_db_conn  # PIT #143 重导出
from _shared_utils import _safe_num  # PIT #117 防御


# ── 数据库查询 ──────────────────────────────────────────────────────────

def load_pool_health_metrics(hours: int = 24) -> pd.DataFrame:
    """加载最近 N 小时的池健康指标 (60s 缓存, 避免高频查询)

    PIT #12 铁律: 已用 information_schema.columns 验证 saturated_duration_s 列存在
    (2026-06-19 ALTER TABLE ADD COLUMN IF NOT EXISTS)

    Cache: 用 st.cache_data 但保留 __wrapped__ 属性便于测试 mock (PIT #136 测试隔离)
    """
    conn = get_db_connection()
    if not conn:
        return pd.DataFrame()

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT timestamp, level, message,
                   in_use, peak_in_use, maxconn, invariant,
                   saturated_duration_s
            FROM audit.pool_health_metrics
            WHERE timestamp >= NOW() - INTERVAL '%s hours'
            ORDER BY timestamp ASC
        """, (hours,))
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=[
            "timestamp", "level", "message",
            "in_use", "peak_in_use", "maxconn", "invariant",
            "saturated_duration_s"
        ])
        # PIT #117: _safe_num 防御 None
        # 注意: pandas 自动把 None 转 NaN, _safe_num 无法识别 NaN, 用 mask 显式处理
        for col in ["in_use", "peak_in_use", "maxconn", "saturated_duration_s"]:
            default = 20 if col == "maxconn" else 0
            df[col] = df[col].apply(lambda x: _safe_num(x, default=default) if pd.notna(x) else default)
            # 兜底: 仍为 NaN (e.g. None 漏处理) 强制转 0
            df[col] = df[col].fillna(default)

        return df
    except Exception as e:
        st.error(f"加载池健康指标失败: {e}")
        return pd.DataFrame()
    finally:
        release_db_conn(conn)  # PIT #143 归还池


# streamlit cache 装饰器 (放在函数定义后, 避免 mock 干扰)
load_pool_health_metrics = st.cache_data(ttl=60)(load_pool_health_metrics)


def get_current_pool_health() -> dict:
    """当前池快照 (调 PoolManager.health_check, 不查历史表)"""
    try:
        from pool_manager import get_pool
        return get_pool().health_check()
    except Exception as e:
        return {"error": str(e)}


# ── 视图渲染 ────────────────────────────────────────────────────────────

def render_pool_health():
    """PG 池健康监控页 (PIT #148 集成)"""
    st.markdown("## 🏊 PG 连接池健康监控")
    st.caption("PoolManager 单例 · health_check 不变量 · PIT #148 持续时长阈值")

    # ── 当前快照 ────────────────────────────────────────────────────────
    with st.expander("📸 当前池快照", expanded=True):
        health = get_current_pool_health()

        if "error" in health:
            st.error(f"PoolManager.health_check 失败: {health['error']}")
            return

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("当前 in_use", f"{health.get('in_use', 0)}/{health.get('maxconn', 20)}")
        col2.metric("当前 peak", f"{health.get('peak_in_use', 0)}/{health.get('maxconn', 20)}")
        col3.metric("get / put", f"{health.get('get_count', 0)} / {health.get('put_count', 0)}")
        col4.metric("Invariant",
                    health.get("invariant", "?"),
                    delta="✓" if health.get("healthy", False) else "✗",
                    delta_color="normal" if health.get("healthy", False) else "inverse")

        # PIT #148: 持续时长字段
        saturated_dur = health.get("maxconn_saturated_duration_s")
        saturated_since = health.get("maxconn_saturated_since")
        col5, col6 = st.columns(2)
        if saturated_dur is not None and saturated_dur > 0:
            col5.metric("🔴 池满持续时长", f"{saturated_dur:.0f}s",
                       delta=f"≥600s = 真泄漏" if saturated_dur >= 600 else f"<600s = 临时并发",
                       delta_color="inverse" if saturated_dur >= 600 else "off")
            if saturated_since:
                col6.metric("池满开始", datetime.fromtimestamp(saturated_since).strftime("%H:%M:%S"))
        else:
            col5.metric("池满持续时长", "0s", delta="🟢 健康", delta_color="normal")
            col6.metric("池满开始", "—")

        # invariant violations
        violations = health.get("invariant_violations", 0)
        if violations > 0:
            st.warning(f"⚠️ invariant 违反 {violations} 次: {health.get('last_violation_msg', '?')}")

    # ── 时间范围选择 ────────────────────────────────────────────────────
    st.divider()
    hours = st.selectbox("📅 时间范围", [1, 6, 24, 168], index=2,
                         format_func=lambda x: f"{x}h ({x//24}天)" if x >= 24 else f"{x}h",
                         key="pool_hours")

    df = load_pool_health_metrics(hours)

    if df.empty:
        st.info(f"近 {hours}h 暂无池健康数据 (PoolMonitor cron 5min 跑, 请等待)")
        return

    # ── 趋势图 1: in_use 比例 ───────────────────────────────────────────
    st.divider()
    st.markdown("### 📊 连接池使用率趋势")

    df["usage_pct"] = (df["in_use"] / df["maxconn"] * 100).round(1)
    df["peak_pct"] = (df["peak_in_use"] / df["maxconn"] * 100).round(1)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["usage_pct"],
        name="in_use %", mode="lines+markers",
        line=dict(color="#3b82f6", width=2),
        marker=dict(size=4),
    ))
    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["peak_pct"],
        name="peak_in_use %", mode="lines",
        line=dict(color="#94a3b8", width=1, dash="dot"),
    ))

    # 阈值线 (PIT #141)
    fig.add_hline(y=80, line_dash="dash", line_color="orange",
                  annotation_text="CRITICAL 阈值 (80%)", annotation_position="top left")
    fig.add_hline(y=50, line_dash="dash", line_color="yellow",
                  annotation_text="WARNING 阈值 (50%)", annotation_position="top left")

    fig.update_layout(
        yaxis_title="使用率 (%)", xaxis_title="时间",
        height=350, hovermode="x unified",
        margin=dict(l=0, r=0, t=20, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── 趋势图 2: 池满持续时长 (PIT #148 核心) ─────────────────────────
    st.divider()
    st.markdown("### ⏱️ 池满持续时长趋势 (PIT #148)")
    st.caption(
        "🟡 <600s = 临时并发 (e.g. cron 同时跑, 跑完自然回落) | "
        "🔴 ≥600s = 真泄漏预警 (异常 conn 永不归还, 应立即排查)"
    )

    # 只显示 saturated_duration > 0 的点 (健康时全是 0, 不画图)
    df_sat = df[df["saturated_duration_s"] > 0].copy()

    if df_sat.empty:
        st.info(f"🟢 近 {hours}h 无池满事件 (所有 cron 都健康归还连接)")
    else:
        # 颜色编码: <600s 黄色, ≥600s 红色
        colors = df_sat["saturated_duration_s"].apply(
            lambda x: "#ef4444" if x >= 600 else "#f59e0b"
        )

        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=df_sat["timestamp"], y=df_sat["saturated_duration_s"],
            mode="markers+lines",
            name="saturated_duration_s",
            line=dict(color="#fbbf24", width=2),
            marker=dict(size=8, color=colors, line=dict(color="white", width=1)),
            text=df_sat["message"].str[:60],
            hovertemplate="<b>%{x}</b><br>持续: %{y:.0f}s<br>%{text}<extra></extra>",
        ))

        # PIT #148 阈值线
        fig2.add_hline(y=600, line_dash="dash", line_color="red",
                       annotation_text="🔴 CRITICAL 阈值 (600s=10min)",
                       annotation_position="top right")

        fig2.update_layout(
            yaxis_title="持续时长 (秒)", xaxis_title="时间",
            height=350, hovermode="x unified",
            margin=dict(l=0, r=0, t=20, b=0),
        )
        st.plotly_chart(fig2, use_container_width=True)

        # 统计
        st.markdown("#### 池满事件统计")
        col_a, col_b, col_c, col_d = st.columns(4)
        col_a.metric("总事件数", len(df_sat))
        col_b.metric("临时并发 (<600s)",
                     len(df_sat[df_sat["saturated_duration_s"] < 600]))
        col_c.metric("真泄漏 (≥600s)",
                     len(df_sat[df_sat["saturated_duration_s"] >= 600]),
                     delta="🔴" if len(df_sat[df_sat["saturated_duration_s"] >= 600]) > 0 else None,
                     delta_color="inverse")
        col_d.metric("平均持续",
                     f"{df_sat['saturated_duration_s'].mean():.0f}s")

    # ── 告警事件列表 ────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 🚨 告警事件列表")

    df_alerts = df[df["level"].isin(["warning", "critical"])].copy()
    df_alerts = df_alerts.sort_values("timestamp", ascending=False).head(20)

    if df_alerts.empty:
        st.success(f"🟢 近 {hours}h 无告警 (全部 healthy)")
    else:
        # emoji 化级别
        df_alerts["level_emoji"] = df_alerts["level"].apply(
            lambda x: "🔴 CRITICAL" if x == "critical" else "🟡 WARNING"
        )
        display_df = df_alerts[["timestamp", "level_emoji", "message", "in_use", "maxconn", "saturated_duration_s"]].copy()
        display_df.columns = ["时间", "级别", "消息", "in_use", "maxconn", "持续(s)"]
        display_df["时间"] = display_df["时间"].dt.strftime("%Y-%m-%d %H:%M:%S")
        display_df["持续(s)"] = display_df["持续(s)"].apply(lambda x: f"{x:.0f}" if x > 0 else "—")

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "级别": st.column_config.TextColumn(width="small"),
                "消息": st.column_config.TextColumn(width="large"),
            },
        )

    # ── 底部说明 ────────────────────────────────────────────────────────
    with st.expander("ℹ️ 关于此页面 (PIT #148 设计)", expanded=False):
        st.markdown("""
        **PIT #148 实战教训 (2026-06-19 17:39)**:
        - PIT #141 cron 仅看 `in_use/maxconn` 比例, 实战 5min 临时并发占满会误推 CRITICAL → 飞书 spam
        - 修复: 加 `saturated_duration_s` 字段, **临时并发 WARNING 静默 (不推飞书), 真泄漏 ≥600s CRITICAL 推飞书**

        **监控维度**:
        - **瞬时比例** (in_use%): 当前池使用率, 反映并发压力
        - **持续时长** (saturated_duration_s): 池满持续时间, 区分临时并发 vs 真泄漏
        - **invariant**: PIT #142 借/还不变量检查, 异常时触发

        **铁律**:
        - PIT #143 走池 (storage_factory.get_db_conn + release_db_conn)
        - PIT #117 _safe_num 防御 None
        - PIT #148 持续时长 vs 比例双维度
        - 部分索引: `audit.pool_health_metrics(saturated_duration_s) WHERE IS NOT NULL` 节省空间
        """)


if __name__ == "__main__":
    # Streamlit 单独测试入口 (直接 `streamlit run _pool_health.py`)
    st.set_page_config(page_title="Pool Health", layout="wide")
    render_pool_health()