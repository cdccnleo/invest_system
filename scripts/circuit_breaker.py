"""
circuit_breaker.py — 熔断机制
三大触发规则：单日亏损 / 单股熔断 / 大盘熔断
触发后：暂停所有买入，转为"仅观察"模式
"""

import os, json, logging
from datetime import date, datetime
from typing import Optional

import psycopg2

try:
    from credentials import get_credential
    _HAS_CRED = True
except ImportError:
    _HAS_CRED = False

logger = logging.getLogger("invest_system.circuit_breaker")

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "user": os.environ.get("DB_USER", "invest_admin"),
    "database": os.environ.get("DB_NAME", "investpilot"),
    "password": get_credential("DB_PASSWORD") if _HAS_CRED else os.environ.get("DB_PASSWORD", ""),
}
POSITIONS_CSV = os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv")


def get_db_conn():
    return psycopg2.connect(**DB_CONFIG)


# ── 熔断规则配置 ──────────────────────────────────────────────────────────

CIRCUIT_BREAKER_RULES = {
    # 单日总市值下跌超 N%，暂停所有买入
    "daily_max_loss_pct": -5.0,

    # 单只持仓股日内跌幅超 N%，该股触发熔断
    "single_stock_drop": -8.0,

    # 连续 N 个交易日亏损，降低仓位上限至 50%
    "consecutive_loss_days": 3,

    # 沪深300跌幅超 N%，所有买入计划转为"仅观察"
    "market_crash": -3.0,
}


# ── 熔断状态 ─────────────────────────────────────────────────────────────

class CircuitBreakerStatus:
    """熔断器状态"""

    NORMAL = "NORMAL"          # 正常运行
    SINGLE_STOCK_BROKEN = "SINGLE_STOCK_BROKEN"  # 单股熔断
    PORTFOLIO_BROKEN = "PORTFOLIO_BROKEN"        # 组合熔断
    MARKET_BROKEN = "MARKET_BROKEN"              # 大盘熔断
    COOLDOWN = "COOLDOWN"                        # 冷却中


