"""
持仓历史equity curve追踪器
每日收盘后计算组合总市值，存入数据库
"""

import sys
from pathlib import Path
from datetime import date
from decimal import Decimal

# ── 路径设置 ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from storage_factory import get_storage
from pgcrypto_migration import load_positions_from_db


def calc_total_portfolio_value() -> float:
    """
    计算当前组合总市值。
    从 holdings.encrypted_positions 读取最新持仓（已解密），
    返回所有标的 market_value 之和。
    """
    try:
        positions = load_positions_from_db()
        total = sum(float(p.get("market_value", 0)) for p in positions)
        return total
    except Exception as e:
        # 防御：连接失败时返回 0 而不抛异常
        import logging
        logging.getLogger("equity_curve_tracker").warning(f"计算总市值失败: {e}")
        return 0.0


def save_daily_equity(calc_date_arg: date | None = None) -> dict:
    """
    保存当日equity到 market.portfolio_equity_curve 表。
    目标日期默认今天，UPSERT 语义（已存在则更新市值和持仓数）。
    返回 dict: {saved: bool, total_value: float, position_count: int, calc_date: date}
    """
    calc_date = calc_date_arg if calc_date_arg is not None else date.today()

    total_value = calc_total_portfolio_value()

    try:
        positions = load_positions_from_db()
        position_count = len(positions)
    except Exception:
        position_count = 0

    try:
        storage = get_storage()
        cur = storage._ensure_pg() and storage._pg_conn.cursor()
        if not cur:
            storage.close()
            return {"saved": False, "total_value": total_value,
                    "position_count": position_count, "calc_date": calc_date,
                    "error": "无法连接数据库"}

        conn = storage._pg_conn  # type: ignore[assignment]
        cur.execute("""
            INSERT INTO market.portfolio_equity_curve
                (calc_date, total_value, position_count)
            VALUES (%s, %s, %s)
            ON CONFLICT (calc_date) DO UPDATE SET
                total_value    = EXCLUDED.total_value,
                position_count = EXCLUDED.position_count,
                created_at     = CURRENT_TIMESTAMP
        """, (calc_date, Decimal(str(total_value)), position_count))
        conn.commit()
        cur.close()
        storage.close()
        return {"saved": True, "total_value": total_value,
                "position_count": position_count, "calc_date": calc_date}
    except Exception as e:
        import logging
        logging.getLogger("equity_curve_tracker").error(f"保存equity失败: {e}")
        return {"saved": False, "total_value": total_value,
                "position_count": position_count, "calc_date": calc_date,
                "error": str(e)}


def get_equity_curve(days: int = 365) -> list[dict]:
    """
    获取历史equity曲线数据（最近 days 天）。
    返回列表按 calc_date ASC 排序，每条记录包含:
    {calc_date, total_value, position_count}
    """
    try:
        storage = get_storage()
        cur = storage._ensure_pg() and storage._pg_conn.cursor()
        if not cur:
            storage.close()
            return []

        conn = storage._pg_conn  # type: ignore[assignment]
        cur.execute("""
            SELECT calc_date, total_value, position_count
            FROM market.portfolio_equity_curve
            WHERE calc_date >= CURRENT_DATE - INTERVAL '1 day' * %s
            ORDER BY calc_date ASC
        """, (days,))
        rows = cur.fetchall()
        cur.close()
        storage.close()

        return [
            {
                "calc_date":      r[0].isoformat() if r[0] else "",
                "total_value":    float(r[1]) if r[1] else 0.0,
                "position_count": r[2] or 0,
            }
            for r in rows
        ]
    except Exception as e:
        import logging
        logging.getLogger("equity_curve_tracker").warning(f"读取equity曲线失败: {e}")
        return []


# ── 初始化 market.portfolio_equity_curve 表 ─────────────────────────────────────

def init_equity_curve_table():
    """确保 market.portfolio_equity_curve 表存在，不存在则创建。"""
    DDL = """
    CREATE TABLE IF NOT EXISTS market.portfolio_equity_curve (
        id              SERIAL PRIMARY KEY,
        calc_date       DATE UNIQUE NOT NULL,
        total_value     DECIMAL(16, 2) NOT NULL,
        position_count  INTEGER DEFAULT 0,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_equity_curve_calc_date
        ON market.portfolio_equity_curve(calc_date);
    """
    try:
        storage = get_storage()
        cur = storage._ensure_pg() and storage._pg_conn.cursor()
        if not cur:
            storage.close()
            return False

        conn = storage._pg_conn  # type: ignore[assignment]
        cur.execute(DDL)
        conn.commit()
        cur.close()
        storage.close()
        return True
    except Exception as e:
        import logging
        logging.getLogger("equity_curve_tracker").error(f"建表失败: {e}")
        return False


if __name__ == "__main__":
    # 初始化表（首次运行）
    init_equity_curve_table()
    # 直接调用时打印当前总市值
    total = calc_total_portfolio_value()
    print(f"当前组合总市值: ¥{total:,.2f}")
