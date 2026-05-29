"""
agent_interface.py — Agent 抽象接口层
隔离对 Hermes Agent / LLM API 的直接依赖，支持降级
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional

from dotenv import load_dotenv
import os as _os
load_dotenv(_os.path.join(_os.path.dirname(__file__), "..", ".env"))
import sys; sys.path.insert(0, "/home/aileo/invest_system/scripts")

logger = logging.getLogger("invest_system.agent_interface")


# ── 接口定义 ──────────────────────────────────────────────────────────────

class AgentInterface(ABC):
    """Agent 抽象接口，隔离具体实现"""

    @abstractmethod
    def chat(self, prompt: str, system: str = None, model: str = None) -> dict:
        """发送对话请求并获取响应"""
        pass

    @abstractmethod
    def search_memory(self, query: str, top_k: int = 5) -> list[dict]:
        """语义搜索记忆库"""
        pass

    @abstractmethod
    def run_skill(self, skill_name: str, **params) -> dict:
        """执行已固化的技能"""
        pass

    def health_check(self) -> bool:
        """健康检查"""
        try:
            result = self.chat("ping", system="回答pong")
            return result.get("error") is None
        except Exception:
            return False


# ── DeepSeek 实现 ─────────────────────────────────────────────────────────

class DeepSeekAgent(AgentInterface):
    """DeepSeek API 实现"""

    def __init__(self, api_key: str = None, base_url: str = None):
        import os
        from llm_caller import get_llm_client
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self._client = get_llm_client()

    def chat(self, prompt: str, system: str = None, model: str = "deepseek-chat") -> dict:
        try:
            result = self._client.chat(prompt, system=system or "你是一名专业量化投资顾问。")
            return {"content": result.get("content", ""), "error": result.get("error")}
        except Exception as e:
            logger.error(f"DeepSeek 调用失败: {e}")
            return {"content": "", "error": str(e)}

    def search_memory(self, query: str, top_k: int = 5) -> list[dict]:
        from embedding_service import search_similar_news
        return search_similar_news(query, top_k=top_k)

    def run_skill(self, skill_name: str, **params) -> dict:
        """通过 skills/ 目录执行固化技能"""
        from pathlib import Path
        skill_file = Path(__file__).parent.parent / "skills" / f"{skill_name}.md"
        if not skill_file.exists():
            return {"error": f"技能不存在: {skill_name}"}

        content = skill_file.read_text(encoding="utf-8")
        # 简单 skill 执行：把内容作为 system prompt 注入
        system_prompt = content[:2000]  # 截断避免超长
        return self.chat(params.get("query", ""), system=system_prompt)


# ── Ollama 本地实现 ──────────────────────────────────────────────────────

class OllamaAgent(AgentInterface):
    """Ollama 本地模型实现"""

    def __init__(self, base_url: str = None, model: str = None):
        import os
        self.base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        self.model = model or os.environ.get("LOCAL_MODEL", "gemma4:e4b")

    def chat(self, prompt: str, system: str = None, model: str = None) -> dict:
        import json, urllib.request, urllib.error

        payload = {
            "model": model or self.model,
            "messages": [
                *( [{"role": "system", "content": system}] if system else [] ),
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }

        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
                content = result.get("message", {}).get("content", "")
                return {"content": content, "error": None}
        except Exception as e:
            logger.warning(f"Ollama 调用失败: {e}")
            return {"content": "", "error": str(e)}

    def search_memory(self, query: str, top_k: int = 5) -> list[dict]:
        # Ollama 本地模型暂时不支持向量检索，降级到关键词搜索
        from storage_factory import get_storage
        storage = get_storage()
        try:
            news = storage.get_recent_news(limit=top_k)
            # 简单关键词匹配
            keywords = query.lower().split()
            scored = []
            for n in news:
                title = n.get("title", "").lower()
                score = sum(1 for k in keywords if k in title)
                if score > 0:
                    scored.append({**n, "score": score})
            return sorted(scored, key=lambda x: x["score"], reverse=True)[:top_k]
        finally:
            storage.close()

    def run_skill(self, skill_name: str, **params) -> dict:
        return {"error": f"Ollama 模式不支持运行技能: {skill_name}"}


# ── 路由代理实现 ──────────────────────────────────────────────────────────

class RouterAgent(AgentInterface):
    """
    路由代理：根据任务类型自动选择 DeepSeek 或 Ollama
    这是默认使用的实现
    """

    def __init__(self):
        from model_router import route
        self._route = route
        self._deepseek = DeepSeekAgent()
        self._ollama = OllamaAgent()
        # 预加载已批准技能的触发关键词
        self._approved_skills: list[dict] = []
        self._reload_skills()

    def _reload_skills(self):
        """重新扫描已批准技能，建立触发匹配表"""
        from skill_library import APPROVED_DIR
        import re, json
        self._approved_skills = []
        for f in APPROVED_DIR.glob("*.md"):
            text = f.read_text(encoding="utf-8")
            kw_match = re.search(r"## 触发关键词\n```regex\n(.*?)\n```", text, re.DOTALL)
            name_match = re.search(r"^# (.+)$", text, re.MULTILINE)
            if kw_match and name_match:
                self._approved_skills.append({
                    "name": name_match.group(1).strip(),
                    "keywords": kw_match.group(1).strip(),
                })

    def _match_skill(self, prompt: str) -> str | None:
        """匹配 prompt 是否命中某个已批准技能的触发词"""
        import re
        for skill in self._approved_skills:
            if re.search(skill["keywords"], prompt, re.IGNORECASE):
                return skill["name"]
        return None

    def chat(self, prompt: str, system: str = None, model: str = None, force_model: str = None) -> dict:
        # 强制指定模型
        if force_model:
            if force_model == "deepseek":
                return self._deepseek.chat(prompt, system)
            else:
                return self._ollama.chat(prompt, system, model=model)

        # 技能优先：匹配已批准技能的触发词
        matched_skill = self._match_skill(prompt)
        if matched_skill:
            from skill_library import run_skill
            logger.info(f"[技能命中] {matched_skill} ← {prompt[:30]}...")
            return run_skill(matched_skill.lower().replace(" ", "_"), prompt)

        # 路由决策
        target = self._route(prompt)
        if target == "deepseek":
            return self._deepseek.chat(prompt, system)
        else:
            return self._ollama.chat(prompt, system, model=model)

    def search_memory(self, query: str, top_k: int = 5) -> list[dict]:
        return self._deepseek.search_memory(query, top_k)

    def run_skill(self, skill_name: str, **params) -> dict:
        return self._deepseek.run_skill(skill_name, **params)

    def health_check(self) -> bool:
        return self._deepseek.health_check() or self._ollama.health_check()


# ── 降级策略 ─────────────────────────────────────────────────────────────

class FallbackAgent(AgentInterface):
    """降级实现：当所有 Agent 都不可用时"""

    def chat(self, prompt: str, system: str = None, model: str = None) -> dict:
        return {
            "content": "当前所有 AI 服务均不可用，请检查网络连接。",
            "error": "ALL_AGENTS_UNAVAILABLE",
        }

    def search_memory(self, query: str, top_k: int = 5) -> list[dict]:
        return []

    def run_skill(self, skill_name: str, **params) -> dict:
        return {"error": f"无可用 Agent 执行技能: {skill_name}"}


# ── 工厂函数 ─────────────────────────────────────────────────────────────

_agent: Optional[AgentInterface] = None


def get_agent() -> AgentInterface:
    """
    获取 Agent 实例（支持降级）
    优先级: RouterAgent > DeepSeekAgent > OllamaAgent > FallbackAgent
    """
    global _agent
    if _agent is not None:
        return _agent

    # 尝试 RouterAgent
    try:
        _agent = RouterAgent()
        if _agent.health_check():
            logger.info("Agent: RouterAgent (自动路由)")
            return _agent
    except Exception as e:
        logger.warning(f"RouterAgent 初始化失败: {e}")

    # 降级到 DeepSeek
    try:
        agent = DeepSeekAgent()
        if agent.health_check():
            _agent = agent
            logger.info("Agent: DeepSeekAgent")
            return _agent
    except Exception:
        pass

    # 降级到 Ollama
    try:
        agent = OllamaAgent()
        if agent.health_check():
            _agent = agent
            logger.info("Agent: OllamaAgent")
            return _agent
    except Exception:
        pass

    # 终极降级
    _agent = FallbackAgent()
    logger.warning("Agent: FallbackAgent (所有服务不可用)")
    return _agent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent = get_agent()
    print(f"当前 Agent: {type(agent).__name__}")
    print(f"健康检查: {'✅' if agent.health_check() else '❌'}")

    print("\n测试 chat...")
    result = agent.chat("你好，简单介绍一下你自己")
    print(f"结果: {result.get('content', '')[:200]}")
    print(f"错误: {result.get('error')}")
