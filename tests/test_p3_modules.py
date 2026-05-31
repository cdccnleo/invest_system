"""
test_p3_modules.py — P3阶段模块单元测试汇总
覆盖: data_source, strategy_optimizer, skill_pipeline, report_generator
"""
import pytest
import numpy as np
from unittest.mock import patch, MagicMock


# ============================================================================
# Data Source Tests
# ============================================================================

class TestDataSourceRegistry:
    """数据源注册中心测试"""

    def test_singleton(self):
        from data_source import get_registry, DataSourceRegistry
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_register_and_get(self):
        from data_source import DataSourceRegistry, AkshareSource
        registry = DataSourceRegistry()
        source = AkshareSource()
        registry.register(source)
        assert registry.get("akshare") is source

    def test_list_sources(self):
        from data_source import DataSourceRegistry, AkshareSource
        registry = DataSourceRegistry()
        registry.register(AkshareSource())
        sources = registry.list_sources()
        assert len(sources) == 1
        assert sources[0]["name"] == "akshare"

    def test_get_for_market_hk(self):
        from data_source import DataSourceRegistry, AkshareHKSource
        registry = DataSourceRegistry()
        registry.register(AkshareHKSource())
        source = registry.get_for_market("00700.HK")
        assert source is not None
        assert source.name == "akshare_hk"

    def test_get_for_market_a(self):
        from data_source import DataSourceRegistry, AkshareSource
        registry = DataSourceRegistry()
        registry.register(AkshareSource())
        source = registry.get_for_market("000001.XSHE")
        assert source is not None


class TestInitDefaultSources:
    """默认数据源初始化测试"""

    def test_init_registers_both(self):
        from data_source import init_default_sources, get_registry
        init_default_sources()
        registry = get_registry()
        sources = registry.list_sources()
        assert len(sources) >= 2


# ============================================================================
# Strategy Optimizer Tests
# ============================================================================

class TestParamSpace:
    """参数空间测试"""

    def test_int_sampling(self):
        from strategy_optimizer import ParamSpace
        ps = ParamSpace("test", 1, 10, 2, "int")
        for _ in range(20):
            val = ps.sample()
            assert 1 <= val <= 12
            assert isinstance(val, (int, np.integer))

    def test_float_sampling(self):
        from strategy_optimizer import ParamSpace
        ps = ParamSpace("test", 0.0, 1.0, 0.1, "float")
        for _ in range(20):
            val = ps.sample()
            assert 0.0 <= val <= 1.0


class TestBayesianOptimizer:
    """贝叶斯优化器测试"""

    def test_basic_optimization(self):
        from strategy_optimizer import BayesianOptimizer, ParamSpace
        param_spaces = [
            ParamSpace("x", 1, 10, 1, "int"),
            ParamSpace("y", 0.0, 1.0, 0.1, "float"),
        ]

        def objective(params):
            x = params["x"]
            y = params["y"]
            return -(x - 5) ** 2 - (y - 0.5) ** 2 * 10 + 10

        optimizer = BayesianOptimizer(param_spaces, objective, n_initial=5, n_iter=15)
        result = optimizer.optimize()

        assert result.best_score is not None
        assert len(result.all_trials) > 0
        assert result.best_params is not None
        assert "x" in result.best_params

    def test_convergence_tracking(self):
        from strategy_optimizer import BayesianOptimizer, ParamSpace
        param_spaces = [ParamSpace("x", 1, 10, 1, "int")]

        def objective(params):
            return params["x"] * 1.0

        optimizer = BayesianOptimizer(param_spaces, objective, n_initial=3, n_iter=5)
        result = optimizer.optimize()
        assert len(result.convergence) > 0


class TestGridSearchOptimizer:
    """网格搜索优化器测试"""

    def test_basic_grid_search(self):
        from strategy_optimizer import GridSearchOptimizer, ParamSpace
        param_spaces = [
            ParamSpace("x", 1, 3, 1, "int"),
        ]

        def objective(params):
            return params["x"] * 2.0

        optimizer = GridSearchOptimizer(param_spaces, objective)
        result = optimizer.optimize()
        assert result.best_params["x"] == 3
        assert result.best_score == 6.0
        assert len(result.all_trials) == 3


# ============================================================================
# Skill Pipeline Tests
# ============================================================================

