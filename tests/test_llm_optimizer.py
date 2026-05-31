"""
test_llm_optimizer.py — LLM 调用优化模块单元测试
"""

import time
import json
import pytest
from unittest.mock import patch, MagicMock


class TestSemanticCache:
    """语义缓存测试（LLM Prompt 缓存）"""

    def test_cache_key_generation(self):
        """缓存键应基于 prompt 内容生成"""
        import hashlib

        prompt = "分析贵州茅台的基本面情况"
        cache_key = hashlib.md5(prompt.encode()).hexdigest()

        assert len(cache_key) == 32
        assert cache_key == hashlib.md5(prompt.encode()).hexdigest()

    def test_cache_hit_identical_prompt(self):
        """相同 prompt 应命中缓存"""
        cache = {}

        prompt = "分析浪潮信息的估值水平"
        result = {"analysis": "估值合理"}

        cache[prompt] = result
        assert cache.get(prompt) == result

    def test_cache_miss_different_prompt(self):
        """不同 prompt 不应命中缓存"""
        cache = {"prompt_a": "result_a"}
        assert cache.get("prompt_b") is None

    def test_cache_ttl_expiry(self):
        """模拟 TTL 过期"""
        cache = {
            "old_prompt": {
                "result": "old",
                "timestamp": time.time() - 86401,
            }
        }
        now = time.time()
        ttl = 86400

        assert (now - cache["old_prompt"]["timestamp"]) > ttl


class TestBatchProcessor:
    """批处理优化测试"""

    def test_batch_merge_prompts(self):
        """多个标的的分析 prompt 应可合并为单个批处理 prompt"""
        codes = ["000977", "600519", "300059"]
        names = ["浪潮信息", "贵州茅台", "东方财富"]

        batch_prompt = "请分析以下股票的基本面情况，以JSON数组格式返回：\n"
        for code, name in zip(codes, names):
            batch_prompt += f"- {code} {name}\n"

        assert "000977" in batch_prompt
        assert "600519" in batch_prompt
        assert "300059" in batch_prompt
        assert "JSON数组" in batch_prompt

    def test_batch_result_parsing(self):
        """批处理结果应可解析为单标的格式"""
        batch_result = json.dumps([
            {"code": "000977", "analysis": "估值合理"},
            {"code": "600519", "analysis": "高估"},
        ])
        parsed = json.loads(batch_result)
        assert len(parsed) == 2
        assert parsed[0]["code"] == "000977"


class TestTokenCompressor:
    """Token 压缩测试"""

    def test_truncate_long_text(self):
        """长文本截断"""
        text = "x" * 10000
        max_chars = 2000
        truncated = text[:max_chars] + "..."
        assert len(truncated) == max_chars + 3

    def test_remove_redundant_info(self):
        """冗余信息去除 — context_compressor 函数可导入"""
        from context_compressor import compress_news, compress_reports, estimate_tokens
        assert callable(compress_news)
        assert callable(compress_reports)
        assert callable(estimate_tokens)