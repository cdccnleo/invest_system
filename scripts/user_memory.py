"""
user_memory.py — USER.md / MEMORY.md 自动生成与更新
基于 PostgreSQL 中的 profile + memory schema 数据
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import psycopg2
from pgcrypto_migration import get_credential

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
USER_MD_PATH = os.environ.get("USER_MD_PATH", str(PROJECT_ROOT / "USER.md"))
MEMORY_MD_PATH = os.environ.get("MEMORY_MD_PATH", str(PROJECT_ROOT / "MEMORY.md"))

logger = logging.getLogger("invest_system.user_memory")

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


# ── USER.md 生成 ──────────────────────────────────────────────────────────

def generate_user_profile_summary() -> str:
    """
    从 PostgreSQL profile.user_profile 读取用户画像，生成 USER.md
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT risk_tolerance, max_single_position_pct, max_sector_position_pct,
                   max_daily_loss_pct, investment_goal, updated_at
            FROM profile.user_profile
            ORDER BY updated_at DESC
            LIMIT 1
        """)
        row = cur.fetchone()
    except Exception as e:
        logger.warning(f"读取 user_profile 失败: {e}")
        row = None
    finally:
        conn.close()

    if not row:
        return _default_user_md()

    risk_tol, max_single, max_sector, max_daily, goal, updated = row
    updated_str = updated.strftime("%Y-%m-%d %H:%M") if updated else "未知"

    # 读取持仓偏好
    positions_summary = _get_positions_summary()

    return f"""# USER.md — 个人投资画像
> 自动生成于 {now_str}，上次更新: {updated_str}

## 投资风格
- **风险偏好**: {risk_tol or 'medium'}
- **单股仓位上限**: {max_single or 20}%
- **单行业仓位上限**: {max_sector or 30}%
- **单日最大亏损容忍**: {max_daily or 5}%
- **投资目标**: {goal or '资产稳健增值'}

## 持仓概况
{positions_summary}

## 近期操作特征
- 偏好科技/半导体赛道（高仓位占比）
- 对亏损容忍度低，盈利时倾向于减持
- 注重仓位集中度管理，设有 20% 单股上限

## 禁忌事项
- 不得建议单股仓位超过 20%
- 科创板/创业板不加杠杆
- 宏观风险事件期间不主动加仓
- 不追涨停板

---
*本文件由系统自动生成，内容以 PostgreSQL profile.user_profile 为准*
"""


def _get_positions_summary() -> str:
    """获取近期持仓特征摘要"""
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        # 读取最新持仓（从 audit log 或 market.daily_quotes）
        cur.execute("""
            SELECT COUNT(DISTINCT ts_code), MAX(trade_date)
            FROM market.daily_quotes
            WHERE trade_date = CURRENT_DATE
        """)
        row = cur.fetchone()
        count, latest_date = row if row else (0, "无数据")

        cur.execute("""
            SELECT COUNT(DISTINCT ts_code), COUNT(*)
            FROM market.daily_quotes
            WHERE trade_date >= CURRENT_DATE - INTERVAL '5 days'
        """)
        row2 = cur.fetchone()
        active, total = row2 if row2 else (0, 0)

        return f"- 持仓数量: {count or 0} 只\n- 活跃标的(近5日): {active or 0} 条\n- 最新数据日期: {latest_date or '无'}"
    except Exception as e:
        logger.warning(f"持仓摘要读取失败: {e}")
        return "- 暂无数据"
    finally:
        conn.close()


def _default_user_md() -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""# USER.md — 个人投资画像
> 自动生成于 {now_str}

## 投资风格
- **风险偏好**: medium（中等）
- **单股仓位上限**: 20%
- **单行业仓位上限**: 30%
- **单日最大亏损容忍**: 5%
- **投资目标**: 资产稳健增值

## 持仓概况
- 暂无持仓数据

## 禁忌事项
- 不得建议单股仓位超过 20%
- 科创板/创业板不加杠杆
- 宏观风险事件期间不主动加仓
- 不追涨停板

---
*本文件由系统自动生成*
"""


# ── MEMORY.md 生成 ─────────────────────────────────────────────────────────

def generate_meta_memories() -> str:
    """
    从 PostgreSQL memory schema 读取历史决策与归因，生成 MEMORY.md
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_db_conn()
    cur = conn.cursor()

    try:
        # 读取近 30 条决策结果
        cur.execute("""
            SELECT event_time, target_type, detail, result
            FROM audit.audit_log
            WHERE event_type IN ('ANALYSIS_COMPLETE', 'USER_MODIFY_PLAN', 'DECISION_OUTCOME')
            ORDER BY event_time DESC
            LIMIT 30
        """)
        audit_rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"读取 audit_log 失败: {e}")
        audit_rows = []
    finally:
        conn.close()

    # 按月份分组
    by_month = {}
    for event_time, target_type, detail_json, result in audit_rows:
        if event_time is None:
            continue
        month_key = event_time.strftime("%Y-%m")
        by_month.setdefault(month_key, []).append((event_time, target_type, detail_json, result))

    sections = []
    for month in sorted(by_month.keys(), reverse=True)[:6]:
        entries = by_month[month]
        section_lines = [f"### {month}", ""]
        for event_time, target_type, detail_json, result in entries[:10]:
            try:
                detail = json.loads(detail_json) if detail_json else {}
            except Exception:
                detail = {}

            ts = event_time.strftime("%m-%d %H:%M")
            result_icon = "✅" if result == "SUCCESS" else "❌" if result == "FAILED" else "⚠️"
            section_lines.append(f"- {ts} {result_icon} [{target_type}] {json.dumps(detail, ensure_ascii=False)[:80]}")

        sections.append("\n".join(section_lines))

    if not sections:
        return _default_memory_md()

    body = "\n\n".join(sections)

    return f"""# MEMORY.md — 系统化策略演化史
> 自动生成于 {now_str}

## 近期决策历史

{body}

## 策略教训（从审计日志提炼）
- 暂无足够数据生成教训摘要（系统需运行至少 30 天）

## 改进方向
- 持续跟踪 Phase 2 执行质量
- 根据用户修正行为优化 Prompt 模板

---
*本文件由系统自动生成，内容以 PostgreSQL audit.audit_log 为准*
"""


def _default_memory_md() -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""# MEMORY.md — 系统化策略演化史
> 自动生成于 {now_str}

## 近期决策历史
- 系统刚启动，尚无历史数据

## 策略教训
- 系统需运行至少 30 天后才能生成有意义的教训摘要

## 改进方向
- Phase 2 刚启动，持续监控数据积累

---
*本文件由系统自动生成*
"""


# ── 写文件 ────────────────────────────────────────────────────────────────

def update_user_memory_files():
    """更新 USER.md 和 MEMORY.md"""
    logger.info("更新 USER.md 和 MEMORY.md...")

    user_md = generate_user_profile_summary()
    with open(USER_MD_PATH, "w", encoding="utf-8") as f:
        f.write(user_md)
    logger.info(f"USER.md 已更新 ({len(user_md)} chars)")

    memory_md = generate_meta_memories()
    with open(MEMORY_MD_PATH, "w", encoding="utf-8") as f:
        f.write(memory_md)
    logger.info(f"MEMORY.md 已更新 ({len(memory_md)} chars)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    update_user_memory_files()
