"""
llm_caller.py — DeepSeek API 调用模块
支持 DeepSeek API（主）→ Ollama 本地模型（降级）
所有 chat() 方法返回 dict：{"content": str, "error": str|None}
"""

import os
import json
import logging
import time
import re
from typing import Optional

import openai
from dotenv import load_dotenv

load_dotenv("/home/aileo/invest_system/.env")

logger = logging.getLogger("invest_system.llm_caller")

# ── 凭据获取（支持 WCM / 本地文件 / 环境变量）─────────────────────────────
try:
    from credentials import get_credential as _get_cred
    _HAS_CREDENTIALS = True
except ImportError:
    _HAS_CREDENTIALS = False
    _get_cred = None


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
        调用 DeepSeek API。
        返回 {"content": str, "error": str|None}
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            logger.info(f"调用 DeepSeek API，Prompt 长度: {len(prompt)} chars")
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

            return {"content": content, "error": None}

        except Exception as e:
            logger.error(f"DeepSeek API 调用失败: {e}")
            return {"content": "", "error": str(e)}


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
    import json, re
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
