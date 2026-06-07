"""
ollama_benchmark.py — Ollama 金融基准测试
===========================================
对比 DeepSeek API vs Ollama 本地模型在 50 道金融问答上的表现差异。

指标体系：
  1. 响应质量（DeepSeek 为基准，人工/自动评分）
  2. 响应速度（平均延迟秒数）
  3. Token 消耗（输入/输出）
  4. 成本估算（DeepSeek API 费用 vs Ollama 免费）

用法：
  python ollama_benchmark.py                          # 完整 50 题测试
  python ollama_benchmark.py --quick                  # 快速模式（10 题）
  python ollama_benchmark.py --output report.json     # 输出 JSON 报告

依赖：
  - DeepSeek API 可用（环境变量 DEEPSEEK_API_KEY）
  - Ollama 服务运行中（http://localhost:11434）
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("ollama_benchmark")

# ── 50 道金融测试题 ──────────────────────────────────────────────────────
# 按类别分组，每类 5-10 题，覆盖白名单规则表的所有任务类型
BENCHMARK_QUESTIONS = [
    # ── 持仓查询（ollama）────────────────────────────────────────────────
    {"id": "Q01", "category": "持仓查询", "expected_model": "ollama", "question": "我的持仓目前盈亏多少？"},  # noqa: E501
    {"id": "Q02", "category": "持仓查询", "expected_model": "ollama", "question": "招商银行目前的成本价是多少？"},  # noqa: E501
    {"id": "Q03", "category": "持仓查询", "expected_model": "ollama", "question": "我账户里现金比例有多少？"},  # noqa: E501
    {"id": "Q04", "category": "持仓查询", "expected_model": "ollama", "question": "东方财富的市值占比是多少？"},  # noqa: E501
    {"id": "Q05", "category": "持仓查询", "expected_model": "ollama", "question": "我的总资产现在多少钱？"},  # noqa: E501

    # ── 行情查询（ollama）────────────────────────────────────────────────
    {"id": "Q06", "category": "行情查询", "expected_model": "ollama", "question": "比亚迪现价多少？"},  # noqa: E501
    {"id": "Q07", "category": "行情查询", "expected_model": "ollama", "question": "今天沪深300涨了多少？"},  # noqa: E501
    {"id": "Q08", "category": "行情查询", "expected_model": "ollama", "question": "茅台今天收盘价是多少？"},  # noqa: E501
    {"id": "Q09", "category": "行情查询", "expected_model": "ollama", "question": "招商银行的PE现在多少？"},  # noqa: E501
    {"id": "Q10", "category": "行情查询", "expected_model": "ollama", "question": "恒瑞医药今天涨了还是跌了？"},  # noqa: E501

    # ── 技术指标计算（ollama）────────────────────────────────────────────
    {"id": "Q11", "category": "技术指标", "expected_model": "ollama", "question": "RSI指标现在什么水平？"},  # noqa: E501
    {"id": "Q12", "category": "技术指标", "expected_model": "ollama", "question": "帮我算一下布林带"},  # noqa: E501
    {"id": "Q13", "category": "技术指标", "expected_model": "ollama", "question": "MACD金叉了吗？"},
    {"id": "Q14", "category": "技术指标", "expected_model": "ollama", "question": "均线系统怎么走？"},  # noqa: E501
    {"id": "Q15", "category": "技术指标", "expected_model": "ollama", "question": "ATR指标值是多少？"},  # noqa: E501

    # ── 通用计算（ollama）───────────────────────────────────────────────
    {"id": "Q16", "category": "通用计算", "expected_model": "ollama", "question": "帮我算年化收益率"},  # noqa: E501
    {"id": "Q17", "category": "通用计算", "expected_model": "ollama", "question": "夏普比率怎么算？"},  # noqa: E501
    {"id": "Q18", "category": "通用计算", "expected_model": "ollama", "question": "最大回撤是多少？"},  # noqa: E501
    {"id": "Q19", "category": "通用计算", "expected_model": "ollama", "question": "这个组合的胜率如何？"},  # noqa: E501
    {"id": "Q20", "category": "通用计算", "expected_model": "ollama", "question": "盈亏比是多少？"},

    # ── 新闻/行情解释（ollama）───────────────────────────────────────────
    {"id": "Q21", "category": "新闻摘要", "expected_model": "ollama", "question": "今天市场有什么重要新闻？"},  # noqa: E501
    {"id": "Q22", "category": "新闻摘要", "expected_model": "ollama", "question": "今天大盘为什么涨？"},  # noqa: E501
    {"id": "Q23", "category": "新闻摘要", "expected_model": "ollama", "question": "最近有什么利好？"},  # noqa: E501
    {"id": "Q24", "category": "新闻摘要", "expected_model": "ollama", "question": "今天市场情绪怎么样？"},  # noqa: E501
    {"id": "Q25", "category": "新闻摘要", "expected_model": "ollama", "question": "外资今天流入还是流出？"},  # noqa: E501

    # ── 研报/公告提取（ollama）──────────────────────────────────────────
    {"id": "Q26", "category": "研报提取", "expected_model": "ollama", "question": "最近有哪些券商研报？"},  # noqa: E501
    {"id": "Q27", "category": "研报提取", "expected_model": "ollama", "question": "帮我总结一下最近的公告"},  # noqa: E501
    {"id": "Q28", "category": "研报提取", "expected_model": "ollama", "question": "提取这只股票的关键数据"},  # noqa: E501
    {"id": "Q29", "category": "研报提取", "expected_model": "ollama", "question": "最近有什么增持公告？"},  # noqa: E501

    # ── 行业分析（DeepSeek）────────────────────────────────────────────
    {"id": "Q30", "category": "行业分析", "expected_model": "deepseek", "question": "半导体板块近期怎么看？"},  # noqa: E501
    {"id": "Q31", "category": "行业分析", "expected_model": "deepseek", "question": "新能源赛道还有机会吗？"},  # noqa: E501
    {"id": "Q32", "category": "行业分析", "expected_model": "deepseek", "question": "消费板块现在能不能布局？"},  # noqa: E501
    {"id": "Q33", "category": "行业分析", "expected_model": "deepseek", "question": "医药行业最近有什么变化？"},  # noqa: E501
    {"id": "Q34", "category": "行业分析", "expected_model": "deepseek", "question": "金融板块估值合理吗？"},  # noqa: E501

    # ── 宏观分析（DeepSeek）────────────────────────────────────────────
    {"id": "Q35", "category": "宏观分析", "expected_model": "deepseek", "question": "美联储降息对A股有什么影响？"},  # noqa: E501
    {"id": "Q36", "category": "宏观分析", "expected_model": "deepseek", "question": "最近CPI数据怎么看？"},  # noqa: E501
    {"id": "Q37", "category": "宏观分析", "expected_model": "deepseek", "question": "人民币汇率走势如何？"},  # noqa: E501
    {"id": "Q38", "category": "宏观分析", "expected_model": "deepseek", "question": "当前经济形势怎么样？"},  # noqa: E501
    {"id": "Q39", "category": "宏观分析", "expected_model": "deepseek", "question": "利率政策对市场有什么影响？"},  # noqa: E501

    # ── 策略/决策（DeepSeek）────────────────────────────────────────────
    {"id": "Q40", "category": "策略建议", "expected_model": "deepseek", "question": "东方财富现在可以加仓吗？"},  # noqa: E501
    {"id": "Q41", "category": "策略建议", "expected_model": "deepseek", "question": "建议我现在卖出一部分吗？"},  # noqa: E501
    {"id": "Q42", "category": "策略建议", "expected_model": "deepseek", "question": "帮我制定下周的操作计划"},  # noqa: E501
    {"id": "Q43", "category": "策略建议", "expected_model": "deepseek", "question": "要不要止损？"},
    {"id": "Q44", "category": "策略建议", "expected_model": "deepseek", "question": "帮我评估一下组合风险"},  # noqa: E501

    # ── 风控检查（ollama）───────────────────────────────────────────────
    {"id": "Q45", "category": "风控检查", "expected_model": "ollama", "question": "我的仓位有没有超限？"},  # noqa: E501
    {"id": "Q46", "category": "风控检查", "expected_model": "ollama", "question": "持仓集中度风险高吗？"},  # noqa: E501
    {"id": "Q47", "category": "风控检查", "expected_model": "ollama", "question": "帮我检查合规情况"},  # noqa: E501
    {"id": "Q48", "category": "风控检查", "expected_model": "ollama", "question": "现在杠杆率多少？"},  # noqa: E501

    # ── 情绪分析（ollama）───────────────────────────────────────────────
    {"id": "Q49", "category": "情绪分析", "expected_model": "ollama", "question": "市场情绪偏向多头还是空头？"},  # noqa: E501
    {"id": "Q50", "category": "情绪分析", "expected_model": "ollama", "question": "资金流向怎么样？"},  # noqa: E501
]


def _get_model_client(model_type: str):
    """获取模型客户端实例"""
    if model_type == "deepseek":
        from llm_caller import DeepSeekClient
        return DeepSeekClient()
    else:
        from llm_caller import OllamaClient
        return OllamaClient()


def _estimate_cost(prompt_tokens: int, completion_tokens: int, model: str) -> dict:
    """
    估算 API 费用。
    DeepSeek: ~¥1.0/1M 输入 tokens, ¥2.0/1M 输出 tokens
    Ollama: 免费（本地运行，仅计电费）
    """
    if model == "ollama":
        return {"cost_cny": 0.0, "note": "本地运行，无直接 API 费用"}
    input_cost = prompt_tokens * 1.0 / 1_000_000
    output_cost = completion_tokens * 2.0 / 1_000_000
    return {"cost_cny": round(input_cost + output_cost, 6), "input_cost": round(input_cost, 6), "output_cost": round(output_cost, 6)}  # noqa: E501


def run_single_test(q: dict, model_type: str, timeout: int = 60) -> dict:
    """
    对单道题目执行一次模型调用，返回结构化结果。
    """
    client = _get_model_client(model_type)
    start = time.time()

    try:
        result = client.chat(q["question"])
        elapsed = time.time() - start
        content = result.get("content", "")
        error = result.get("error")

        return {
            "model": model_type,
            "elapsed_s": round(elapsed, 2),
            "content_length": len(content),
            "content_preview": content[:200] if content else "",
            "error": error,
            "success": error is None,
        }
    except Exception as e:
        elapsed = time.time() - start
        return {
            "model": model_type,
            "elapsed_s": round(elapsed, 2),
            "content_length": 0,
            "content_preview": "",
            "error": str(e),
            "success": False,
        }


def run_benchmark(quick: bool = False, output_path: str = None) -> dict:
    """
    执行全量/快速基准测试。
    """
    questions = BENCHMARK_QUESTIONS[:10] if quick else BENCHMARK_QUESTIONS
    print(f"\n{'='*60}")
    print("  Ollama 金融基准测试")
    print(f"  模式: {'快速 (10 题)' if quick else f'完整 ({len(questions)} 题)'}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    results = []
    stats = {"deepseek": {"total": 0, "success": 0, "elapsed": [], "chars": []},
             "ollama": {"total": 0, "success": 0, "elapsed": [], "chars": []}}

    # 路由匹配统计
    route_match = {"correct": 0, "wrong": 0, "total": 0}

    for i, q in enumerate(questions):
        print(f"  [{i+1}/{len(questions)}] {q['id']} [{q['category']}] {q['question'][:40]}...")

        # 测试 DeepSeek
        ds_result = run_single_test(q, "deepseek")
        stats["deepseek"]["total"] += 1
        if ds_result["success"]:
            stats["deepseek"]["success"] += 1
            stats["deepseek"]["elapsed"].append(ds_result["elapsed_s"])
            stats["deepseek"]["chars"].append(ds_result["content_length"])

        # 测试 Ollama
        ol_result = run_single_test(q, "ollama")
        stats["ollama"]["total"] += 1
        if ol_result["success"]:
            stats["ollama"]["success"] += 1
            stats["ollama"]["elapsed"].append(ol_result["elapsed_s"])
            stats["ollama"]["chars"].append(ol_result["content_length"])

        # 路由验证（model_router 判断结果 vs 预期）
        from model_router import route
        actual_route = route(q["question"])
        expected = q["expected_model"]
        is_match = actual_route == expected
        if is_match:
            route_match["correct"] += 1
        else:
            route_match["wrong"] += 1
        route_match["total"] += 1

        ds_status = "✅" if ds_result["success"] else "❌"
        ol_status = "✅" if ol_result["success"] else "❌"
        route_icon = "✅" if is_match else "⚠️"
        print(f"    DeepSeek {ds_status} {ds_result['elapsed_s']:.1f}s | "
              f"Ollama {ol_status} {ol_result['elapsed_s']:.1f}s | "
              f"路由: {actual_route}(期望{expected}) {route_icon}")

        results.append({
            "id": q["id"],
            "category": q["category"],
            "question": q["question"],
            "expected_model": expected,
            "actual_route": actual_route,
            "route_match": is_match,
            "deepseek": ds_result,
            "ollama": ol_result,
        })

    # ── 汇总统计 ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  测试汇总")
    print(f"{'='*60}")

    for model in ["deepseek", "ollama"]:
        s = stats[model]
        avg_elapsed = sum(s["elapsed"]) / len(s["elapsed"]) if s["elapsed"] else 0
        avg_chars = sum(s["chars"]) / len(s["chars"]) if s["chars"] else 0
        success_rate = s["success"] / s["total"] * 100 if s["total"] else 0
        print(f"\n  {model.upper()}:")
        print(f"    成功率:      {s['success']}/{s['total']} ({success_rate:.1f}%)")
        print(f"    平均延迟:    {avg_elapsed:.2f} 秒")
        print(f"    平均响应长度: {avg_chars:.0f} 字符")

    # 路由准确率
    route_acc = route_match["correct"] / route_match["total"] * 100 if route_match["total"] else 0
    print(f"\n  路由准确率: {route_match['correct']}/{route_match['total']} ({route_acc:.1f}%)")
    if route_match["wrong"] > 0:
        print("  路由偏差明细:")
        for r in results:
            if not r["route_match"]:
                print(f"    ⚠️ {r['id']} [{r['category']}] → 路由 {r['actual_route']}（期望 {r['expected_model']}）")  # noqa: E501

    # 响应速度对比
    ds_avg = sum(stats["deepseek"]["elapsed"]) / len(stats["deepseek"]["elapsed"]) if stats["deepseek"]["elapsed"] else 0  # noqa: E501
    ol_avg = sum(stats["ollama"]["elapsed"]) / len(stats["ollama"]["elapsed"]) if stats["ollama"]["elapsed"] else 0  # noqa: E501
    speed_ratio = ol_avg / ds_avg if ds_avg > 0 else 0
    print("\n  速度对比:")
    print(f"    DeepSeek 平均: {ds_avg:.2f}s")
    print(f"    Ollama 平均:   {ol_avg:.2f}s")
    print(f"    速度比:        {speed_ratio:.2f}x (Ollama / DeepSeek)")

    # ── 输出报告 ──────────────────────────────────────────────────────
    report = {
        "meta": {
            "timestamp": datetime.now().isoformat(),
            "mode": "quick" if quick else "full",
            "total_questions": len(questions),
        },
        "summary": {
            "deepseek": {
                "success_rate": round(stats["deepseek"]["success"] / stats["deepseek"]["total"] * 100, 1),  # noqa: E501
                "avg_latency_s": round(ds_avg, 2),
                "avg_response_chars": round(sum(stats["deepseek"]["chars"]) / len(stats["deepseek"]["chars"]), 0) if stats["deepseek"]["chars"] else 0,  # noqa: E501
            },
            "ollama": {
                "success_rate": round(stats["ollama"]["success"] / stats["ollama"]["total"] * 100, 1),  # noqa: E501
                "avg_latency_s": round(ol_avg, 2),
                "avg_response_chars": round(sum(stats["ollama"]["chars"]) / len(stats["ollama"]["chars"]), 0) if stats["ollama"]["chars"] else 0,  # noqa: E501
            },
            "route_accuracy": round(route_acc, 1),
            "speed_ratio_ollama_vs_deepseek": round(speed_ratio, 2),
        },
        "details": results,
    }

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n  报告已保存: {output_path}")

    print(f"\n{'='*60}\n")
    return report


def main():
    parser = argparse.ArgumentParser(description="Ollama 金融基准测试")
    parser.add_argument("--quick", action="store_true", help="快速模式（10 题）")
    parser.add_argument("--output", type=str, default=None, help="输出 JSON 报告路径")
    args = parser.parse_args()

    report = run_benchmark(quick=args.quick, output_path=args.output)

    # 返回退出码：路由准确率 < 60% 时告警
    if report["summary"]["route_accuracy"] < 60:
        print("⚠️  路由准确率低于 60%，建议检查 model_router.py 的白名单规则")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()