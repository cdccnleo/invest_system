"""
corporate_actions.py — 公司行为处理模块
自动处理：除权除息、送股、分红、配股
数据源：东方财富数据中心
"""

import logging
import time
from datetime import date, timedelta

import psycopg2
import urllib.request

logger = logging.getLogger("invest_system.corp_actions")

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",  # 运行时由 _get_password() 注入,
}
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.eastmoney.com/",
}


def _get_password():
    from pgcrypto_migration import get_credential
    return get_credential("DB_PASSWORD")

def get_db_conn():
    cfg = dict(DB_CONFIG)
    cfg["password"] = _get_password()
    return psycopg2.connect(**cfg)


def fetch_dividends(ts_code: str) -> list[dict]:
    """
    获取股票除权除息信息（东方财富 API）
    ts_code格式: 300059.XSHE
    """
    code, market = ts_code.split(".")

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "sortColumns": "EX_DIVIDEND_DATE",
        "sortTypes": "-1",
        "pageSize": 10,
        "pageNumber": 1,
        "reportName": "RPT_SHAREHOLDER_ALLOT_DETAILS",
        "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,EX_DIVIDEND_DATE,DIVIDEND_RATIO_BEFORE,DIVIDEND_RATIO_AFTER,BONUS_IT_RATIO,ALLOT_PRICE,ALLOT_RATIO",  # noqa: E501
        "filter": f'(SECURITY_CODE="{code}")',
    }

    from urllib.parse import urlencode
    try:
        req = urllib.request.Request(f"{url}?{urlencode(params)}", headers=EM_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = __import__("json").loads(resp.read().decode("utf-8"))
            results = data.get("result", {}).get("data", []) or []
            actions = []
            for r in results:
                ex_date = r.get("EX_DIVIDEND_DATE", "")
                if not ex_date:
                    continue
                div_ratio = r.get("DIVIDEND_RATIO_BEFORE", 0) or 0
                bonus_ratio = r.get("BONUS_IT_RATIO", 0) or 0
                allot_price = r.get("ALLOT_PRICE", 0) or 0
                allot_ratio = r.get("ALLOT_RATIO", 0) or 0

                actions.append({
                    "ts_code": ts_code,
                    "action_date": ex_date,
                    "dividend_per_share": div_ratio,
                    "bonus_shares_ratio": bonus_ratio,
                    "allot_price": allot_price,
                    "allot_ratio": allot_ratio,
                    "type": _classify_action(div_ratio, bonus_ratio, allot_ratio),
                })
            return actions
    except Exception as e:
        logger.warning(f"获取除权信息失败 {ts_code}: {e}")
        return []


def _classify_action(div: float, bonus: float, allot: float) -> str:
    if allot > 0:
        return "配股"
    elif bonus > 0:
        return "送股"
    elif div > 0:
        return "分红"
    return "其他"


def apply_dividend_adjustment(conn, ts_code: str, dividend_per_share: float, ex_date: str):
    """
    处理除息：更新持仓成本 = 原成本 - 每股分红
    """
    cur = conn.cursor()
    try:
        # 检查是否已处理过
        cur.execute("""
            SELECT id FROM audit.audit_log
            WHERE event_type = 'CORPORATE_ACTION'
              AND detail->>'ts_code' = %s
              AND detail->>'action_date' = %s
            LIMIT 1
        """, (ts_code, ex_date))
        if cur.fetchone():
            logger.info(f"{ts_code} {ex_date} 已处理过，跳过")
            return

        # 写入持仓调整记录（这里仅记录，不直接修改 CSV）
        # 实际应用需要更新 positions.csv 或数据库持仓表
        cur.execute("""
            INSERT INTO audit.audit_log
                (event_type, operator, target_type, target_id, detail, result)
            VALUES ('CORPORATE_ACTION', 'SYSTEM', 'HOLDING', %s,
                    %s, 'SUCCESS')
        """, (ts_code, __import__("json").dumps({
            "action": "DIVIDEND_ADJUST",
            "ts_code": ts_code,
            "action_date": ex_date,
            "dividend_per_share": dividend_per_share,
            "adjustment": f"成本下调 {dividend_per_share:.4f} 元/股",
        })))
        conn.commit()
        logger.info(f"已记录除息调整: {ts_code} {ex_date} 分红 {dividend_per_share}")
    except Exception as e:
        logger.warning(f"除息调整记录失败: {e}")


def check_pending_corporate_actions(ts_codes: list[str]) -> list[dict]:
    """
    批量检查持仓股近期公司行为
    返回待处理行为列表
    """
    today = date.today()
    pending = []

    for ts_code in ts_codes:
        actions = fetch_dividends(ts_code)
        for action in actions:
            action_date = action["action_date"]
            if isinstance(action_date, str):
                try:
                    action_date = date.fromisoformat(action_date[:10])
                except Exception:
                    continue

            # 仅关注未来或近期（7天内）的行为
            if action_date >= today - timedelta(days=7):
                pending.append(action)

        time.sleep(0.5)  # 避免过快请求

    return pending


def get_cash_dividends(ts_code: str, shares: float) -> list[dict]:
    """
    计算持仓分红收益
    """
    actions = fetch_dividends(ts_code)
    dividends = []
    for a in actions:
        if a["type"] == "分红" and a["dividend_per_share"] > 0:
            total = shares * a["dividend_per_share"]
            dividends.append({
                "date": a["action_date"],
                "dividend_per_share": a["dividend_per_share"],
                "total_amount": round(total, 2),
                "shares": shares,
            })
    return dividends


class CorporateActionHandler:
    """公司行为处理器（简化版）"""

    def __init__(self, positions: list[dict]):
        self.positions = {p["code"]: p for p in positions}

    def check_pending(self) -> list[dict]:
        """检查待处理公司行为"""
        ts_codes = [f"{p['code']}.{'XSHG' if p['code'].startswith(('6','5')) else 'XSHE'}"
                    for p in self.positions]
        return check_pending_corporate_actions(ts_codes)

    def generate_notifications(self) -> list[str]:
        """生成公司行为提醒"""
        pending = self.check_pending()
        notifications = []
        for p in pending:
            code = p["ts_code"].split(".")[0]
            name = self.positions.get(code, {}).get("name", code)
            action_type = p["type"]
            action_date = p["action_date"]
            notifications.append(
                f"{action_date} {name}({code}) {action_type}："
                f"{'每股分红 ¥' + str(p.get('dividend_per_share', '')) if action_type == '分红' else action_type}"  # noqa: E501
            )
        return notifications


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 测试
    actions = fetch_dividends("300059.XSHE")
    print(f"东方财富(300059) 公司行为: {len(actions)} 条")
    for a in actions:
        print(f"  {a}")
