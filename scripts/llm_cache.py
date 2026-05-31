"""
llm_cache.py — LLM 调用性能优化模块

提供:
  - SemanticCache: 基于 prompt 相似度的语义缓存，减少重复 API 调用
  - BatchProcessor: 多标的分析请求批处理，减少 API 调用次数
"""

import time
import hashlib
import json
import logging
from typing import Optional, Any
from collections import OrderedDict

logger = logging.getLogger("llm_cache")


class SemanticCache:
    """
    语义缓存：对相似的 prompt 进行缓存，避免重复调用 LLM API。

    缓存策略:
      - 精确匹配: 相同 prompt 直接返回缓存结果
      - TTL: 默认 24 小时（行情数据日级变化）
      - LRU 淘汰: 最多保留 100 条缓存
    """

    def __init__(self, max_size: int = 100, ttl_seconds: int = 86400):
        self._cache = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    def _key(self, prompt: str) -> str:
        """生成缓存键"""
        return hashlib.md5(prompt.encode("utf-8")).hexdigest()

    def get(self, prompt: str) -> Optional[Any]:
        """
        查询缓存。

        Returns:
            缓存的 LLM 响应，如果未命中或已过期则返回 None
        """
        key = self._key(prompt)
        if key not in self._cache:
            self._misses += 1
            return None

        entry = self._cache[key]
        if time.time() - entry["timestamp"] > self._ttl:
            del self._cache[key]
            self._misses += 1
            return None

        self._cache.move_to_end(key)
        self._hits += 1
        return entry["result"]

    def set(self, prompt: str, result: Any):
        """
        写入缓存。
        若缓存已满，淘汰最久未使用的条目。
        """
        key = self._key(prompt)
        self._cache[key] = {
            "result": result,
            "timestamp": time.time(),
        }
        self._cache.move_to_end(key)

        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def clear(self):
        """清空缓存"""
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    @property
    def hit_rate(self) -> float:
        """缓存命中率"""
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return self._hits / total

    @property
    def stats(self) -> dict:
        """缓存统计"""
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 3),
            "ttl_seconds": self._ttl,
        }


class BatchProcessor:
    """
    批处理器：将多个标的的分析请求合并为单次 LLM 调用。
    适用于盘后批量分析场景。

    使用方式:
        processor = BatchProcessor()
        results = processor.batch_analyze(
            codes=["000977", "600519", "300059"],
            names=["浪潮信息", "贵州茅台", "东方财富"],
            llm_caller=call_llm
        )
    """

    def build_batch_prompt(
        self,
        codes: list[str],
        names: list[str],
        analysis_type: str = "基本面",
    ) -> str:
        """
        构建批处理 prompt。

        Args:
            codes: 标的代码列表
            names: 标的名称列表
            analysis_type: 分析类型（基本面/技术面/消息面）
        """
        lines = [
            f"请对以下{len(codes)}只股票进行{analysis_type}分析，以JSON数组格式返回。",
            "每只股票返回一个JSON对象，包含以下字段：",
            '  - "code": 股票代码',
            '  - "analysis": 分析结论（50字以内）',
            '  - "rating": 评级（1-5星）',
            '  - "key_point": 关键要点',
            "",
            "股票列表：",
        ]
        for code, name in zip(codes, names):
            lines.append(f"  - {code} {name}")
        lines.append("")
        lines.append("请仅返回JSON数组，不要包含其他文字。")

        return "\n".join(lines)

    def parse_batch_response(self, response: str) -> dict[str, dict]:
        """
        解析 LLM 批处理响应。

        Returns:
            {code: {analysis, rating, key_point}, ...} 映射
        """
        try:
            text = response.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            items = json.loads(text)
            return {item["code"]: item for item in items}
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.warning(f"批处理响应解析失败: {e}")
            return {}


# 全局单例（进程内共享）
_global_cache = SemanticCache()


def get_cache() -> SemanticCache:
    """获取全局缓存实例"""
    return _global_cache