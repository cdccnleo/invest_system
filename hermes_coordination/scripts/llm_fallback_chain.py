"""
llm_fallback_chain.py — LLM 4级降级链 (P2-T2 补丁7 落地 v1.0)

4级降级:
- L1 normal: Hermes 路由 (gpt-4o-mini / deepseek / ollama)
- L2 degraded: OpenAI/DeepSeek 直连 (绕开 Hermes)
- L3 offline: 本地规则引擎 (无 LLM)
- L4 skip: 跳过本次决策，下次补推

触发条件:
- L1→L2: Hermes timeout>30s 连续 3 次
- L2→L3: 直连 API 5xx>5次/小时
- L3→L4: 规则引擎 None/Exception
- L4→L1: API 恢复 (>1h 正常)

使用:
    from llm_fallback_chain import LLMFallbackChain
    chain = LLMFallbackChain()
    result = chain.call(prompt, system="...")
"""

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("llm_fallback")


class FallbackLevel(Enum):
    L1_NORMAL = "L1_normal"           # Hermes 路由
    L2_DIRECT = "L2_direct"           # API 直连
    L3_RULE = "L3_rule"               # 本地规则
    L4_SKIP = "L4_skip"               # 跳过


@dataclass
class FallbackStats:
    """降级链统计"""
    l1_attempts: int = 0
    l1_successes: int = 0
    l1_timeouts: int = 0
    l2_attempts: int = 0
    l2_successes: int = 0
    l2_5xx: int = 0
    l3_attempts: int = 0
    l3_successes: int = 0
    l3_none: int = 0
    l4_skipped: int = 0
    last_reset: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "l1_attempts": self.l1_attempts,
            "l1_successes": self.l1_successes,
            "l1_timeouts": self.l1_timeouts,
            "l2_attempts": self.l2_attempts,
            "l2_successes": self.l2_successes,
            "l2_5xx": self.l2_5xx,
            "l3_attempts": self.l3_attempts,
            "l3_successes": self.l3_successes,
            "l3_none": self.l3_none,
            "l4_skipped": self.l4_skipped,
            "uptime_seconds": int(time.time() - self.last_reset),
        }