class CircuitBreaker:
    """熔断器"""

    def __init__(self):
        self.status = CircuitBreakerStatus.NORMAL
        self.broken_stocks: set[str] = set()   # 触发熔断的股票代码
        self.cooldown_until: Optional[datetime] = None
        self.reason: str = ""

    def check(self, quotes: list[dict], indices: list[dict]) -> CircuitBreakerStatus:
        """
        全面检查熔断条件
        quotes: 当日行情 [{ts_code, close, prev_close, change_pct}]
        indices: 当日指数 [{index_code, close, change_pct}]
        """
        self.broken_stocks = set()
        self.reason = ""

        # ── 1. 大盘熔断检查（沪深300）────────────────────────────
        csi300 = next((i for i in indices if "000300" in (i.get("index_code") or "")), None)
        if csi300:
            csi300_change = csi300.get("change_pct", 0)
            market_limit = CIRCUIT_BREAKER_RULES["market_crash"]
            if csi300_change <= market_limit:
                self.status = CircuitBreakerStatus.MARKET_BROKEN
                self.reason = f"沪深300跌幅{csi300_change:.1f}% > {market_limit:.1f}%，大盘熔断"
                logger.warning(f"🚨 熔断触发: {self.reason}")
                return self.status

        # ── 2. 单股熔断检查 ────────────────────────────────────
        stock_limit = CIRCUIT_BREAKER_RULES["single_stock_drop"]
        for q in quotes:
            change = q.get("change_pct", 0)
            if change <= stock_limit:
                code = q.get("ts_code", "unknown")
                self.broken_stocks.add(code)
                self.reason = f"{code}跌幅{change:.1f}% > {stock_limit:.1f}%，单股熔断"
                logger.warning(f"🚨 熔断触发: {self.reason}")

        if self.broken_stocks:
            self.status = CircuitBreakerStatus.SINGLE_STOCK_BROKEN
            return self.status

        # ── 3. 组合层面检查（从持仓盈亏计算） ──────────────────
        daily_pnl_pct = self._calc_daily_pnl_pct()
        portfolio_limit = CIRCUIT_BREAKER_RULES["daily_max_loss_pct"]
        if daily_pnl_pct <= portfolio_limit:
            self.status = CircuitBreakerStatus.PORTFOLIO_BROKEN
            self.reason = f"单日亏损{daily_pnl_pct:.1f}% > {abs(portfolio_limit):.1f}%，组合熔断"
            logger.warning(f"🚨 熔断触发: {self.reason}")
            return self.status

        # ── 4. 连续亏损检查 ───────────────────────────────────
        if self._check_consecutive_losses():
            self.status = CircuitBreakerStatus.COOLDOWN
            self.cooldown_until = datetime.now()
            self.reason = f"连续{CIRCUIT_BREAKER_RULES['consecutive_loss_days']}日亏损，降仓至50%"
            logger.warning(f"🚨 熔断触发: {self.reason}")
            return self.status

        self.status = CircuitBreakerStatus.NORMAL
        return self.status

    def _calc_daily_pnl_pct(self) -> float:
        """
        计算持仓加权平均涨跌幅。
        从当日成交数据计算，若无持仓则返回 0。
        """
        try:
            conn = get_db_conn()
            cur = conn.cursor()
            # 从持仓行情表查询当日加权平均涨跌
            cur.execute("""
                SELECT AVG(q.change_pct) as weighted_change
                FROM market.daily_quotes q
                JOIN LATERAL (
                    SELECT unnest(ARRAY[
                        '300059','002149','002943','002206'
                    ]::text[]) AS held_code
                ) h ON q.ts_code = h.held_code
                WHERE q.trade_date = CURRENT_DATE
            """)
            row = cur.fetchone()
            conn.close()
            if row and row[0] is not None:
                return float(row[0])
            return 0.0
        except Exception:
            return 0.0

    def _check_consecutive_losses(self) -> bool:
        """检查是否连续N日全市场普跌（持仓股均含在内）"""
        conn = get_db_conn()
        cur = conn.cursor()
        limit_days = CIRCUIT_BREAKER_RULES["consecutive_loss_days"]
        try:
            cur.execute("""
                SELECT trade_date, AVG(change_pct) as avg_change
                FROM market.daily_quotes
                WHERE trade_date >= CURRENT_DATE - INTERVAL '%s days'
                  AND change_pct IS NOT NULL
                GROUP BY trade_date
                ORDER BY trade_date DESC
                LIMIT %s
            """, (str(limit_days), limit_days))
            rows = cur.fetchall()
            if len(rows) < limit_days:
                return False
            return all(r[1] < 0 for r in rows)
        except Exception:
            return False
        finally:
            conn.close()

    def is_buy_allowed(self, ts_code: str = None) -> tuple[bool, str]:
        """
        检查是否可以买入
        returns: (allowed, reason)
        """
        if self.status == CircuitBreakerStatus.NORMAL:
            return True, "正常状态，可交易"

        if self.status == CircuitBreakerStatus.PORTFOLIO_BROKEN:
            return False, f"组合熔断中：{self.reason}，暂停所有买入"

        if self.status == CircuitBreakerStatus.MARKET_BROKEN:
            return False, f"大盘熔断中：{self.reason}，所有计划转为仅观察"

        if self.status == CircuitBreakerStatus.SINGLE_STOCK_BROKEN:
            if ts_code and ts_code in self.broken_stocks:
                return False, f"{ts_code}触发熔断，暂停该标的买入"
            return True, f"其他标的正常，但注意：{self.reason}"

        if self.status == CircuitBreakerStatus.COOLDOWN:
            return False, f"降仓冷却中：{self.reason}"

        return False, f"未知熔断状态: {self.status}"

    def get_position_limit(self) -> float:
        """
        获取当前仓位上限
        熔断触发后降为50%
        """
        if self.status == CircuitBreakerStatus.COOLDOWN:
            return 50.0
        return 100.0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "broken_stocks": list(self.broken_stocks),
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "position_limit": self.get_position_limit(),
            "rules": CIRCUIT_BREAKER_RULES,
        }

    def to_user_message(self) -> str:
        """生成用户可见的熔断状态消息"""
        status_emoji = {
            CircuitBreakerStatus.NORMAL: "🟢",
            CircuitBreakerStatus.SINGLE_STOCK_BROKEN: "🟡",
            CircuitBreakerStatus.PORTFOLIO_BROKEN: "🔴",
            CircuitBreakerStatus.MARKET_BROKEN: "🔴",
            CircuitBreakerStatus.COOLDOWN: "🟠",
        }
        emoji = status_emoji.get(self.status, "⚪")
        msg = f"{emoji} 熔断状态: {self.status}\n{self.reason}"
        if self.broken_stocks:
            msg += f"\n触发熔断标的: {', '.join(self.broken_stocks)}"
        if self.status != CircuitBreakerStatus.NORMAL:
            msg += f"\n当前仓位上限: {self.get_position_limit()}%"
        return msg


# ── 全局单例（每日重置） ────────────────────────────────────────────────

_circuit_breaker: Optional[CircuitBreaker] = None
_checked_date: Optional[date] = None


def get_circuit_breaker() -> CircuitBreaker:
    """获取熔断器单例（每日自动重置）"""
    global _circuit_breaker, _checked_date
    today = date.today()
    if _circuit_breaker is None or _checked_date != today:
        _circuit_breaker = CircuitBreaker()
        _checked_date = today
        logger.info(f"熔断器已重置（{today}）")
    return _circuit_breaker


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cb = get_circuit_breaker()

    print("=== 熔断器状态 ===")
    print(f"状态: {cb.status}")
    print(f"规则: {json.dumps(CIRCUIT_BREAKER_RULES, indent=2)}")

    allowed, reason = cb.is_buy_allowed()
    print(f"\n买入检查: {'✅' if allowed else '❌'} {reason}")
    print(f"仓位上限: {cb.get_position_limit()}%")
