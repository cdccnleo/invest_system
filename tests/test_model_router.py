"""
test_model_router.py — 模型路由器单元测试
覆盖: classify_task, route
"""

import pytest
from model_router import classify_task, route, WHITELIST_RULES


class TestClassifyTask:
    """任务分类测试"""

    def test_strategy_routing(self):
        """策略生成类查询应路由到 DeepSeek"""
        cat, t = classify_task("帮我制定下周的操作计划")
        assert cat == "strategy"
        assert t == "generate"

    def test_macro_routing(self):
        """宏观分析类查询应路由到 DeepSeek"""
        cat, t = classify_task("分析一下当前的宏观经济形势")
        assert cat == "macro"

    def test_fundamental_routing(self):
        """个股基本面类查询应路由到 DeepSeek"""
        cat, t = classify_task("帮我分析一下贵州茅台的估值水平")
        assert cat == "stock"
        assert t == "research"

    def test_holding_query_routing(self):
        """持仓查询应路由到 Ollama"""
        cat, t = classify_task("我的持仓有哪些")
        assert cat == "query"
        assert t == "holding"

    def test_technical_calc_routing(self):
        """技术指标计算应路由到 Ollama"""
        cat, t = classify_task("计算一下这只股票的MACD")
        assert cat == "calc"
        assert t == "technical"

    def test_news_explain_routing(self):
        """新闻解释应路由到 Ollama"""
        cat, t = classify_task("今天大盘为什么跌了")
        assert cat == "news"
        assert t == "summary"

    def test_sentiment_routing(self):
        """市场情绪分析应路由到 Ollama"""
        cat, t = classify_task("当前市场情绪怎么样")
        assert cat == "news"
        assert t == "sentiment"

    def test_decision_routing(self):
        """决策判断应路由到 DeepSeek"""
        cat, t = classify_task("这只股票能不能买")
        assert cat == "judge"
        assert t == "action"

    def test_unknown_query(self):
        """未匹配的查询应为 unknown"""
        cat, t = classify_task("你好")
        assert cat == "unknown"

    def test_sector_analysis(self):
        """行业分析应路由到 DeepSeek"""
        cat, t = classify_task("新能源行业现在的景气度如何")
        assert cat == "sector"


class TestRoute:
    """路由决策测试"""

    def test_force_model_overrides(self):
        """强制模型应覆盖白名单"""
        result = route("帮我制定操作计划", force_model="ollama")
        assert result == "ollama"

    def test_whitelist_rules_complete(self):
        """白名单规则应有合理的规则数量"""
        assert len(WHITELIST_RULES) >= 10


class TestWhitelistRules:
    """白名单规则完整性测试"""

    def test_all_rules_have_fields(self):
        """每条规则应有 5 个字段"""
        for rule in WHITELIST_RULES:
            assert len(rule) == 5, f"规则字段数应为 5: {rule}"

    def test_all_routes_valid(self):
        """路由目标应是有效值"""
        valid_targets = {"deepseek", "ollama", "local"}
        for rule in WHITELIST_RULES:
            _, _, _, route_to, _ = rule
            assert route_to in valid_targets, f"无效路由目标: {route_to}"