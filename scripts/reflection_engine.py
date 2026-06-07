"""
reflection_engine.py — 每日复盘 + USER.md 自动更新
对比：计划 vs 修改后 vs 实际执行 → 归因分析 → 更新画像
"""

import os
import json
import logging
from datetime import date

import psycopg2

logger = logging.getLogger("invest_system.reflection")

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",  # 运行时由 _get_password() 注入,
}
POSITIONS_CSV = os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv")
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
USER_MD_PATH = os.environ.get("USER_MD_PATH", str(PROJECT_ROOT / "USER.md"))


def _get_password():
    from pgcrypto_migration import get_credential
    return get_credential("DB_PASSWORD")

def get_db_conn():
    cfg = dict(DB_CONFIG)
    cfg["password"] = _get_password()
    return psycopg2.connect(**cfg)


# ── 复盘核心 ────────────────────────────────────────────────────────

def daily_reflection(trade_date: str = None) -> dict:
    """
    每日复盘主流程：
    1. 读取当日操作计划（来自 audit_log）
    2. 读取用户修正记录
    3. 读取实际执行结果（收盘数据）
    4. 对比计划 vs 实际，归因分析
    5. 更新 USER.md 画像
    """
    if trade_date is None:
        trade_date = date.today().strftime("%Y-%m-%d")

    conn = get_db_conn()
    cur = conn.cursor()

    try:
        # 1. 读取当日生成的操作计划
        cur.execute("""
            SELECT event_time, detail
            FROM audit.audit_log
            WHERE event_type IN ('ANALYSIS_COMPLETE', 'SCHEDULED_MORNING_RUN')
              AND DATE(event_time) = %s
            ORDER BY event_time DESC
            LIMIT 1
        """, (trade_date,))
        plan_row = cur.fetchone()

        # 2. 读取用户修正记录
        cur.execute("""
            SELECT event_time, detail
            FROM audit.audit_log
            WHERE event_type = 'USER_MODIFY_PLAN'
              AND DATE(event_time) = %s
            ORDER BY event_time ASC
        """, (trade_date,))
        mod_rows = cur.fetchall()

        # 3. 读取当日收盘行情（实际结果）
        cur.execute("""
            SELECT ts_code, close_price, change_pct
            FROM market.daily_quotes
            WHERE trade_date = %s
        """, (trade_date,))
        quote_rows = cur.fetchall()
        quotes_map = {r[0]: {"close": float(r[1]), "change_pct": float(r[2] or 0)} for r in quote_rows}  # noqa: E501

    finally:
        conn.close()

    # 组装复盘数据
    plan_detail = {}
    if plan_row:
        plan_detail = plan_row[1] if isinstance(plan_row[1], dict) else {}

    modifications = [
        {"time": r[0].isoformat(), "detail": r[1] if isinstance(r[1], dict) else {}}
        for r in mod_rows
    ]

    # 归因分析
    attribution = _analyze_attribution(plan_detail, modifications, quotes_map)

    # 更新 USER.md
    _update_user_profile_from_reflection(attribution)

    # 记录归因到审计日志
    _log_reflection(trade_date, plan_detail, modifications, attribution)

    return {
        "trade_date": trade_date,
        "plan_detail": plan_detail,
        "modifications": modifications,
        "attribution": attribution,
    }


