"""
test_pool_health_view.py — dashboard _pool_health.py 单元测试 (PIT #148)

PIT #148 集成: 验证 saturated_duration_s 字段正确读取 + 趋势图数据构造

测试覆盖:
1. load_pool_health_metrics (mock DB): 返回 DataFrame 含 saturated_duration_s 列
2. PIT #117 _safe_num 防御 None: in_use/peak/maxconn/saturated 全部 _safe_num
3. PIT #12 铁律: SQL 查 saturated_duration_s (PIT #148 新列)
4. get_current_pool_health: 调 PoolManager.health_check 暴露新字段
5. release_db_conn 归还池 (PIT #143 防御)

设计:
- 全 mock (无 DB 连接), 1s 内跑完
- 不直接测 streamlit UI (那是 E2E 测试范围, 不属于单元测试)
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "dashboard_views"))


class TestLoadPoolHealthMetrics:
    """PIT #148: load_pool_health_metrics DB 查询 + _safe_num 防御"""

    @patch("_pool_health.get_db_connection")
    @patch("_pool_health.release_db_conn")
    def test_returns_dataframe_with_saturated_column(self, mock_release, mock_get_conn):
        """PIT #148: 必须返回 saturated_duration_s 列"""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        # 模拟 DB 返回多行
        mock_cur.fetchall.return_value = [
            ("2026-06-19 20:00:00", "healthy", "🟢 健康", 5, 10, 20, "OK", None),
            ("2026-06-19 20:05:00", "warning", "🟡 池满 30s", 20, 20, 20, "OK", 30.0),
            ("2026-06-19 20:10:00", "critical", "🔴 池满 700s", 20, 20, 20, "OK", 700.0),
        ]
        mock_get_conn.return_value = mock_conn

        from _pool_health import load_pool_health_metrics
        # 绕过 st.cache_data (测试隔离, PIT #136 防御)
        df = load_pool_health_metrics.__wrapped__(hours=24)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert "saturated_duration_s" in df.columns, "PIT #148: 缺 saturated_duration_s 列"

        # _safe_num 防御后 None → 0
        assert df.iloc[0]["saturated_duration_s"] == 0
        assert df.iloc[1]["saturated_duration_s"] == 30.0
        assert df.iloc[2]["saturated_duration_s"] == 700.0

        # release_db_conn 必须被调 (PIT #143 防御)
        mock_release.assert_called_once_with(mock_conn)

    @patch("_pool_health.get_db_connection")
    @patch("_pool_health.release_db_conn")
    def test_safe_num_defends_none(self, mock_release, mock_get_conn):
        """PIT #117: in_use/peak/maxconn/saturated 字段 None 时 _safe_num → 0"""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        # 模拟一行全 None (异常情况)
        mock_cur.fetchall.return_value = [
            ("2026-06-19 20:00:00", "healthy", "?", None, None, None, "OK", None),
        ]
        mock_get_conn.return_value = mock_conn

        from _pool_health import load_pool_health_metrics
        df = load_pool_health_metrics.__wrapped__(hours=1)

        # 全部 None 应被 _safe_num 兜底为 0
        assert df.iloc[0]["in_use"] == 0
        assert df.iloc[0]["peak_in_use"] == 0
        assert df.iloc[0]["maxconn"] == 20  # _safe_num default=20
        assert df.iloc[0]["saturated_duration_s"] == 0

    @patch("_pool_health.get_db_connection")
    @patch("_pool_health.release_db_conn")
    def test_empty_result_returns_empty_dataframe(self, mock_release, mock_get_conn):
        """无数据时返回空 DataFrame (不抛异常)"""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        from _pool_health import load_pool_health_metrics
        df = load_pool_health_metrics.__wrapped__(hours=1)

        assert isinstance(df, pd.DataFrame)
        assert df.empty

    @patch("_pool_health.get_db_connection")
    def test_db_error_returns_empty_dataframe(self, mock_get_conn):
        """DB 异常 → 返回空 DataFrame, 写 st.error (PIT #119 兼容)"""
        mock_get_conn.return_value = None  # DB 连接失败

        from _pool_health import load_pool_health_metrics
        df = load_pool_health_metrics.__wrapped__(hours=1)

        assert isinstance(df, pd.DataFrame)
        assert df.empty

    @patch("_pool_health.get_db_connection")
    @patch("_pool_health.release_db_conn")
    def test_uses_interval_hours_param(self, mock_release, mock_get_conn):
        """验证 SQL 用了 INTERVAL '%s hours' (PIT #12 防御 SQL 注入)"""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        from _pool_health import load_pool_health_metrics
        load_pool_health_metrics.__wrapped__(hours=168)  # 7 天

        # 验证 SQL 包含 INTERVAL (参数化查询)
        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "INTERVAL" in sql
        assert "%s" in sql, "必须用参数化查询 (PIT #12 防御 SQL 注入)"
        assert params == (168,), f"hours 参数应传递: {params}"


