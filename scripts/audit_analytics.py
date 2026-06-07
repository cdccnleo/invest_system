"""
audit_analytics.py — 审计日志驱动的洞察分析
从 audit.audit_log 中挖掘投资行为模式
"""

import logging
from datetime import date, timedelta

import psycopg2
from pgcrypto_migration import get_credential

logger = logging.getLogger("invest_system.audit_analytics")

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",  # 运行时由 _get_password() 注入
}


def _get_password():
    return get_credential("DB_PASSWORD")

def get_db_conn():
    cfg = dict(DB_CONFIG)
    cfg["password"] = _get_password()
    return psycopg2.connect(**cfg)


# ── 行为模式挖掘 ──────────────────────────────────────────────────────

def analyze_trading_behavior(days: int = 30) -> dict:
    """
    分析近 N 天的交易行为模式
    返回: {pattern_name: description, metrics: {...}}
    """
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT event_type, COUNT(*) as cnt
            FROM audit.audit_log
            WHERE event_time >= CURRENT_DATE - (INTERVAL '1 day' * %s)
            GROUP BY event_type
            ORDER BY cnt DESC
        """, (days,))
        event_counts = {r[0]: r[1] for r in cur.fetchall()}

        # 分析用户修改频率
        cur.execute("""
            SELECT DATE(event_time), COUNT(*)
            FROM audit.audit_log
            WHERE event_type = 'USER_MODIFY_PLAN'
              AND event_time >= CURRENT_DATE - (INTERVAL '1 day' * %s)
            GROUP BY DATE(event_time)
            ORDER BY DATE(event_time)
        """, (days,))
        mod_by_day = [{"date": r[0].isoformat(), "count": r[1]} for r in cur.fetchall()]

        # 分析连续修改日
        consecutive_mod_days = 0
        max_consecutive = 0
        for item in mod_by_day:
            if item["count"] > 0:
                consecutive_mod_days += 1
                max_consecutive = max(max_consecutive, consecutive_mod_days)
            else:
                consecutive_mod_days = 0

        # 分析置信度变化趋势
        cur.execute("""
            SELECT DATE(event_time),
                   SUM(CASE WHEN result = 'SUCCESS' THEN 1 ELSE 0 END) as success,
                   SUM(CASE WHEN result = 'FAILED' THEN 1 ELSE 0 END) as failed
            FROM audit.audit_log
            WHERE event_type IN ('ANALYSIS_COMPLETE', 'SCHEDULED_MORNING_RUN', 'SKILL_EXECUTED')
              AND event_time >= CURRENT_DATE - (INTERVAL '1 day' * %s)
            GROUP BY DATE(event_time)
            ORDER BY DATE(event_time)
        """, (days,))
        quality_rows = cur.fetchall()
        total_success = sum(r[1] for r in quality_rows)
        total_failed = sum(r[2] for r in quality_rows)
        success_rate = total_success / (total_success + total_failed) if (total_success + total_failed) > 0 else 0

        # 行为模式判断
        patterns = []
        if max_consecutive >= 3:
            patterns.append(f"连续{max_consecutive}天修改AI计划（激进型投资者）")
        if success_rate < 0.6:
            patterns.append(f"AI计划采纳率仅{success_rate:.0%}（可能需要调整Prompt）")
        if event_counts.get("SCHEDULED_MORNING_RUN", 0) >= 20:
            patterns.append("每日定时分析已成习惯（成熟投资者）")
        if event_counts.get("SKILL_EXECUTED", 0) > 5:
            patterns.append(f"已使用{event_counts.get('SKILL_EXECUTED', 0)}次技能（技能驱动型）")
        if not patterns:
            patterns.append("行为模式稳定，以观察和学习为主")

        return {
            "period_days": days,
            "event_counts": event_counts,
            "modifications_per_day": mod_by_day,
            "max_consecutive_mod_days": max_consecutive,
            "analysis_success_rate": round(success_rate * 100, 1),
            "behavior_patterns": patterns,
            "total_analysis_runs": sum(r[1] + r[2] for r in quality_rows),
            "recommendations": _generate_recommendations(patterns, success_rate),
        }

    finally:
        conn.close()


def _generate_recommendations(patterns: list, success_rate: float) -> list[str]:
    """基于行为模式生成建议"""
    recs = []
    if any("激进" in p for p in patterns):
        recs.append("建议适当减少修改频率，给AI计划更多信任空间")
    if success_rate < 0.6:
        recs.append("当前AI计划采纳率偏低，建议复盘时关注修改原因，减少过度干预")
    if any("技能" in p for p in patterns):
        recs.append("技能驱动模式良好，可考虑固化更多高频任务为技能")
    if not recs:
        recs.append("当前行为模式健康，维持现有节奏")
    return recs


# ── 月度统计报告 ──────────────────────────────────────────────────────

def monthly_report(year_month: str = None) -> dict:
    """
    生成月度审计报告
    year_month: "YYYY-MM" 格式，默认上月
    """
    if year_month is None:
        today = date.today()
        year_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COUNT(DISTINCT DATE(event_time)) as active_days,
                COUNT(*) as total_events,
                COUNT(DISTINCT CASE WHEN event_type LIKE 'SCHEDULED%%' THEN event_type END) as scheduled_runs,
                COUNT(DISTINCT CASE WHEN event_type = 'USER_MODIFY_PLAN' THEN 1 END) as user_modifications,
                COUNT(DISTINCT CASE WHEN event_type = 'SKILL_APPROVED' THEN 1 END) as skills_approved,
                COUNT(DISTINCT CASE WHEN event_type LIKE 'ANALYSIS%%' AND result = 'SUCCESS' THEN 1 END) as successful_analyses
            FROM audit.audit_log
            WHERE event_time >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month')
              AND event_time < DATE_TRUNC('month', CURRENT_DATE)
        """)
        row = cur.fetchone()

        if not row or len(row) < 6:
            return {
                "year_month": year_month,
                "active_days": 0, "total_events": 0,
                "scheduled_runs": 0, "user_modifications": 0,
                "skills_approved": 0, "successful_analyses": 0,
                "auto_adoption_rate": 100.0,
            }

        return {
            "year_month": year_month,
            "active_days": row[0] or 0,
            "total_events": row[1] or 0,
            "scheduled_runs": row[2] or 0,
            "user_modifications": row[3] or 0,
            "skills_approved": row[4] or 0,
            "successful_analyses": row[5] or 0,
            "auto_adoption_rate": round(
                (1 - (row[3] or 0) / max(row[2] or 1, 1)) * 100, 1
            ),
        }
    finally:
        conn.close()


