"""
盘中实时异动关联分析器
当持仓标的出现异动时，关联分析同行业/同概念/供应链相关持仓
避免单一标的异动告警遗漏关联机会
"""

import sys
from pathlib import Path
from datetime import datetime, date
import json
import logging

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from storage_factory import get_storage
from pgcrypto_migration import load_positions_from_db

COOLDOWN_FILE = Path("logs/alert_cooldown.json")
COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)

# 行业关联映射（静态，可扩展为DB配置）
SAME_INDUSTRY_THRESHOLD = 0.05  # 5%涨跌阈值
LINKED_PEERS = {
    "芯片/半导体": [" semiconductor peers..."],
    "新能源车": [" related EV供应链..."],
    "医药": [" related pharma..."],
}

def load_cooldown() -> dict:
    if COOLDOWN_FILE.exists():
        return json.loads(COOLDOWN_FILE.read_text())
    return {}

def save_cooldown(data: dict):
    COOLDOWN_FILE.write_text(json.dumps(data, ensure_ascii=False))

def get_cooldown(key: str, cooldown_seconds: int = 3600) -> bool:
    """检查是否在冷却期内，是返回True"""
    cooldown = load_cooldown()
    last_alert = cooldown.get(key, 0)
    if datetime.now().timestamp() - last_alert < cooldown_seconds:
        return True
    return False

def set_cooldown(key: str):
    cooldown = load_cooldown()
    cooldown[key] = datetime.now().timestamp()
    save_cooldown(cooldown)

def get_holding_industries() -> dict[str, list[dict]]:
    """获取持仓行业分布"""
    positions = load_positions_from_db()
    industries = {}
    for p in positions:
        industry = p.get("industry", "未知")
        if industry not in industries:
            industries[industry] = []
        industries[industry].append(p)
    return industries

def detect_unusual_movement(ts_code: str, change_pct: float) -> bool:
    """检测是否异动（涨跌幅超过阈值）"""
    return abs(change_pct) >= SAME_INDUSTRY_THRESHOLD * 100

def find_linked_positions(ts_code: str, industry: str, all_positions: list[dict]) -> list[dict]:
    """找出同行业/同概念关联持仓"""
    linked = []
    for p in all_positions:
        if (p.get("industry") == industry or p.get("concept") == industry) and p.get("code") != ts_code:
            linked.append(p)
    return linked

def scan_linked_alerts() -> list[dict]:
    """
    扫描持仓异动 + 关联持仓告警
    返回需要发送的告警列表
    """
    positions = load_positions_from_db()
    industries = get_holding_industries()

    # 从数据库读取今日行情（已接入）
    try:
        storage = get_storage()
        conn = storage._pg_conn
        cur = conn.cursor()

        # 获取今日有异动的持仓
        codes = [p.get("code") for p in positions]
        placeholders = ",".join(["%s"] * len(codes))
        cur.execute(f"""
            SELECT ts_code, close_price, change_pct, trade_date
            FROM market.daily_quotes
            WHERE ts_code IN ({placeholders})
              AND trade_date = CURRENT_DATE
              AND ABS(change_pct) >= %s
            ORDER BY ABS(change_pct) DESC
        """, codes + [SAME_INDUSTRY_THRESHOLD * 100])

        alerts = []
        for row in cur.fetchall():
            ts_code = row[0]
            # row[2] = change_pct (corrected from original)
            change_pct = float(row[2]) if row[2] else 0
            # Get position data
            pos = next((p for p in positions if p.get("code") == ts_code), {})

            # 检查是否冷却
            key = f"alert_{ts_code}"
            if get_cooldown(key, cooldown_seconds=3600):
                continue

            # 找关联持仓
            industry = pos.get("industry", "未知")
            linked = find_linked_positions(ts_code, industry, positions)

            alert = {
                "ts_code": ts_code,
                "name": pos.get("name", ts_code),
                "change_pct": change_pct,
                "industry": industry,
                "linked": [{"code": p.get("code"), "name": p.get("name")} for p in linked],
                "time": datetime.now().strftime("%H:%M:%S"),
            }
            alerts.append(alert)
            set_cooldown(key)

        cur.close()
        storage.close()
        return alerts
    except Exception as e:
        logging.getLogger("intraday_alert").error(f"扫描异动失败: {e}")
        return []

def job_intraday_linked_alert():
    """
    盘中异动关联扫描任务
    每30分钟执行一次（schedule_runner）
    非交易时段直接返回，不产生无效扫描
    """
    # ── 交易日守卫 ────────────────────────────────────────────────────
    try:
        from chinese_calendar import is_holiday
        from datetime import date
        if is_holiday(date.today()):
            logging.getLogger("intraday_alert").debug("非交易日，跳过关联扫描")
            return {"skipped": "non_trading_day"}
    except Exception:
        pass  # 保守策略，异常时继续执行

    # ── 交易时段守卫（9:30-11:30 / 13:00-15:00）────────────────────────
    now = datetime.now()
    morning_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    morning_end = now.replace(hour=11, minute=30, second=0, microsecond=0)
    afternoon_start = now.replace(hour=13, minute=0, second=0, microsecond=0)
    afternoon_end = now.replace(hour=15, minute=0, second=0, microsecond=0)
    is_trading = (morning_start <= now <= morning_end) or (afternoon_start <= now <= afternoon_end)
    if not is_trading:
        logging.getLogger("intraday_alert").debug(f"非交易时段，跳过关联扫描 ({now.strftime('%H:%M')})")
        return {"skipped": "non_trading_hours"}

    logging.getLogger("intraday_alert").info("盘中异动关联扫描触发")
    from notification import send_linked_alert

    alerts = scan_linked_alerts()
    if not alerts:
        logging.getLogger("intraday_alert").debug("本次扫描无关联异动")
        return {"scanned": 0, "alerts": 0}

    for alert in alerts:
        send_linked_alert(alert)

    logging.getLogger("intraday_alert").info(f"关联异动告警已发送: {len(alerts)} 个")
    return {"scanned": len(alerts), "alerts": len(alerts)}