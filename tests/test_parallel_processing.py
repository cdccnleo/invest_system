"""
test_parallel_processing.py — 并行处理模块单元测试
"""

import pytest
from unittest.mock import patch, MagicMock
import time


class TestParallelUpdateAllHoldings:
    """parallel_update_all_holdings 函数测试"""

    def test_import_function(self):
        """函数可导入"""
        from tamf_updater import parallel_update_all_holdings
        assert callable(parallel_update_all_holdings)

    def test_empty_positions(self):
        """空持仓列表应快速返回"""
        from tamf_updater import parallel_update_all_holdings
        with patch("tamf_updater.load_positions", return_value=[]):
            result = parallel_update_all_holdings(max_workers=2)
            assert result["total"] == 0
            assert result["updated"] == 0
            assert result["failed"] == 0

    def test_small_positions_list(self):
        """少量持仓应正常处理"""
        from tamf_updater import parallel_update_all_holdings

        mock_positions = [
            {"code": "000977"},
            {"code": "600519"},
            {"code": "300059"},
        ]

        mock_result = {"status": "updated"}

        with patch("tamf_updater.load_positions", return_value=mock_positions), \
             patch("tamf_updater.incremental_update", return_value=mock_result):
            result = parallel_update_all_holdings(max_workers=2)
            assert result["total"] == 3
            assert result["updated"] == 3
            assert result["failed"] == 0

    def test_mixed_results(self):
        """混合结果（部分成功、部分跳过）"""
        from tamf_updater import parallel_update_all_holdings

        mock_positions = [
            {"code": "000977"},
            {"code": "600519"},
            {"code": "300059"},
            {"code": "002050"},
        ]

        def mock_update(code):
            if code == "300059":
                return {"status": "no_change"}
            return {"status": "updated"}

        with patch("tamf_updater.load_positions", return_value=mock_positions), \
             patch("tamf_updater.incremental_update", side_effect=mock_update):
            result = parallel_update_all_holdings(max_workers=2)
            assert result["total"] == 4
            assert result["updated"] == 3
            assert result["skipped"] == 1

    def test_failure_handling(self):
        """单个标的失败不应影响其他标的"""
        from tamf_updater import parallel_update_all_holdings

        mock_positions = [
            {"code": "000977"},
            {"code": "BAD_CODE"},
            {"code": "600519"},
        ]

        def mock_update(code):
            if code == "BAD_CODE":
                raise ValueError("模拟错误")
            return {"status": "updated"}

        with patch("tamf_updater.load_positions", return_value=mock_positions), \
             patch("tamf_updater.incremental_update", side_effect=mock_update):
            result = parallel_update_all_holdings(max_workers=2)
            assert result["total"] == 3
            assert result["updated"] == 2
            assert result["failed"] == 1
            assert result["details"]["BAD_CODE"]["status"] == "error"

    def test_performance_improvement(self):
        """并行应比串行更快（模拟场景）"""
        from tamf_updater import parallel_update_all_holdings

        mock_positions = [{"code": str(i).zfill(6)} for i in range(12)]

        def slow_update(code):
            time.sleep(0.05)
            return {"status": "updated"}

        with patch("tamf_updater.load_positions", return_value=mock_positions), \
             patch("tamf_updater.incremental_update", side_effect=slow_update):
            start = time.time()
            result = parallel_update_all_holdings(max_workers=4)
            elapsed = time.time() - start
            assert result["updated"] == 12
            # 12 tasks * 0.05s = 0.6s serial, with 4 workers should be ~0.15s
            assert elapsed < 0.6, f"并行耗时 {elapsed:.2f}s 应小于串行 0.6s"


class TestParallelCollectCloseData:
    """_parallel_collect_close_data 函数测试"""

    def test_function_importable(self):
        """函数可从 schedule_runner 访问"""
        import sys
        sys.path.insert(0, ".")
        try:
            from schedule_runner import _parallel_collect_close_data
            assert callable(_parallel_collect_close_data)
        except ImportError:
            pytest.skip("schedule_runner 依赖过多外部模块")