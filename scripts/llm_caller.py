"""
llm_caller.py — DeepSeek API 调用模块
支持 DeepSeek API（主）→ Ollama 本地模型（降级）→ 语义缓存 → 友好错误
所有 chat() 方法返回 dict：{"content": str, "error": str|None}
"""

import os
import json
import logging
import time
import re

import openai
from openai import RateLimitError, APITimeoutError, APIError as OpenAIAPIError
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger("invest_system.llm_caller")

# ── 凭据获取（支持 WCM / 本地文件 / 环境变量）─────────────────────────────
try:
    from credentials import get_credential as _get_cred
    _HAS_CREDENTIALS = True
except ImportError:
    _HAS_CREDENTIALS = False
    _get_cred = None

# ── 语义缓存（减少重复 LLM 调用）────────────────────────────────────────
try:
    from llm_cache import SemanticCache
    _semantic_cache = SemanticCache(max_size=200, ttl_seconds=86400)
    _CACHE_ENABLED = True
except ImportError:
    _semantic_cache = None
    _CACHE_ENABLED = False

# ── LLM 成本追踪 ─────────────────────────────────────────────────────────
try:
    from llm_cost_tracker import record_usage as _record_usage
    _COST_TRACKING_ENABLED = True
except ImportError:
    _record_usage = None
    _COST_TRACKING_ENABLED = False


def _deepseek_api_key() -> str:
    """获取 DeepSeek API Key（优先 credentials 模块，其次环境变量）"""
    if _HAS_CREDENTIALS:
        key = _get_cred("DEEPSEEK_API_KEY")
        if key:
            return key
    return os.environ.get("DEEPSEEK_API_KEY", "")


DEEPSEEK_API_KEY = _deepseek_api_key()
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
LOCAL_MODEL = os.environ.get("LOCAL_MODEL", "qwen2.5:7b")


# ─── DeepSeek API 客户端 ─────────────────────────────────────────────────

class DeepSeekClient:
    def __init__(self):
        # 每次初始化时动态获取凭据（支持 credentials.setup_credentials() 后重新调用）
        api_key = _deepseek_api_key()
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            timeout=60.0,
        )
        self.model = "deepseek-chat"

    def chat(self, prompt: str, system: str = "") -> dict:
        """
        调用 DeepSeek API（支持语义缓存）。
        缓存键：prompt + system 的组合 MD5。
        返回 {"content": str, "error": str|None}
        """
        cache_key = None
        # ── 缓存查询 ───────────────────────────────
        if _CACHE_ENABLED and _semantic_cache is not None:
            cache_key = f"ds:{system}:{prompt}" if system else f"ds::{prompt}"
            cached = _semantic_cache.get(cache_key)
            if cached is not None:
                logger.info("语义缓存命中（DeepSeek），跳过 API 调用")
                return cached

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            return self._call_deepseek_direct(messages, cache_key)

        except RateLimitError as e:
            logger.warning(f"DeepSeek rate limit: {e}, trying Ollama")
            return _call_ollama_fallback(system, prompt)

        except APITimeoutError as e:
            logger.warning(f"DeepSeek timeout: {e}, trying Ollama")
            return _call_ollama_fallback(system, prompt)

        except OpenAIAPIError as e:
            err_str = str(e).lower()
            if "context" in err_str or "maximum" in err_str or "length" in err_str:
                logger.warning(f"DeepSeek context length error: {e}, trying compressed prompt")
                return _call_with_compressed_context(system, prompt)
            logger.error(f"DeepSeek API error: {e}")
            return _call_ollama_fallback(system, prompt)

        except Exception as e:
            logger.error(f"DeepSeek API 调用失败: {e}")
            return _call_ollama_fallback(system, prompt)

    def _call_deepseek_direct(self, messages: list[dict], cache_key: str | None = None) -> dict:
        """直接调用 DeepSeek API（不含缓存/降级逻辑，供内部和压缩重试用）"""
        logger.info(f"调用 DeepSeek API，Prompt 长度: {sum(len(m.get('content','')) for m in messages)} chars")
        start = time.time()

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.3,
            max_tokens=2000,
        )

        elapsed = time.time() - start
        content = response.choices[0].message.content.strip()
        usage = response.usage

        logger.info(f"DeepSeek 响应: {len(content)} chars, "
                    f"耗时 {elapsed:.1f}s, "
                    f"输入 {usage.prompt_tokens} tokens, "
                    f"输出 {usage.completion_tokens} tokens")

        # ── 成本追踪 ───────────────────────────
        if _COST_TRACKING_ENABLED and _record_usage is not None:
            _record_usage(
                model=self.model,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
            )

        result = {"content": content, "error": None}

        # ── 缓存写入 ───────────────────────────
        if _CACHE_ENABLED and _semantic_cache is not None and not result.get("error") and cache_key:
            _semantic_cache.set(cache_key, result)
            logger.info("语义缓存已写入（DeepSeek）")

        return result


