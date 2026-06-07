"""
health_monitor.py — 系统健康监控模块
提供 6 项监控指标 + 告警推送，支持定时检查与仪表盘展示
"""

import logging
import os
import time
import json
from datetime import datetime, date

import psycopg2

logger = logging.getLogger("invest_system.health_monitor")

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",
}

ALERT_THRESHOLDS = {
    "db_conn_ms": 1000,         # 数据库连接超时 (ms)
    "disk_usage_pct": 85,       # 磁盘使用率 (%)
    "api_error_rate": 0.1,      # API 错误率
    "schedule_lag_min": 30,     # 定时任务延迟 (分钟)
    "news_freshness_days": 3,   # 新闻新鲜度 (天)
    "quote_freshness_days": 2,  # 行情新鲜度 (天)
}


def _get_db_conn():
    """获取数据库连接"""
    from pgcrypto_migration import get_credential
    cfg = dict(DB_CONFIG)
    cfg["password"] = get_credential("DB_PASSWORD")
    return psycopg2.connect(**cfg)


def check_db_connectivity() -> dict:
    """
    检查数据库连通性

    Returns:
        {"status": "ok"/"error", "latency_ms": float, "message": str}
    """
    start = time.time()
    try:
        conn = _get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        latency = (time.time() - start) * 1000
        status = "ok" if latency < ALERT_THRESHOLDS["db_conn_ms"] else "slow"
        return {"status": status, "latency_ms": round(latency, 1), "message": f"连接正常 ({latency:.1f}ms)"}  # noqa: E501
    except Exception as e:
        latency = (time.time() - start) * 1000
        return {"status": "error", "latency_ms": round(latency, 1), "message": f"连接失败: {e}"}


def check_disk_usage() -> dict:
    """
    检查磁盘使用率

    Returns:
        {"status": "ok"/"warning"/"critical", "usage_pct": float, "free_gb": float}
    """
    try:
        import shutil
        usage = shutil.disk_usage(os.path.expanduser("~"))
        pct = usage.used / usage.total * 100
        free_gb = usage.free / (1024 ** 3)
        if pct > ALERT_THRESHOLDS["disk_usage_pct"]:
            status = "critical"
        elif pct > 70:
            status = "warning"
        else:
            status = "ok"
        return {"status": status, "usage_pct": round(pct, 1), "free_gb": round(free_gb, 1),
                "message": f"磁盘使用 {pct:.1f}%，剩余 {free_gb:.1f}GB"}
    except Exception as e:
        return {"status": "error", "usage_pct": 0, "free_gb": 0, "message": str(e)}


def check_api_error_rate() -> dict:
    """
    检查 LLM API 错误率（从审计日志中统计）

    Returns:
        {"status": "ok"/"warning", "error_rate": float, "total_calls": int}
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN result = 'ERROR' OR detail::text LIKE '%error%' THEN 1 ELSE 0 END) as errors
            FROM audit.audit_log
            WHERE event_type IN ('LLM_CALL', 'ANALYSIS_COMPLETE', 'QUALITY_ASSESSMENT')
              AND event_time >= NOW() - INTERVAL '24 hours'
        """)
        row = cur.fetchone()
        total = row[0] or 0
        errors = row[1] or 0
        error_rate = errors / total if total > 0 else 0
        status = "warning" if error_rate > ALERT_THRESHOLDS["api_error_rate"] else "ok"
        return {"status": status, "error_rate": round(error_rate, 3), "total_calls": total,
                "message": f"24h 内 {total} 次调用，错误率 {error_rate:.1%}"}
    except Exception as e:
        return {"status": "error", "error_rate": 0, "total_calls": 0, "message": str(e)}
    finally:
        conn.close()


def check_schedule_health() -> dict:
    """
    检查定时任务健康状态（最近一次执行时间）

    Returns:
        {"status": "ok"/"warning", "last_run": str, "lag_minutes": float}
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT event_type, MAX(event_time)
            FROM audit.audit_log
            WHERE event_type IN ('SCHEDULED_MORNING_RUN', 'SCHEDULED_EVENING_RUN', 'DAILY_REFLECTION')
            GROUP BY event_type
            ORDER BY 2 DESC
        """)
        rows = cur.fetchall()
        if not rows:
            return {"status": "warning", "last_run": None, "lag_minutes": 0,
                    "message": "无定时任务执行记录"}

        latest_type, latest_time = rows[0]
        lag = (datetime.now() - latest_time).total_seconds() / 60 if latest_time else 0
        status = "warning" if lag > ALERT_THRESHOLDS["schedule_lag_min"] else "ok"
        return {"status": status, "last_run": latest_time.isoformat() if latest_time else None,
                "lag_minutes": round(lag, 1), "last_type": latest_type,
                "message": f"最近任务: {latest_type} ({lag:.0f}分钟前)"}
    except Exception as e:
        return {"status": "error", "last_run": None, "lag_minutes": 0, "message": str(e)}
    finally:
        conn.close()


