"""
test_health_monitor.py — 系统健康监控单元测试
"""
import pytest
from unittest.mock import patch, MagicMock


class TestCheckDBConnectivity:
    """数据库连通性检查测试"""

    @patch("health_monitor._get_db_conn")
    def test_successful_connection(self, mock_db):
        from health_monitor import check_db_connectivity
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_db.return_value = mock_conn

        result = check_db_connectivity()
        assert result["status"] in ("ok", "slow")
        assert "latency_ms" in result

    @patch("health_monitor._get_db_conn")
    def test_failed_connection(self, mock_db):
        from health_monitor import check_db_connectivity
        mock_db.side_effect = Exception("Connection refused")

        result = check_db_connectivity()
        assert result["status"] == "error"


import shutil as _shutil_module


class TestCheckDiskUsage:
    """磁盘使用率检查测试"""

    @patch.object(_shutil_module, "disk_usage")
    def test_normal_usage(self, mock_disk):
        from health_monitor import check_disk_usage
        mock_disk.return_value = MagicMock(used=50e9, total=200e9, free=150e9)

        result = check_disk_usage()
        assert result["status"] == "ok"
        assert result["usage_pct"] < 70

    @patch.object(_shutil_module, "disk_usage")
    def test_high_usage_warning(self, mock_disk):
        from health_monitor import check_disk_usage
        mock_disk.return_value = MagicMock(used=180e9, total=200e9, free=20e9)

        result = check_disk_usage()
        assert result["status"] in ("critical", "warning")


class TestCheckAPIRate:
    """API 错误率检查测试"""

    @patch("health_monitor._get_db_conn")
    def test_no_errors(self, mock_db):
        from health_monitor import check_api_error_rate
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (100, 0)
        mock_conn.cursor.return_value = mock_cur
        mock_db.return_value = mock_conn

        result = check_api_error_rate()
        assert result["status"] == "ok"
        assert result["error_rate"] == 0.0


class TestRunHealthCheck:
    """全量健康检查测试"""

    @patch("health_monitor.check_db_connectivity")
    @patch("health_monitor.check_disk_usage")
    @patch("health_monitor.check_api_error_rate")
    @patch("health_monitor.check_schedule_health")
    @patch("health_monitor.check_news_freshness")
    @patch("health_monitor.check_quote_freshness")
    def test_all_healthy(self, *mocks):
        from health_monitor import run_health_check
        for mock in mocks:
            mock.return_value = {"status": "ok", "message": "正常"}

        result = run_health_check()
        assert result["overall"] == "healthy"
        assert len(result["checks"]) == 6
        assert "timestamp" in result

    @patch("health_monitor.check_db_connectivity")
    @patch("health_monitor.check_disk_usage")
    @patch("health_monitor.check_api_error_rate")
    @patch("health_monitor.check_schedule_health")
    @patch("health_monitor.check_news_freshness")
    @patch("health_monitor.check_quote_freshness")
    def test_with_warnings(self, mock_quote, mock_news, mock_sched, mock_api, mock_disk, mock_db):
        from health_monitor import run_health_check
        mock_db.return_value = {"status": "ok", "message": "正常"}
        mock_disk.return_value = {"status": "ok", "message": "正常"}
        mock_api.return_value = {"status": "ok", "message": "正常"}
        mock_sched.return_value = {"status": "ok", "message": "正常"}
        mock_news.return_value = {"status": "warning", "message": "新闻过期"}
        mock_quote.return_value = {"status": "warning", "message": "行情过期"}

        result = run_health_check()
        assert result["overall"] == "warning"
        assert len(result["alerts"]) == 2


class TestGetHealthSummary:
    """健康摘要获取测试"""

    @patch("health_monitor.run_health_check")
    def test_returns_summary(self, mock_run):
        from health_monitor import get_health_summary
        mock_run.return_value = {
            "overall": "healthy",
            "timestamp": "2026-05-31T12:00:00",
            "checks": {"db_connectivity": {"status": "ok", "message": "ok"}},
            "alerts": [],
        }

        result = get_health_summary()
        assert result["overall"] == "healthy"
        assert result["alert_count"] == 0
        assert "checks" in result