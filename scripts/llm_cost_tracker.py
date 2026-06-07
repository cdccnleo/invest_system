"""
LLM Token/成本追踪器
记录每日 DeepSeek API 调用次数、Token消耗、估算费用
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("invest_system.llm_cost_tracker")

# DeepSeek pricing (as of 2025)
# deepseek-chat: ¥1/千Tokens (input), ¥2/千Tokens (output)
# Assume: 1 CNY = 7.2 USD rate for calculation

MODEL_COST = {
    "deepseek-chat": {"input": 1.0, "output": 2.0},  # CNY per 1K tokens
    "deepseek-coder": {"input": 1.0, "output": 2.0},
    "gpt-4o": {"input": 15.0, "output": 60.0},  # USD per 1M tokens
}

COST_FILE = Path("data/llm_cost_tracker.json")


def _ensure_data_dir():
    """确保 data 目录存在"""
    COST_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_data() -> dict:
    """加载现有追踪数据"""
    if not COST_FILE.exists():
        return {"records": [], "daily": {}, "monthly": {}}
    try:
        with open(COST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"加载追踪数据失败: {e}")
        return {"records": [], "daily": {}, "monthly": {}}


def _save_data(data: dict):
    """保存追踪数据"""
    _ensure_data_dir()
    try:
        with open(COST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"保存追踪数据失败: {e}")


def record_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    date_str: str | None = None,
) -> bool:
    """
    记录一次 LLM 调用。

    Args:
        model: 模型名称 (如 "deepseek-chat")
        input_tokens: 输入 token 数
        output_tokens: 输出 token 数
        date_str: 日期字符串，格式 YYYYMMDD，默认今天

    Returns:
        bool: 是否成功记录
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    year_month = date_str[:6]  # YYYYMM

    try:
        data = _load_data()

        # 计算费用
        cost_info = MODEL_COST.get(model) or MODEL_COST["deepseek-chat"]
        input_cost = (input_tokens / 1000) * cost_info["input"]
        output_cost = (output_tokens / 1000) * cost_info["output"]
        total_cost = input_cost + output_cost

        # 构建记录
        record = {
            "timestamp": datetime.now().isoformat(),
            "date": date_str,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_cny": round(total_cost, 4),
            "cost_usd": round(total_cost / 7.2, 4),
        }

        # 追加记录
        data["records"].append(record)

        # 更新日统计
        if date_str not in data["daily"]:
            data["daily"][date_str] = {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_cny": 0.0,
                "cost_usd": 0.0,
                "models": {},
            }
        daily = data["daily"][date_str]
        daily["calls"] += 1
        daily["input_tokens"] += input_tokens
        daily["output_tokens"] += output_tokens
        daily["total_tokens"] += input_tokens + output_tokens
        daily["cost_cny"] = round(daily["cost_cny"] + total_cost, 4)
        daily["cost_usd"] = round(daily["cost_usd"] + total_cost / 7.2, 4)

        # 按模型统计
        if model not in daily["models"]:
            daily["models"][model] = {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_cny": 0.0,
            }
        dm = daily["models"][model]
        dm["calls"] += 1
        dm["input_tokens"] += input_tokens
        dm["output_tokens"] += output_tokens
        dm["cost_cny"] = round(dm["cost_cny"] + total_cost, 4)

        # 更新月统计
        if year_month not in data["monthly"]:
            data["monthly"][year_month] = {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_cny": 0.0,
                "cost_usd": 0.0,
            }
        monthly = data["monthly"][year_month]
        monthly["calls"] += 1
        monthly["input_tokens"] += input_tokens
        monthly["output_tokens"] += output_tokens
        monthly["total_tokens"] += input_tokens + output_tokens
        monthly["cost_cny"] = round(monthly["cost_cny"] + total_cost, 4)
        monthly["cost_usd"] = round(monthly["cost_usd"] + total_cost / 7.2, 4)

        _save_data(data)
        logger.debug(
            f"记录 LLM 使用: {model} | "
            f"输入 {input_tokens} / 输出 {output_tokens} | "
            f"费用 ¥{total_cost:.4f}"
        )
        return True

    except Exception as e:
        logger.error(f"记录 LLM 使用失败: {e}")
        return False