# ── 投资行为年度报告 ──────────────────────────────────────────────────

def annual_report(year: int = None) -> dict:
    """
    生成年度投资行为分析报告
    """
    if year is None:
        year = date.today().year

    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE event_type LIKE 'SCHEDULED%%') as total_runs,
                COUNT(*) FILTER (WHERE event_type = 'USER_MODIFY_PLAN') as total_mods,
                COUNT(*) FILTER (WHERE event_type = 'SKILL_APPROVED') as skills_approved,
                COUNT(*) FILTER (WHERE event_type LIKE 'ANALYSIS%%' AND result = 'SUCCESS') as success_count,
                COUNT(*) FILTER (WHERE event_type = 'DAILY_REFLECTION') as reflections,
                COUNT(*) FILTER (WHERE event_type = 'SKILL_VALIDATED') as skill_validations
            FROM audit.audit_log
            WHERE EXTRACT(YEAR FROM event_time) = %s
        """, (year,))
        row = cur.fetchone()

        if not row or len(row) < 6:
            return {"year": year, "total_scheduled_runs": 0, "total_modifications": 0,
                    "auto_adoption_rate": 100.0, "skills_approved": 0, "successful_analyses": 0,
                    "reflections_completed": 0, "skill_validations": 0, "monthly_runs": [],
                    "maturity_score": 0.0}

        total_runs = row[0] or 0
        total_mods = row[1] or 0
        adoption_rate = (1 - total_mods / total_runs) * 100 if total_runs > 0 else 0

        # 按月统计
        cur.execute("""
            SELECT EXTRACT(MONTH FROM event_time) as month,
                   COUNT(*) FILTER (WHERE event_type LIKE 'SCHEDULED%%') as runs
            FROM audit.audit_log
            WHERE EXTRACT(YEAR FROM event_time) = %s
              AND event_type LIKE 'SCHEDULED%%'
            GROUP BY EXTRACT(MONTH FROM event_time)
            ORDER BY month
        """, (year,))
        monthly_runs = [{"month": int(r[0]), "runs": r[1]} for r in cur.fetchall()]

        return {
            "year": year,
            "total_scheduled_runs": total_runs,
            "total_modifications": total_mods,
            "auto_adoption_rate": round(adoption_rate, 1),
            "skills_approved": row[2] or 0,
            "successful_analyses": row[3] or 0,
            "reflections_completed": row[4] or 0,
            "skill_validations": row[5] or 0,
            "monthly_runs": monthly_runs,
            "maturity_score": _calc_maturity_score(total_runs, total_mods, row[2] or 0, row[4] or 0),
        }
    finally:
        conn.close()


def _calc_maturity_score(runs: int, mods: int, skills: int, reflections: int) -> float:
    """计算机系统成熟度评分（0-100）"""
    score = 0
    # 运行频率得分（最高30分）
    score += min(runs / 30, 30)  # 假设每天1次，30天满分
    # 自我修正率（最高30分）
    mod_rate = mods / runs if runs > 0 else 1
    score += max(30 - mod_rate * 30, 0)
    # 技能积累（最高20分）
    score += min(skills * 5, 20)
    # 复盘习惯（最高20分）
    score += min(reflections * 5, 20)
    return round(min(score, 100), 1)


# ── 推送洞察报告 ──────────────────────────────────────────────────────

def send_behavior_insights_report(days: int = 7) -> str:
    """生成并推送近N天行为洞察摘要"""
    behavior = analyze_trading_behavior(days)

    report_lines = [
        f"📊 InvestPilot 行为洞察（近{days}天）",
        "",
        f"运行次数: {behavior['total_analysis_runs']} 次",
        f"AI计划采纳率: {behavior['analysis_success_rate']}%",
        f"连续修改天数: {behavior['max_consecutive_mod_days']} 天",
        "",
        "📈 行为模式:",
    ]
    for pattern in behavior["behavior_patterns"]:
        report_lines.append(f"  • {pattern}")

    if behavior["recommendations"]:
        report_lines.append("")
        report_lines.append("💡 建议:")
        for rec in behavior["recommendations"]:
            report_lines.append(f"  • {rec}")

    return "\n".join(report_lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== 行为分析（近7天）===")
    r = analyze_trading_behavior(7)
    print(f"分析次数: {r['total_analysis_runs']}")
    print(f"AI采纳率: {r['analysis_success_rate']}%")
    print(f"行为模式: {r['behavior_patterns']}")

    print("\n=== 月度报告（上月）===")
    m = monthly_report()
    print(f"活跃天数: {m['active_days']}")
    print(f"定时运行: {m['scheduled_runs']} 次")
    print(f"AI采纳率: {m['auto_adoption_rate']}%")

    print("\n=== 行为洞察推送内容 ===")
    print(send_behavior_insights_report(7))