class TestGetCurrentPoolHealth:
    """get_current_pool_health: 调 PoolManager.health_check"""

    @patch("pool_manager.get_pool")
    def test_returns_health_dict(self, mock_get_pool):
        mock_pool = MagicMock()
        mock_pool.health_check.return_value = {
            "in_use": 5, "maxconn": 20, "peak_in_use": 10,
            "healthy": True, "invariant": "OK",
            "maxconn_saturated_since": None,
            "maxconn_saturated_duration_s": None,
        }
        mock_get_pool.return_value = mock_pool

        from _pool_health import get_current_pool_health
        result = get_current_pool_health()

        assert "in_use" in result
        assert "maxconn_saturated_duration_s" in result

    @patch("pool_manager.get_pool")
    def test_error_returns_error_dict(self, mock_get_pool):
        """PoolManager 异常 → 返回 dict 带 error 字段 (不抛)"""
        mock_get_pool.side_effect = Exception("DB not reachable")

        from _pool_health import get_current_pool_health
        result = get_current_pool_health()

        assert "error" in result
        assert "DB not reachable" in result["error"]


class TestPoolHealthRender:
    """render_pool_health: streamlit 渲染主入口

    跳过 streamlit runtime test (需 ScriptRunContext).
    验证策略: 通过 patch 函数体, 不实际跑渲染.
    """

    def test_render_module_imports_successfully(self):
        """_pool_health.py 能正常 import (基本 smoke test)"""
        # 重 import 确保 module-level code 不抛
        import importlib
        import _pool_health
        importlib.reload(_pool_health)
        assert hasattr(_pool_health, "render_pool_health")
        assert hasattr(_pool_health, "load_pool_health_metrics")
        assert hasattr(_pool_health, "get_current_pool_health")

    def test_render_pool_health_calls_st_functions(self):
        """render_pool_health 调用 st.markdown/st.expander/st.columns 等 streamlit API"""
        from _pool_health import render_pool_health, get_current_pool_health, load_pool_health_metrics
        import streamlit as real_st

        # 用 mock 替换 _pool_health 模块内的 st 引用 (而不是 streamlit module)
        mock_st = MagicMock()
        mock_st.columns.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        mock_st.expander.return_value.__enter__ = MagicMock()
        mock_st.expander.return_value.__exit__ = MagicMock()

        with patch.object(get_current_pool_health, '__call__', return_value={
            "in_use": 0, "maxconn": 20, "peak_in_use": 0,
            "healthy": True, "invariant": "OK",
            "maxconn_saturated_since": None,
            "maxconn_saturated_duration_s": None,
        }), patch.object(load_pool_health_metrics, '__wrapped__', return_value=pd.DataFrame()), patch.dict("_pool_health.__dict__", {"st": mock_st}):
            render_pool_health()

        # 验证调了 streamlit API
        assert mock_st.markdown.called
        assert mock_st.expander.called
        assert mock_st.columns.called

    def test_render_handles_pool_error(self):
        """PoolManager 异常 → 显示 error + return (不抛)"""
        from _pool_health import render_pool_health, get_current_pool_health

        mock_st = MagicMock()
        mock_st.expander.return_value.__enter__ = MagicMock()
        mock_st.expander.return_value.__exit__ = MagicMock()

        with patch.object(get_current_pool_health, '__call__', return_value={"error": "DB unreachable"}), patch.dict("_pool_health.__dict__", {"st": mock_st}):
            render_pool_health()

        # 验证 st.error 调了
        assert mock_st.error.called
        assert mock_st.markdown.called


class TestPoolHealthDataFiltering:
    """PIT #148 趋势图数据过滤逻辑 (饱和时长 > 0 才画图)"""

    def test_filter_saturated_events(self):
        """df_sat = df[df['saturated_duration_s'] > 0] (健康时全 0, 不画)"""
        df = pd.DataFrame({
            "timestamp": pd.date_range("2026-06-19 20:00", periods=5, freq="5min"),
            "level": ["healthy", "warning", "healthy", "critical", "healthy"],
            "saturated_duration_s": [0, 30, 0, 700, 0],
            "message": ["ok", "tmp", "ok", "leak", "ok"],
        })

        df_sat = df[df["saturated_duration_s"] > 0].copy()

        assert len(df_sat) == 2
        assert list(df_sat["saturated_duration_s"]) == [30, 700]
        assert list(df_sat["level"]) == ["warning", "critical"]

    def test_filter_critical_events(self):
        """CRITICAL 事件列表: level in ['warning', 'critical']"""
        df = pd.DataFrame({
            "timestamp": pd.date_range("2026-06-19 20:00", periods=4, freq="5min"),
            "level": ["healthy", "warning", "critical", "healthy"],
            "message": ["ok", "tmp", "leak", "ok"],
        })

        alerts = df[df["level"].isin(["warning", "critical"])].copy()

        assert len(alerts) == 2
        assert "healthy" not in alerts["level"].values


# ─── pytest markers ───────────────────────────────────────────────────

# 默认全跑 (无 marker), 无副作用
# 跑法: pytest tests/test_pool_health_view.py -v