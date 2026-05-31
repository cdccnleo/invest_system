"""
test_report_summarizer.py — 研报摘要模块单元测试
"""
import pytest
from unittest.mock import patch, MagicMock


class TestSummarizeReport:
    """单篇摘要生成测试"""

    @patch("report_summarizer._ensure_summary_column")
    def test_empty_content(self, mock_ensure):
        from report_summarizer import summarize_report
        result = summarize_report("测试标题", "")
        assert result == ""

    @patch("report_summarizer._ensure_summary_column")
    def test_empty_title(self, mock_ensure):
        from report_summarizer import summarize_report
        result = summarize_report("", "测试内容")
        assert result == ""

    @patch("report_summarizer._ensure_summary_column")
    @patch("agent_interface.get_agent")
    def test_successful_summary(self, mock_agent, mock_ensure):
        from report_summarizer import summarize_report
        mock_agent.return_value.chat.return_value = {"content": "核心观点：买入评级，目标价50元。", "error": None}

        result = summarize_report("测试研报", "这是一篇测试研报，包含详细分析内容。")
        assert len(result) > 0
        assert "核心观点" in result

    @patch("report_summarizer._ensure_summary_column")
    @patch("agent_interface.get_agent")
    def test_llm_error(self, mock_agent, mock_ensure):
        from report_summarizer import summarize_report
        mock_agent.return_value.chat.return_value = {"content": "", "error": "API Error"}

        result = summarize_report("测试研报", "内容")
        assert result == ""


class TestSummarizeReportsBatch:
    """批量摘要测试"""

    @patch("report_summarizer._ensure_summary_column")
    @patch("report_summarizer._get_db_conn")
    def test_no_unsummarized_reports(self, mock_db, mock_ensure):
        from report_summarizer import summarize_reports_batch
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_db.return_value = mock_conn

        result = summarize_reports_batch(days=7, limit=5)
        assert result["summarized"] == 0
        assert result["success"] == 0


class TestGetReportsWithSummary:
    """获取带摘要研报测试"""

    @patch("report_summarizer._ensure_summary_column")
    @patch("report_summarizer._get_db_conn")
    def test_returns_list(self, mock_db, mock_ensure):
        from report_summarizer import get_reports_with_summary
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            {"id": 1, "title": "测试", "summary": "摘要",
             "report_date": "2026-05-31", "org_name": "测试",
             "info_code": "T001", "stock_name": "测试",
             "rating": "买入", "target_price": "50"}
        ]
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_db.return_value = mock_conn

        reports = get_reports_with_summary(days=7, limit=5)
        assert isinstance(reports, list)


class TestInjectSummariesToTAMF:
    """TAMF 注入测试"""

    def test_empty_summaries(self):
        from report_summarizer import inject_summaries_to_tamf
        result = inject_summaries_to_tamf("000001.XSHE", [])
        assert result is False

    @patch("tamf_updater.get_tamf_path")
    def test_no_tamf_file(self, mock_path):
        from report_summarizer import inject_summaries_to_tamf
        mock_path.return_value = None
        result = inject_summaries_to_tamf("000001.XSHE", ["摘要1"])
        assert result is False

    @patch("tamf_updater.get_tamf_path")
    def test_successful_injection(self, mock_path):
        from report_summarizer import inject_summaries_to_tamf
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("# TAMF 测试\n\n## 基本信息\n测试内容\n")
            f.flush()
            mock_path.return_value = __import__("pathlib").Path(f.name)

        try:
            result = inject_summaries_to_tamf("000001.XSHE", ["摘要1", "摘要2"])
            assert result is True
        finally:
            os.unlink(f.name)