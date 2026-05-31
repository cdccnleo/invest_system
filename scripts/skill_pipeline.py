"""
skill_pipeline.py — 跨技能流水线编排模块
支持将多个技能串联为可执行流水线，支持条件分支、并行执行、错误处理
"""

import logging
import json
import time
from enum import Enum
from typing import Callable, Optional
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("invest_system.skill_pipeline")


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PipelineStep:
    """流水线步骤定义"""
    name: str
    func: Callable
    depends_on: list[str] = field(default_factory=list)
    condition: Optional[Callable] = None
    on_error: str = "stop"  # stop / continue / skip
    retry: int = 0
    timeout: int = 300
    params: dict = field(default_factory=dict)


@dataclass
class StepResult:
    """步骤执行结果"""
    name: str
    status: StepStatus
    output: any = None
    error: Optional[str] = None
    duration_ms: float = 0
    retries: int = 0


class SkillPipeline:
    """
    技能流水线编排器
    支持 DAG 依赖拓扑排序、条件分支执行、并行执行

    Args:
        name: 流水线名称
        steps: 步骤列表
        max_workers: 并行执行线程数
    """

    def __init__(self, name: str, steps: list[PipelineStep], max_workers: int = 4):
        self.name = name
        self.steps = steps
        self.max_workers = max_workers
        self._results: dict[str, StepResult] = {}
        self._context: dict = {}

    def _topological_sort(self) -> list[list[PipelineStep]]:
        """
        拓扑排序：返回按层级分组的步骤列表
        同一层级的步骤可以并行执行
        """
        step_map = {s.name: s for s in self.steps}
        in_degree = {s.name: len(s.depends_on) for s in self.steps}
        levels = []

        while in_degree:
            ready = [name for name, deg in in_degree.items() if deg == 0]
            if not ready:
                raise ValueError(f"流水线存在循环依赖: {list(in_degree.keys())}")

            level_steps = [step_map[name] for name in ready]
            levels.append(level_steps)

            for name in ready:
                del in_degree[name]
                for s in self.steps:
                    if name in s.depends_on:
                        in_degree[s.name] -= 1

        return levels

    def _execute_step(self, step: PipelineStep) -> StepResult:
        """执行单个步骤"""
        start = time.time()
        result = StepResult(name=step.name, status=StepStatus.RUNNING)

        # 检查条件
        if step.condition and not step.condition(self._context):
            result.status = StepStatus.SKIPPED
            result.duration_ms = (time.time() - start) * 1000
            return result

        # 收集依赖步骤的输出
        deps_output = {}
        for dep_name in step.depends_on:
            if dep_name in self._results:
                deps_output[dep_name] = self._results[dep_name].output

        # 执行（含重试）
        attempts = 0
        last_error = None
        while attempts <= step.retry:
            try:
                output = step.func(
                    context=self._context,
                    deps=deps_output,
                    **step.params,
                )
                result.output = output
                result.status = StepStatus.COMPLETED
                result.retries = attempts
                result.duration_ms = (time.time() - start) * 1000
                self._context[step.name] = output
                return result
            except Exception as e:
                last_error = str(e)
                attempts += 1
                logger.warning(f"步骤 {step.name} 执行失败 (尝试 {attempts}/{step.retry + 1}): {e}")

        result.status = StepStatus.FAILED
        result.error = last_error
        result.retries = attempts
        result.duration_ms = (time.time() - start) * 1000
        return result

    def run(self) -> dict:
        """
        执行流水线

        Returns:
            {
                "name": str,
                "status": "completed"/"partial"/"failed",
                "results": {step_name: StepResult},
                "total_duration_ms": float,
            }
        """
        start = time.time()
        levels = self._topological_sort()
        logger.info(f"流水线 [{self.name}] 开始: {len(levels)} 层, {len(self.steps)} 步骤")

        for level_idx, level_steps in enumerate(levels):
            logger.info(f"  执行第 {level_idx + 1}/{len(levels)} 层: {[s.name for s in level_steps]}")

            # 并行执行同层步骤
            if len(level_steps) > 1:
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {executor.submit(self._execute_step, s): s for s in level_steps}
                    for future in as_completed(futures):
                        step = futures[future]
                        try:
                            result = future.result(timeout=step.timeout)
                            self._results[step.name] = result
                        except Exception as e:
                            self._results[step.name] = StepResult(
                                name=step.name, status=StepStatus.FAILED, error=str(e)
                            )
            else:
                step = level_steps[0]
                result = self._execute_step(step)
                self._results[step.name] = result

            # 检查是否有失败步骤需要停止
            for step in level_steps:
                r = self._results[step.name]
                if r.status == StepStatus.FAILED and step.on_error == "stop":
                    logger.error(f"流水线 [{self.name}] 在步骤 {step.name} 失败，停止执行")
                    total_duration = (time.time() - start) * 1000
                    return {
                        "name": self.name,
                        "status": "failed",
                        "results": self._results,
                        "total_duration_ms": round(total_duration, 1),
                        "failed_at": step.name,
                        "error": r.error,
                    }

        total_duration = (time.time() - start) * 1000
        all_completed = all(r.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
                          for r in self._results.values())
        status = "completed" if all_completed else "partial"

        logger.info(f"流水线 [{self.name}] 完成: {status}, 耗时 {total_duration:.0f}ms")
        return {
            "name": self.name,
            "status": status,
            "results": self._results,
            "total_duration_ms": round(total_duration, 1),
        }

    def get_context(self) -> dict:
        """获取流水线上下文"""
        return dict(self._context)


def create_analysis_pipeline() -> SkillPipeline:
    """
    创建标准分析流水线
    数据采集 → 数据校验 → 因子评分 → LLM分析 → 质量评估 → TAMF更新
    """
    steps = [
        PipelineStep(
            name="fetch_data",
            func=lambda context, deps, **kwargs: {"status": "ok", "source": "akshare"},
            params={},
        ),
        PipelineStep(
            name="validate_data",
            func=lambda context, deps, **kwargs: {"valid": True, "records": 100},
            depends_on=["fetch_data"],
            on_error="continue",
        ),
        PipelineStep(
            name="factor_analysis",
            func=lambda context, deps, **kwargs: {"scores": {"value": 3.0, "quality": 4.0}},
            depends_on=["validate_data"],
            condition=lambda ctx: ctx.get("validate_data", {}).get("valid", False),
        ),
        PipelineStep(
            name="llm_analysis",
            func=lambda context, deps, **kwargs: {"result": "分析完成", "confidence": "high"},
            depends_on=["factor_analysis"],
            retry=1,
        ),
        PipelineStep(
            name="quality_assessment",
            func=lambda context, deps, **kwargs: {"score": 85, "level": "high"},
            depends_on=["llm_analysis"],
        ),
        PipelineStep(
            name="tamf_update",
            func=lambda context, deps, **kwargs: {"updated": True},
            depends_on=["quality_assessment"],
        ),
    ]
    return SkillPipeline("standard_analysis", steps)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    pipeline = create_analysis_pipeline()
    result = pipeline.run()
    print(f"\n流水线状态: {result['status']}")
    for name, r in result["results"].items():
        icon = "✅" if r.status == StepStatus.COMPLETED else "❌" if r.status == StepStatus.FAILED else "⏭️"
        print(f"  {icon} {name}: {r.status.value} ({r.duration_ms:.0f}ms)")
        if r.error:
            print(f"      错误: {r.error}")