def _analyze_attribution(plan: dict, modifications: list[dict], quotes: dict) -> dict:
    """
    归因分析：计划 vs 修改 vs 市场结果
    """

    # 收集对比数据
    plan_plans = plan.get("plans", [])
    if not plan_plans:
        return {"summary": "无操作计划，无需归因", "insights": [], "confidence_impact": 0}

    # 统计修改比例
    modification_count = len(modifications)
    total_plans = len(plan_plans)
    modification_ratio = modification_count / total_plans if total_plans > 0 else 0

    # 分析修改原因（关键词）
    modification_reasons = []
    for mod in modifications:
        detail = mod.get("detail", {})
        reason = detail.get("reason", "") or detail.get("modification_notes", "")
        if reason:
            modification_reasons.append(reason)

    # 归因总结
    mod_count = modification_count
    if modification_ratio < 0.1:
        confidence_impact = 5  # 修改少，置信度提升
        summary = f"计划执行度高（{total_plans}条计划，修改{mod_count}条）"
    elif modification_ratio < 0.3:
        confidence_impact = 0
        summary = f"部分计划被调整（{total_plans}条中修改{mod_count}条）"
    else:
        confidence_impact = -5  # 大幅修改，置信度下降
        summary = f"计划大幅调整（{total_plans}条中修改{mod_count}条），需关注模型质量"

    # 用 LLM 提炼洞察
    insights = _llm_generate_insights(plan_plans, modification_reasons, quotes)

    return {
        "summary": summary,
        "modification_ratio": round(modification_ratio * 100, 1),
        "modification_count": modification_count,
        "total_plans": total_plans,
        "confidence_impact": confidence_impact,
        "insights": insights,
        "modification_reasons": modification_reasons[:5],
    }


def _llm_generate_insights(plan_plans: list, reasons: list, quotes: dict) -> list[str]:
    """用 LLM 从修改原因中提炼洞察"""
    from llm_caller import get_llm_client

    # 提取涨跌情况
    market_data = []
    for plan in plan_plans[:5]:
        ts = plan.get("ts_code", "")
        if ts in quotes:
            market_data.append(f"{ts}: {quotes[ts]['change_pct']:+.1f}%")

    prompt = (
        "你是复盘分析师。请根据以下复盘数据提炼2-3条可操作的洞察。\n\n"
        f"当日市场表现:\n{chr(10).join(market_data)}\n\n"
        f"操作计划数量: {len(plan_plans)}\n"
        f"用户修正原因摘要:\n{chr(10).join(reasons[:3]) if reasons else '无'}\n\n"
        "请输出一段不超过100字的中文总结，包含:\n"
        "1. 本次计划中最有效的判断\n"
        "2. 需要改进的地方\n"
        "3. 下次类似场景的操作建议"
    )

    try:
        client = get_llm_client()
        result = client.chat(prompt, system="你是专业量化复盘分析师。")
        content = result.get("content", "")
        return [content[:200]] if content else []
    except Exception as e:
        logger.warning(f"LLM 洞察生成失败: {e}")
        return []


