"""
Equity Curve 历史数据回填脚本
根据每日行情 + 持仓快照，回填历史组合市值

策略：用持仓数量 * 历史收盘价 估算历史市值
持仓份额来自 holdings.encrypted_positions（当前持仓）
历史行情来自 market.daily_quotes
"""

import sys
from pathlib import Path
from datetime import date

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from storage_factory import get_storage
from pgcrypto_migration import load_positions_from_db

def get_all_trade_dates(days: int = 90) -> list[date]:
    """获取最近 N 个交易日"""
    storage = get_storage()
    if not storage._ensure_pg():
        return []
    cur = storage._pg_conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date FROM market.daily_quotes
        WHERE trade_date >= CURRENT_DATE - INTERVAL '1 day' * %s
        ORDER BY trade_date ASC
    """, (days,))
    dates = [row[0] for row in cur.fetchall() if row[0]]
    cur.close()
    storage.close()
    return dates

def backfill_equity_history(days: int = 90) -> dict:
    """回填历史 equity curve"""
    trade_dates = get_all_trade_dates(days)
    positions = load_positions_from_db()
    
    # Build code -> shares map (current holdings)
    code_shares = {}
    code_cost = {}
    for p in positions:
        code = p.get("code") or p.get("ts_code", "")
        code_shares[code] = float(p.get("shares", 0))
        code_cost[code] = float(p.get("avg_cost", 0))
    
    if not code_shares:
        return {"saved": 0, "errors": 0}
    
    # Build code -> full ts_code map by checking daily_quotes
    storage = get_storage()
    if not storage._ensure_pg():
        return {"saved": 0, "errors": 0, "error": "PG not available"}
    conn = storage._pg_conn
    cur = conn.cursor()
    
    cur.execute("SELECT DISTINCT ts_code FROM market.daily_quotes")
    all_ts_codes = set(row[0] for row in cur.fetchall())
    
    code_to_tscode = {}
    for code in code_shares:
        # Try exact match first, then partial match
        if code in all_ts_codes:
            code_to_tscode[code] = code
        else:
            matches = [t for t in all_ts_codes if t.startswith(code + ".")]
            if matches:
                code_to_tscode[code] = matches[0]
    
    # For each trade date, sum up market value
    saved = 0
    errors = 0
    
    for trade_date in trade_dates:
        try:
            # Get closing prices for all held codes on this date
            ts_codes = list(code_to_tscode.values())
            if not ts_codes:
                continue
            placeholders = ",".join(["%s"] * len(ts_codes))
            cur.execute(f"""
                SELECT ts_code, close_price FROM market.daily_quotes
                WHERE ts_code IN ({placeholders}) AND trade_date = %s
            """, ts_codes + [trade_date])
            
            price_map = {row[0]: float(row[1]) for row in cur.fetchall()}
            
            # Map price back to original code
            total_value = 0.0
            for code, ts_code in code_to_tscode.items():
                shares = code_shares.get(code, 0)
                price = price_map.get(ts_code, 0)
                total_value += shares * price
            
            # Only save if we have actual prices (not all missing)
            if len(price_map) > len(ts_codes) // 2:
                from decimal import Decimal
                cur.execute("""
                    INSERT INTO market.portfolio_equity_curve
                        (calc_date, total_value, position_count)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (calc_date) DO UPDATE SET
                        total_value = EXCLUDED.total_value,
                        position_count = EXCLUDED.position_count,
                        created_at = CURRENT_TIMESTAMP
                """, (trade_date, Decimal(str(total_value)), len(code_shares)))
                saved += 1
        except Exception as e:
            errors += 1
            import logging
            logging.getLogger("backfill").warning(f"回填 {trade_date} 失败: {e}")
    
    conn.commit()
    cur.close()
    storage.close()
    return {"saved": saved, "errors": errors}

if __name__ == "__main__":
    result = backfill_equity_history(days=90)
    print(f"回填完成: 写入 {result['saved']} 天, 错误 {result['errors']} 天")