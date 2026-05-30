"""
model_router.py — 白名单优先模型路由
规则匹配 → 命中白名单直接路由 → 未命中再走 LLM 判断
"""

import os, re, logging
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger("invest_system.router")

# ── 模型定义 ──────────────────────────────────────────────────────────────
MODEL_DEEPSEEK = "deepseek-chat"
MODEL_OLLAMA = os.environ.get("LOCAL_MODEL", "gemma4:e4b")

# ── 白名单规则表 ────────────────────────────────────────────────────────
# (task_type, keywords_pattern, route_to, description)
WHITELIST_RULES = [
    # (task_category, task_type, keywords_pattern, route_to, description)
    # ── 强制 DeepSeek（不可降级）────────────────────────────────
    ("strategy", "generate", r"策略|操作计划|建议(?!.*查)|买卖|增持|减持|调仓|建仓|清仓|加仓|减仓|换仓|仓位管理",
     "deepseek", "策略生成"),
    ("macro", "analysis", r"宏观|政策|利率|GDP|CPI|美联储|央行|货币|财政|经济形势|基本面.*分析",
     "deepseek", "宏观分析"),
    ("sector", "analysis", r"行业|板块|赛道|轮动|景气|周期|产业|产业链",
     "deepseek", "行业分析"),
    ("stock", "research", r"基本面|估值|PE(?!.*多少)|PB|ROE|净利润|营收|毛利率|竞争优势|财务.*分析|价值.*投资",
     "deepseek", "个股基本面"),
    ("judge", "action", r"能不能买|该不该卖|要不要|可以加仓吗|帮我决定|要不要清仓|要不要止损|要不要换|哪个好|风险评估|帮我评估|请评估|评估一下",
     "deepseek", "决策判断"),

    # ── Ollama 本地查询（中文词更宽泛匹配）─────────────────────────
    ("query", "holding", r"持仓|仓位|成本|盈亏|市值|份额|资金|现金|我的.*股|股票.*收益|基金.*收益",
     "ollama", "持仓查询"),
    ("query", "price", r"现价|当前价格|行情|报价|收盘|开盘|涨跌|涨了多少|跌了多少|价格.*多少|PE.*多少|股价",
     "ollama", "行情查询"),
    ("calc", "technical", r"RSI|MACD|布林带|均线|KDJ|ATR|OBV|威廉|斐波那契|技术指标",
     "ollama", "技术指标计算"),
    ("calc", "general", r"计算|收益率|年化|夏普比率|最大回撤|胜率|盈亏比",
     "ollama", "通用计算"),
    ("news", "summary", r"新闻|快讯|今日要闻|市场消息|涨了|跌了|为什么涨|为什么跌|利好|利空|大盘.*如何|大盘.*表现|整体.*如何",
     "ollama", "新闻/行情解释"),
    ("news", "report", r"研报|公告内容|总结.*公告|公告.*总结|提取.*数据",
     "ollama", "研报/公告提取"),
    ("news", "sentiment", r"市场情绪|多头|空头|恐慌|乐观|悲观|资金.*流向|情绪",
     "ollama", "情绪分析"),
    ("db", "history", r"历史.*怎么|之前怎么|那段时间|历史.*检索|回顾|历史上|历史.*情况|历史.*市场",
     "local", "历史检索"),
    ("risk", "check", r"风控|仓位检查|超限|合规|集中度|杠杆|风险.*评估",
     "local", "风控检查"),
]


def classify_task(query: str) -> tuple[str, str]:
    """
    任务分类：规则匹配白名单
    返回: (task_category, task_type)
    """
    for rule in WHITELIST_RULES:
        task_category, task_type, pattern, route_to, description = rule
        if re.search(pattern, query, re.IGNORECASE):
            return task_category, task_type
    return ("unknown", "unknown")


def route(query: str, force_model: str = None) -> str:
    """
    路由决策：
    1. 检查白名单 → 命中则直接路由
    2. force_model 强制指定模型
    3. 未命中 → 走 LLM 判断一次（消耗少量 token）
    """
    # 强制指定覆盖一切
    if force_model:
        logger.info(f"强制路由到 {force_model}（用户指定）")
        return force_model

    task_cat, task_type = classify_task(query)
    rule_match = task_cat != "unknown"

    if rule_match:
        # 查白名单
        for rule in WHITELIST_RULES:
            task_category_r, task_type_r, pattern, route_to, desc = rule
            if task_category_r == task_cat and task_type_r == task_type:
                if task_category_r in ("strategy", "macro", "sector", "stock", "judge"):
                    logger.info(f"[路由] '{query[:30]}...' → DeepSeek (强制规则: {desc})")
                    return "deepseek"
                else:
                    logger.info(f"[路由] '{query[:30]}...' → Ollama (规则: {desc})")
                    return "ollama"

    # 未命中白名单：走 LLM 做一次分类判断
    logger.info(f"[路由] '{query[:30]}...' → 未命中白名单，走 LLM 判断")
    return _llm_fallback_classify(query)


def _llm_fallback_classify(query: str) -> str:
    """
    未命中白名单时，调用本地小模型做一次快速分类
    判断：这个任务是否需要 DeepSeek？
    """
    from llm_caller import get_llm_client

    client = get_llm_client()
    prompt = (
        "判断以下用户查询最适合用什么模型处理：\n"
        "A = Ollama本地模型（免费、快速，适合简单查询、计算、摘要）\n"
        "B = DeepSeek API（收费、深度推理，适合策略建议、宏观分析、买卖决策）\n\n"
        f"用户查询：{query}\n\n"
        "只回答 A 或 B，不要解释。"
    )

    try:
        result = client.chat(prompt, system="你是一个任务路由分类器，只输出A或B。")
        raw = result.get("content", "").strip().upper()
        # 直接取第一个字符判断
        answer = raw[0] if raw else ""
        if "B" in answer or answer == "B":
            logger.info(f"[路由] LLM 判断 → DeepSeek")
            return "deepseek"
        else:
            logger.info(f"[路由] LLM 判断 → Ollama")
            return "ollama"
    except Exception as e:
        logger.warning(f"LLM 分类失败，默认走 Ollama: {e}")
        return "ollama"


def route_and_execute(query: str, context: dict = None, force_model: str = None) -> dict:
    """
    完整路由执行流程
    1. 路由决策
    2. 调用对应模型
    3. 返回结果
    """
    model = route(query, force_model=force_model)
    from llm_caller import get_llm_client

    client = get_llm_client()
    prompt = query
    if context:
        # 组装上下文
        ctx_parts = []
        for k, v in context.items():
            if v:
                ctx_parts.append(f"[{k}]\n{v}")
        if ctx_parts:
            prompt = "\n\n".join(ctx_parts) + "\n\n[用户问题]\n" + query

    if model == "deepseek":
        result = client.chat(prompt, system="你是一名专业量化投资顾问。")
    else:
        result = client.chat(prompt, system="用简洁语言回答。", model=MODEL_OLLAMA)

    return {
        "model": model,
        "query": query,
        "result": result.get("content", ""),
        "error": result.get("error"),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    test_queries = [
        "我的持仓现在盈亏多少？",
        "帮我计算年化收益率",
        "市场情绪怎么样？",
        "东方财富现在可以加仓吗？",
        "半导体板块近期怎么看？",
        "RSI指标是什么水平？",
        "美联储降息对A股有什么影响？",
    ]

    print("\n=== 路由测试 ===")
    for q in test_queries:
        model = route(q)
        task_cat, task_type = classify_task(q)
        print(f"Q: {q}")
        print(f"   → {model} | 分类: {task_cat}.{task_type}\n")