# ── LLM 降级辅助函数 ──────────────────────────────────────────────────────

def _call_ollama_fallback(system: str, prompt: str) -> dict:
    """Ollama 降级调用 — 失败后尝试缓存或返回友好错误"""
    try:
        ollama = OllamaClient()
        if not ollama.is_available():
            logger.warning("Ollama 不可用，尝试语义缓存")
            return _fallback_to_cached_or_error(system, prompt)
        return ollama.chat(prompt, system)
    except Exception as e:
        logger.error(f"Ollama 调用也失败: {e}")
        return _fallback_to_cached_or_error(system, prompt)


def _fallback_to_cached_or_error(system: str, prompt: str) -> dict:
    """缓存命中则返回缓存，否则返回友好错误"""
    if _CACHE_ENABLED and _semantic_cache is not None:
        cache_key = f"ds:{system}:{prompt}" if system else f"ds::{prompt}"
        cached = _semantic_cache.get(cache_key)
        if cached:
            logger.warning("LLM API 失败，使用缓存响应（降级）")
            cached["content"] = f"[缓存回复]\n{cached['content']}"
            return cached
    return {"content": "暂时无法完成分析，请稍后重试。", "error": "LLM API 不可用"}


# ── Hermes LLM 4级降级链 (v2.1 补丁7 集成) ──────────────────────────────────
# 触发场景: DeepSeek 超时 + Ollama 不可用 → 用规则引擎生成应急回复
# 降级链路: L1 Hermes(本文件DeepSeek+Ollama) → L2 直连API → L3 规则引擎 → L4 跳过
# 集成位置: _call_ollama_fallback 失败后, _fallback_to_cached_or_error 之前
import sys as _sys_llm
_HERMES_SCRIPTS = Path(__file__).parent.parent / "hermes_coordination" / "scripts"
_sys_llm.path.insert(0, str(_HERMES_SCRIPTS))
try:
    from llm_fallback_chain import LLMFallbackChain  # noqa: E402
    _FALLBACK_CHAIN = LLMFallbackChain(
        hermes_router=None,  # 暂时只 L3 规则引擎路径可用
        direct_caller=None,
    )
    _FALLBACK_CHAIN_AVAILABLE = True
    logger.info("LLMFallbackChain 已加载 (v2.1 补丁7)")
except Exception as _e_fb:
    _FALLBACK_CHAIN = None
    _FALLBACK_CHAIN_AVAILABLE = False
    logger.warning(f"LLMFallbackChain 加载失败, 降级链路退化: {_e_fb}")


def _call_fallback_chain(system: str, prompt: str) -> dict:
    """
    调用 LLMFallbackChain 的最终降级路径（L3 规则引擎）
    用于 DeepSeek + Ollama 都失败时，给出应急回复
    """
    if not _FALLBACK_CHAIN_AVAILABLE or _FALLBACK_CHAIN is None:
        return _fallback_to_cached_or_error(system, prompt)

    try:
        # 用环境变量启用 mock 模式（避免 L1 触发实际 API 调用）
        os.environ.setdefault("HERMES_FALLBACK_MOCK", "1")
        result = _FALLBACK_CHAIN.call(prompt, system=system, max_retries=1)
        content = result.get("content", "")
        level = result.get("level", "unknown")
        # 规则引擎成功 → 返回应急分析
        if content and not result.get("error"):
            return {
                "content": f"[应急降级回复 L3/{level}]\n{content}",
                "error": None,
            }
    except Exception as e:
        logger.error(f"LLMFallbackChain 调用失败: {e}")

    # 最终回退
    return _fallback_to_cached_or_error(system, prompt)