def check_news_freshness() -> dict:
    """
    检查新闻数据新鲜度

    Returns:
        {"status": "ok"/"warning", "latest_date": str, "age_days": int}
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT MAX(publish_date) FROM news.news_articles")
        row = cur.fetchone()
        latest = row[0] if row and row[0] else None
        if not latest:
            return {"status": "warning", "latest_date": None, "age_days": -1,
                    "message": "无新闻数据"}
        age = (date.today() - latest).days
        status = "warning" if age > ALERT_THRESHOLDS["news_freshness_days"] else "ok"
        return {"status": status, "latest_date": latest.isoformat(), "age_days": age,
                "message": f"最新新闻: {latest} ({age}天前)"}
    except Exception as e:
        return {"status": "error", "latest_date": None, "age_days": -1, "message": str(e)}
    finally:
        conn.close()


def check_quote_freshness() -> dict:
    """
    检查行情数据新鲜度

    Returns:
        {"status": "ok"/"warning", "latest_date": str, "age_days": int}
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT MAX(trade_date) FROM market.daily_quotes")
        row = cur.fetchone()
        latest = row[0] if row and row[0] else None
        if not latest:
            return {"status": "warning", "latest_date": None, "age_days": -1,
                    "message": "无行情数据"}
        age = (date.today() - latest).days
        status = "warning" if age > ALERT_THRESHOLDS["quote_freshness_days"] else "ok"
        return {"status": status, "latest_date": latest.isoformat(), "age_days": age,
                "message": f"最新行情: {latest} ({age}天前)"}
    except Exception as e:
        return {"status": "error", "latest_date": None, "age_days": -1, "message": str(e)}
    finally:
        conn.close()


def run_health_check() -> dict:
    """
    运行全量健康检查

    Returns:
        {
            "timestamp": str,
            "overall": "healthy"/"warning"/"critical",
            "checks": {...},
            "alerts": [...]
        }
    """
    checks = {
        "db_connectivity": check_db_connectivity(),
        "disk_usage": check_disk_usage(),
        "api_error_rate": check_api_error_rate(),
        "schedule_health": check_schedule_health(),
        "news_freshness": check_news_freshness(),
        "quote_freshness": check_quote_freshness(),
    }

    alerts = []
    for name, check in checks.items():
        if check["status"] in ("warning", "error", "critical", "slow"):
            alerts.append({
                "check": name,
                "status": check["status"],
                "message": check.get("message", ""),
            })

    critical_count = sum(1 for a in alerts if a["status"] in ("critical", "error"))
    warning_count = sum(1 for a in alerts if a["status"] == "warning")

    if critical_count > 0:
        overall = "critical"
    elif warning_count > 0:
        overall = "warning"
    else:
        overall = "healthy"

    result = {
        "timestamp": datetime.now().isoformat(),
        "overall": overall,
        "checks": checks,
        "alerts": alerts,
    }

    # 告警写入审计日志
    if alerts:
        _log_alerts(alerts)

    return result


def _log_alerts(alerts: list[dict]):
    """将告警写入审计日志"""
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO audit.audit_log (event_type, operator, target_type, detail, result)
            VALUES ('HEALTH_ALERT', 'SYSTEM', 'MONITOR', %s, %s)
        """, (
            json.dumps({"alerts": alerts}, ensure_ascii=False),
            "WARNING",
        ))
        conn.commit()
    except Exception as e:
        logger.warning(f"告警日志写入失败: {e}")
    finally:
        conn.close()


def get_health_summary() -> dict:
    """获取健康检查摘要（供仪表盘使用）"""
    result = run_health_check()
    return {
        "overall": result["overall"],
        "timestamp": result["timestamp"],
        "alert_count": len(result["alerts"]),
        "checks": {
            name: {"status": check["status"], "message": check.get("message", "")}
            for name, check in result["checks"].items()
        },
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    health = run_health_check()
    print(f"系统状态: {health['overall']}")
    for name, check in health["checks"].items():
        icon = {"ok": "✅", "warning": "⚠️", "error": "❌", "critical": "🚨", "slow": "🐢"}.get(check["status"], "❓")  # noqa: E501
        print(f"  {icon} {name}: {check.get('message', '')}")