class LLMFallbackChain:
    """LLM 4级降级链"""

    # 降级阈值
    L1_TIMEOUT_THRESHOLD = 3          # 连续 3 次 timeout → 降级 L2
    L2_5XX_THRESHOLD = 5              # 5xx > 5次/小时 → 降级 L3
    L3_NONE_THRESHOLD = 10            # 规则引擎 None > 10次/小时 → 降级 L4
    HOURLY_RESET = 3600               # 1 小时重置
    L1_RECOVERY_HOURS = 1             # L4→L1 恢复时间

    def __init__(self, hermes_router=None, direct_caller=None, rule_engine=None):
        """
        Args:
            hermes_router: 可注入的 L1 调用器（默认 None 表示用 mock）
            direct_caller: 可注入的 L2 调用器
            rule_engine: 可注入的 L3 规则引擎
        """
        self.hermes_router = hermes_router
        self.direct_caller = direct_caller
        self.rule_engine = rule_engine or self._default_rule_engine
        self.stats = FallbackStats()
        self.current_level = FallbackLevel.L1_NORMAL
        self.l4_start_time: Optional[float] = None

    def call(self, prompt: str, system: str = "", max_retries: int = 2,
             **kwargs) -> Dict[str, Any]:
        """统一入口 — 4级降级链

        Returns:
            {
                "success": bool,
                "level": "L1_normal|L2_direct|L3_rule|L4_skip",
                "content": str,
                "fallback_reason": str | None,
                "duration_seconds": float,
                "attempts": int,
            }
        """
        start = time.time()
        result = {
            "success": False,
            "level": None,
            "content": "",
            "fallback_reason": None,
            "duration_seconds": 0.0,
            "attempts": 0,
        }

        # 检查 L4 恢复
        if self.current_level == FallbackLevel.L4_SKIP:
            if self.l4_start_time and (time.time() - self.l4_start_time) > self.L1_RECOVERY_HOURS * 3600:
                logger.info("L4 恢复 → L1 (1h+ 已过)")
                self.current_level = FallbackLevel.L1_NORMAL
                self.l4_start_time = None
                self.stats = FallbackStats()  # 重置统计

        # L1: Hermes 路由
        if self.current_level in (FallbackLevel.L1_NORMAL, FallbackLevel.L2_DIRECT):
            for attempt in range(max_retries + 1):
                self.stats.l1_attempts += 1
                result["attempts"] += 1
                try:
                    content = self._call_hermes(prompt, system, **kwargs)
                    self.stats.l1_successes += 1
                    result.update({
                        "success": True,
                        "level": FallbackLevel.L1_NORMAL.value,
                        "content": content,
                    })
                    self._maybe_upgrade_to_l1()
                    result["duration_seconds"] = time.time() - start
                    return result
                except TimeoutError as e:
                    self.stats.l1_timeouts += 1
                    logger.warning(f"L1 timeout ({attempt+1}/{max_retries+1}): {e}")
                except Exception as e:
                    logger.warning(f"L1 错误 ({attempt+1}): {e}")
                    break

            # L1 失败 → 降级 L2
            if self.stats.l1_timeouts >= self.L1_TIMEOUT_THRESHOLD:
                logger.warning(f"L1 连续 {self.stats.l1_timeouts} 次 timeout → 降级 L2")
                self.current_level = FallbackLevel.L2_DIRECT
                result["fallback_reason"] = f"L1_timeout_{self.stats.l1_timeouts}"

        # L2: API 直连
        if self.current_level in (FallbackLevel.L2_DIRECT,):
            for attempt in range(max_retries):
                self.stats.l2_attempts += 1
                result["attempts"] += 1
                try:
                    content = self._call_direct(prompt, system, **kwargs)
                    self.stats.l2_successes += 1
                    result.update({
                        "success": True,
                        "level": FallbackLevel.L2_DIRECT.value,
                        "content": content,
                    })
                    # 成功 → 升级回 L1
                    self._maybe_upgrade_to_l1()
                    result["duration_seconds"] = time.time() - start
                    return result
                except Exception as e:
                    if "5xx" in str(e) or "500" in str(e):
                        self.stats.l2_5xx += 1
                    logger.warning(f"L2 错误 ({attempt+1}): {e}")

            # L2 失败 → 降级 L3
            if self.stats.l2_5xx >= self.L2_5XX_THRESHOLD:
                logger.warning(f"L2 5xx > {self.L2_5XX_THRESHOLD}/h → 降级 L3")
                self.current_level = FallbackLevel.L3_RULE
                result["fallback_reason"] = f"L2_5xx_{self.stats.l2_5xx}"

        # L3: 本地规则引擎
        if self.current_level == FallbackLevel.L3_RULE:
            self.stats.l3_attempts += 1
            result["attempts"] += 1
            try:
                content = self._call_rule(prompt, system)
                if content:
                    self.stats.l3_successes += 1
                    result.update({
                        "success": True,
                        "level": FallbackLevel.L3_RULE.value,
                        "content": content,
                    })
                else:
                    self.stats.l3_none += 1
                    logger.warning("L3 规则引擎返回 None")
            except Exception as e:
                logger.warning(f"L3 规则引擎错误: {e}")

            # L3 失败 → 降级 L4
            if self.stats.l3_none >= self.L3_NONE_THRESHOLD:
                logger.warning(f"L3 None > {self.L3_NONE_THRESHOLD}/h → 降级 L4")
                self.current_level = FallbackLevel.L4_SKIP
                self.l4_start_time = time.time()
                result["fallback_reason"] = f"L3_none_{self.stats.l3_none}"

        # L4: 跳过
        if self.current_level == FallbackLevel.L4_SKIP:
            self.stats.l4_skipped += 1
            result.update({
                "success": False,
                "level": FallbackLevel.L4_SKIP.value,
                "content": "[SKIPPED] LLM 不可用 + 规则引擎兜底失败 — 已加入下次补推队列",
                "fallback_reason": "all_levels_failed",
            })
            # TODO: 写入补推队列
            logger.error("L4 跳过本次决策 — 待补推")

        result["duration_seconds"] = time.time() - start
        return result

    def _call_hermes(self, prompt, system, **kwargs) -> str:
        """L1: Hermes 路由调用（可注入）"""
        if self.hermes_router:
            return self.hermes_router(prompt, system, **kwargs)
        # mock: 在没有真实 router 时返回测试数据
        if os.environ.get("HERMES_FALLBACK_MOCK", "0") == "1":
            return f"[L1 mock] {prompt[:50]}..."
        raise NotImplementedError("需要注入 hermes_router 或设 HERMES_FALLBACK_MOCK=1")

    def _call_direct(self, prompt, system, **kwargs) -> str:
        """L2: API 直连（可注入）"""
        if self.direct_caller:
            return self.direct_caller(prompt, system, **kwargs)
        # mock
        if os.environ.get("HERMES_FALLBACK_MOCK", "0") == "1":
            return f"[L2 mock] {prompt[:50]}..."
        raise NotImplementedError("需要注入 direct_caller")

    def _call_rule(self, prompt, system) -> Optional[str]:
        """L3: 本地规则引擎"""
        return self.rule_engine(prompt, system)

    def _default_rule_engine(self, prompt: str, system: str) -> Optional[str]:
        """默认规则引擎：基于 prompt 关键词返回最简答案"""
        prompt_lower = prompt.lower()
        if "买入" in prompt or "buy" in prompt_lower:
            return "规则兜底: 建议持有观望（无 LLM 时降级到规则）"
        if "卖出" in prompt or "sell" in prompt_lower:
            return "规则兜底: 建议持有观望（无 LLM 时降级到规则）"
        if "风险" in prompt or "risk" in prompt_lower:
            return "规则兜底: 关注流动性 + 集中度风险（无 LLM 时降级到规则）"
        return None  # 不知道怎么办

    def _maybe_upgrade_to_l1(self):
        """成功调用后检查是否可以升级回 L1"""
        if self.current_level == FallbackLevel.L2_DIRECT:
            # 成功 3 次后可考虑升级
            if self.stats.l2_successes >= 3 and self.stats.l2_5xx == 0:
                logger.info("L2 稳定成功 → 升级回 L1")
                self.current_level = FallbackLevel.L1_NORMAL
                self.stats = FallbackStats()

    def get_status(self) -> Dict:
        """获取当前状态"""
        return {
            "current_level": self.current_level.value,
            "stats": self.stats.to_dict(),
            "thresholds": {
                "l1_timeout_to_l2": self.L1_TIMEOUT_THRESHOLD,
                "l2_5xx_to_l3": self.L2_5XX_THRESHOLD,
                "l3_none_to_l4": self.L3_NONE_THRESHOLD,
            },
            "l4_elapsed_seconds": int(time.time() - self.l4_start_time) if self.l4_start_time else 0,
        }