def _update_user_profile_from_reflection(attribution: dict):
    """根据归因分析更新 USER.md"""

    # 读取当前 USER.md
    if not os.path.exists(USER_MD_PATH):
        logger.warning("USER.md 不存在，跳过更新")
        return

    confidence_impact = attribution.get("confidence_impact", 0)

    # 构建更新备注
    today = date.today().strftime("%Y-%m-%d")
    update_note = (
        f"\n## {today} 复盘记录\n"
        f"- 计划执行: {attribution.get('summary', '')}\n"
        f"- 修改比例: {attribution.get('modification_ratio', 0)}%\n"
        f"- 洞察: {attribution.get('insights', [''])[0][:100] if attribution.get('insights') else '暂无'}\n"  # noqa: E501
        f"- 置信度调整: {'+' if confidence_impact >= 0 else ''}{confidence_impact}\n"
    )

    try:
        with open(USER_MD_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        # 在 "---" 之前插入复盘记录
        marker = "\n---\n*本文件由系统自动生成"
        if marker in content:
            content = content.replace(marker, update_note + "\n---\n*本文件由系统自动生成")
        else:
            content += update_note

        with open(USER_MD_PATH, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info("USER.md 已更新")
    except Exception as e:
        logger.warning(f"USER.md 更新失败: {e}")


def _log_reflection(trade_date: str, plan: dict, modifications: list, attribution: dict):
    """记录复盘结果到审计日志"""
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO audit.audit_log
                (event_type, operator, target_type, detail, result)
            VALUES ('DAILY_REFLECTION', 'SYSTEM', 'REFLECTION', %s, %s)
        """, (
            json.dumps({
                "trade_date": trade_date,
                "plan_count": len(plan.get("plans", [])),
                "modifications": len(modifications),
                "attribution": attribution,
            }, ensure_ascii=False),
            "SUCCESS",
        ))
        conn.commit()
    finally:
        conn.close()


# ── 定时复盘任务 ──────────────────────────────────────────────────────

def run_daily_reflection():
    """每日盘后定时任务（供 schedule_runner 调用）"""
    today = date.today().strftime("%Y-%m-%d")
    logger.info(f"开始每日复盘: {today}")

    try:
        result = daily_reflection(today)
        logger.info(f"复盘完成: {result['attribution'].get('summary', '')}")
        return result
    except Exception as e:
        logger.error(f"复盘异常: {e}")
        raise


def evaluate_analysis_quality(result: dict, prompt: str = "") -> dict:
    """
    对 LLM 分析结果进行实时质量评估
    评估维度：完整性、一致性、可操作性、置信度
    返回质量评分（0-100）及警告列表
    """
    quality_score = 100
    warnings = []

    # 1. 完整性检查
    if not result.get("plans"):
        quality_score -= 30
        warnings.append("缺少操作计划")
    if not result.get("risks"):
        quality_score -= 10
        warnings.append("缺少风险提示")
    if not result.get("market_outlook"):
        quality_score -= 10
        warnings.append("缺少市场展望")

    # 2. 计划质量检查（每条计划必须有 action + reason）
    plans = result.get("plans", [])
    if plans:
        incomplete_plans = [p for p in plans if not (p.get("action") and p.get("reason"))]
        if incomplete_plans:
            quality_score -= len(incomplete_plans) * 5
            warnings.append(f"{len(incomplete_plans)}条计划缺少操作或理由")

    # 3. 置信度检查
    confidence = result.get("confidence_level", "unknown")
    if confidence == "low":
        quality_score -= 20
        warnings.append("模型自身置信度低")
    elif confidence == "unknown":
        quality_score -= 15
        warnings.append("未提供置信度评估")

    # 4. 错误检查
    if result.get("error"):
        quality_score = 0
        warnings.append(f"LLM 调用错误: {result['error']}")

    quality_score = max(0, min(100, quality_score))

    return {
        "quality_score": quality_score,
        "quality_level": "high" if quality_score >= 80 else "medium" if quality_score >= 50 else "low",  # noqa: E501
        "warnings": warnings,
        "flagged": quality_score < 50,
        "checks": {
            "has_plans": bool(result.get("plans")),
            "has_risks": bool(result.get("risks")),
            "has_outlook": bool(result.get("market_outlook")),
            "has_confidence": confidence != "unknown",
            "has_error": bool(result.get("error")),
        },
    }


def log_quality_to_audit(result: dict, quality: dict, agent_type: str = ""):
    """将质量评估结果写入审计日志"""
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO audit.audit_log
                (event_type, operator, target_type, detail, result)
            VALUES ('QUALITY_ASSESSMENT', 'SYSTEM', 'LLM_OUTPUT', %s, %s)
        """, (
            json.dumps({
                "quality_score": quality["quality_score"],
                "quality_level": quality["quality_level"],
                "warnings": quality["warnings"],
                "checks": quality["checks"],
                "agent_type": agent_type,
                "plan_count": len(result.get("plans", [])),
                "confidence": result.get("confidence_level", "unknown"),
            }, ensure_ascii=False),
            "FLAGGED" if quality["flagged"] else "PASSED",
        ))
        conn.commit()
        logger.info(f"质量评估: {quality['quality_score']}分 ({quality['quality_level']}) "
                    f"{'⚠️已标记' if quality['flagged'] else '✅通过'}")
    except Exception as e:
        logger.warning(f"质量评估日志写入失败: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== 每日复盘 ===")
    result = daily_reflection()
    print(f"日期: {result['trade_date']}")
    print(f"计划数: {result['attribution'].get('total_plans', 0)}")
    print(f"修改数: {result['attribution'].get('modification_count', 0)}")
    print(f"总结: {result['attribution'].get('summary', '')}")
    for insight in result["attribution"].get("insights", []):
        print(f"洞察: {insight}")