class TestSkillPipeline:
    """技能流水线测试"""

    def test_single_step(self):
        from skill_pipeline import SkillPipeline, PipelineStep
        step = PipelineStep(
            name="test",
            func=lambda context, deps, **kwargs: "done",
        )
        pipeline = SkillPipeline("test_pipe", [step])
        result = pipeline.run()
        assert result["status"] == "completed"
        assert result["results"]["test"].output == "done"

    def test_dependency_order(self):
        from skill_pipeline import SkillPipeline, PipelineStep
        execution_order = []

        steps = [
            PipelineStep(
                name="step1",
                func=lambda context, deps, **kwargs: execution_order.append("step1"),
            ),
            PipelineStep(
                name="step2",
                func=lambda context, deps, **kwargs: execution_order.append("step2"),
                depends_on=["step1"],
            ),
            PipelineStep(
                name="step3",
                func=lambda context, deps, **kwargs: execution_order.append("step3"),
                depends_on=["step2"],
            ),
        ]
        pipeline = SkillPipeline("dep_test", steps)
        result = pipeline.run()
        assert result["status"] == "completed"
        assert execution_order == ["step1", "step2", "step3"]

    def test_condition_skip(self):
        from skill_pipeline import SkillPipeline, PipelineStep, StepStatus
        step = PipelineStep(
            name="skip_me",
            func=lambda context, deps, **kwargs: "never",
            condition=lambda ctx: False,
        )
        pipeline = SkillPipeline("cond_test", [step])
        result = pipeline.run()
        assert result["results"]["skip_me"].status == StepStatus.SKIPPED

    def test_on_error_continue(self):
        from skill_pipeline import SkillPipeline, PipelineStep, StepStatus
        step1 = PipelineStep(
            name="fail_step",
            func=lambda context, deps, **kwargs: (_ for _ in ()).throw(Exception("test error")),
            on_error="continue",
        )
        step2 = PipelineStep(
            name="normal_step",
            func=lambda context, deps, **kwargs: "ok",
            depends_on=["fail_step"],
        )
        pipeline = SkillPipeline("error_test", [step1, step2])
        result = pipeline.run()
        assert result["results"]["fail_step"].status == StepStatus.FAILED
        assert result["results"]["normal_step"].status == StepStatus.COMPLETED

    def test_on_error_stop(self):
        from skill_pipeline import SkillPipeline, PipelineStep
        step1 = PipelineStep(
            name="fail_stop",
            func=lambda context, deps, **kwargs: (_ for _ in ()).throw(Exception("fatal")),
            on_error="stop",
        )
        step2 = PipelineStep(
            name="never_reach",
            func=lambda context, deps, **kwargs: "never",
            depends_on=["fail_stop"],
        )
        pipeline = SkillPipeline("fatal_test", [step1, step2])
        result = pipeline.run()
        assert result["status"] == "failed"
        assert "never_reach" not in result["results"]

    def test_context_sharing(self):
        from skill_pipeline import SkillPipeline, PipelineStep
        step1 = PipelineStep(
            name="producer",
            func=lambda context, deps, **kwargs: {"value": 42},
        )
        step2 = PipelineStep(
            name="consumer",
            func=lambda context, deps, **kwargs: context.get("producer", {}).get("value"),
            depends_on=["producer"],
        )
        pipeline = SkillPipeline("ctx_test", [step1, step2])
        result = pipeline.run()
        assert result["results"]["consumer"].output == 42

    def test_create_analysis_pipeline(self):
        from skill_pipeline import create_analysis_pipeline
        pipeline = create_analysis_pipeline()
        assert len(pipeline.steps) == 6


# ============================================================================
# Report Generator Tests
# ============================================================================

class TestGenerateWeeklyReport:
    """周报生成测试"""

    def test_returns_string(self):
        from report_generator import generate_weekly_report
        report = generate_weekly_report()
        assert isinstance(report, str)
        assert len(report) > 0


class TestSaveReport:
    """报告保存测试"""

    def test_save_and_cleanup(self):
        from report_generator import save_report_as_md, REPORT_DIR
        import os
        report = "# 测试报告\n内容"
        path = save_report_as_md(report, "test_type")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "测试报告" in content
        os.unlink(path)


class TestGetRecentReports:
    """获取最近报告测试"""

    def test_returns_list(self):
        from report_generator import get_recent_reports
        reports = get_recent_reports("weekly", 5)
        assert isinstance(reports, list)