def _call_with_compressed_context(system: str, prompt: str) -> dict:
    """上下文超限时，截断 prompt 重试（最后降级手段）"""
    try:
        # 简单截断策略：保留前 3000 字符
        MAX_CHARS = 3000
        truncated_prompt = prompt[:MAX_CHARS] + "\n[...内容已截断...]"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": truncated_prompt})
        # 直接重试 API 调用（不做缓存）
        client = DeepSeekClient()
        return client._call_deepseek_direct(messages)
    except Exception as e:
        logger.error(f"压缩重试失败: {e}")
        return _call_ollama_fallback(system, prompt)


# ─── Ollama 本地模型（降级）───────────────────────────────────────────────

class OllamaClient:
    def __init__(self):
        self.base_url = OLLAMA_BASE_URL
        self.model = LOCAL_MODEL

    def is_available(self) -> bool:
        try:
            import requests
            resp = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def chat(self, prompt: str, system: str = "") -> dict:
        """
        调用 Ollama 本地模型。
        返回 {"content": str, "error": str|None}
        """
        try:
            import requests
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            logger.info(f"调用 Ollama {self.model}")
            start = time.time()

            resp = requests.post(
                f"{self.base_url}/api/chat",
                json={"model": self.model, "messages": messages, "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
            elapsed = time.time() - start

            content = resp.json()["message"]["content"].strip()
            logger.info(f"Ollama 响应: {len(content)} chars, 耗时 {elapsed:.1f}s")

            return {"content": content, "error": None}

        except Exception as e:
            logger.error(f"Ollama 调用失败: {e}")
            return {"content": "", "error": str(e)}


# ─── 成本估算 ─────────────────────────────────────────────────────────────

def estimate_cost(prompt: str, model: str = "deepseek-chat") -> dict:
    input_price = 0.27   # $ / M tokens
    output_price = 1.10   # $ / M tokens
    input_tokens = len(prompt) // 4
    output_tokens = 1500
    total_cost = input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price
    return {
        "input_tokens_estimate": input_tokens,
        "output_tokens_estimate": output_tokens,
        "estimated_cost_usd": round(total_cost, 6),
        "estimated_cost_cny": round(total_cost * 7.2, 4),
    }


# ─── 工厂函数 ─────────────────────────────────────────────────────────────

def _parse_llm_response(content: str) -> dict:
    """
    尝试从 LLM 响应中解析 JSON。
    返回 {"plans": [], "risks": [], "market_outlook": "", "confidence_level": ""}
    如果不是 JSON 格式，返回合理的降级结果。
    """
    # 尝试直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 尝试从 markdown 代码块中提取
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试找到最后一个 {...} 块
    first = content.find("{")
    last = content.rfind("}")
    if first != -1 and last != -1 and last > first:
        try:
            return json.loads(content[first:last + 1])
        except json.JSONDecodeError:
            pass
    # 降级：返回纯文本作为 market_outlook
    return {
        "plans": [],
        "risks": [],
        "market_outlook": content[:500],
        "confidence_level": "low",
        "_raw_text": content,
    }


def get_llm_client():
    """
    获取 LLM 客户端（优先 DeepSeek，降级 Ollama）
    返回稳定的客户端实例
    """
    api_key = _deepseek_api_key()
    if api_key and not api_key.startswith("***"):
        return DeepSeekClient()
    elif OllamaClient().is_available():
        logger.warning("DeepSeek API Key 未配置，降级到 Ollama")
        return OllamaClient()
    else:
        logger.error("既无 DeepSeek API 也无 Ollama")
        return DeepSeekClient()  # 会失败但有客户端


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = get_llm_client()
    print(f"客户端: {type(client).__name__}")

    result = client.chat("用一句话介绍自己")
    print(f"内容: {result['content'][:100]}")
    print(f"错误: {result['error']}")