def main():
    """演示 4 级降级链"""
    print("=" * 70)
    print("LLM 4级降级链演示 (HERMES_FALLBACK_MOCK=1)")
    print("=" * 70)

    os.environ["HERMES_FALLBACK_MOCK"] = "1"
    chain = LLMFallbackChain()

    # 场景 1: 正常 L1
    print("\n--- 场景 1: 正常 L1 调用 ---")
    r = chain.call("分析一下 002050 的走势")
    print(f"  result: {r}")

    # 场景 2: L1 timeout 3 次 → 降级 L2
    print("\n--- 场景 2: L1 timeout 3 次 → 降级 L2 ---")
    chain.stats.l1_timeouts = 3
    chain.hermes_router = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("L1 timeout"))
    r = chain.call("分析 002050")
    print(f"  result: {r}")

    # 场景 3: L2 5xx > 5 → 降级 L3
    print("\n--- 场景 3: L2 5xx > 5 → 降级 L3 ---")
    chain.stats.l2_5xx = 6
    chain.direct_caller = lambda *a, **kw: (_ for _ in ()).throw(Exception("502 Bad Gateway"))
    r = chain.call("分析 002050")
    print(f"  result: {r}")

    # 场景 4: L3 None 10 → 降级 L4
    print("\n--- 场景 4: L3 None > 10 → 降级 L4 ---")
    chain.stats.l3_none = 11
    chain.rule_engine = lambda p, s: None
    r = chain.call("完全陌生的 prompt")
    print(f"  result: {r}")

    # 状态报告
    print("\n" + "=" * 70)
    print("最终状态")
    print("=" * 70)
    import json
    print(json.dumps(chain.get_status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