def get_daily_stats(date_str: str | None = None) -> dict:
    """
    获取指定日期的统计。

    Args:
        date_str: 日期字符串，格式 YYYYMMDD，默认今天

    Returns:
        dict: 日统计数据
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    data = _load_data()
    daily = data["daily"].get(date_str)

    if not daily:
        return {
            "date": date_str,
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_cny": 0.0,
            "cost_usd": 0.0,
            "avg_cost_per_call": 0.0,
            "models": {},
        }

    daily["avg_cost_per_call"] = (
        round(daily["cost_cny"] / daily["calls"], 4) if daily["calls"] > 0 else 0.0
    )
    daily["date"] = date_str
    return daily


def get_monthly_stats(year_month: str | None = None) -> dict:
    """
    获取月统计。

    Args:
        year_month: 年月字符串，格式 YYYYMM，默认当月

    Returns:
        dict: 月统计数据
    """
    if year_month is None:
        year_month = datetime.now().strftime("%Y%m")

    data = _load_data()
    monthly = data.get("monthly", {}).get(year_month)

    if not monthly:
        return {
            "year_month": year_month,
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_cny": 0.0,
            "cost_usd": 0.0,
            "avg_cost_per_call": 0.0,
            "days_with_usage": 0,
        }

    monthly["avg_cost_per_call"] = (
        round(monthly["cost_cny"] / monthly["calls"], 4) if monthly["calls"] > 0 else 0.0
    )

    # 统计有使用量的天数
    days_with_usage = sum(
        1 for d in data.get("daily", {}).keys() if d.startswith(year_month)
        and data["daily"][d]["calls"] > 0
    )
    monthly["days_with_usage"] = days_with_usage
    monthly["year_month"] = year_month

    return monthly


def format_cost_report(stats: dict, period: str = "daily") -> str:
    """
    格式化成本报告为推送文本。

    Args:
        stats: get_daily_stats() 或 get_monthly_stats() 返回的统计
        period: "daily" 或 "monthly"

    Returns:
        str: 格式化的推送报告文本
    """
    date_label = stats.get("date", stats.get("year_month", ""))
    calls = stats.get("calls", 0)
    input_tokens = stats.get("input_tokens", 0)
    output_tokens = stats.get("output_tokens", 0)
    total_tokens = stats.get("total_tokens", 0)
    cost_cny = stats.get("cost_cny", 0.0)
    cost_usd = stats.get("cost_usd", 0.0)
    avg_cost = stats.get("avg_cost_per_call", 0.0)
    models = stats.get("models", {})

    if period == "daily":
        title = f"📊 LLM 日成本报告 ({date_label})"
    else:
        title = f"📊 LLM 月成本报告 ({date_label})"

    if calls == 0:
        return f"{title}\n\n今日无 LLM 调用记录。"

    lines = [
        title,
        "",
        f"💰 总费用: ¥{cost_cny:.4f} (≈${cost_usd:.4f})",
        f"📞 调用次数: {calls} 次",
        f"📥 输入 Token: {input_tokens:,}",
        f"📤 输出 Token: {output_tokens:,}",
        f"📊 总 Token: {total_tokens:,}",
        f"📈 平均费用/次: ¥{avg_cost:.4f}",
        "",
    ]

    if models:
        lines.append("🔹 按模型统计:")
        for model_name, model_stats in models.items():
            model_cost = model_stats.get("cost_cny", 0)
            model_calls = model_stats.get("calls", 0)
            lines.append(
                f"  • {model_name}: ¥{model_cost:.4f} ({model_calls}次, "
                f"输入{model_stats.get('input_tokens', 0):,} / "
                f"输出{model_stats.get('output_tokens', 0):,})"
            )

    return "\n".join(lines)
