"""
hk_fetcher.py — 港股市场数据采集模块
基于 akshare 采集港股行情、财务指标、指数数据
支持港股代码标准化（5位 → 完整代码）
"""

import logging
from datetime import date, timedelta


logger = logging.getLogger("invest_system.hk_fetcher")

HK_INDEX_MAP = {
    "HSI": "恒生指数",
    "HSCEI": "国企指数",
    "HSTECH": "恒生科技指数",
}

HK_QUOTE_COLUMNS = [
    "trade_date", "open", "high", "low", "close", "volume", "amount", "change_pct"
]


def normalize_hk_code(code: str) -> str:
    """
    港股代码标准化：补齐 5 位数字

    Args:
        code: 原始代码（如 5, 700, 9988）

    Returns:
        标准化 5 位代码（如 00005, 00700, 09988）
    """
    code = str(code).strip().zfill(5)
    return code


def fetch_hk_daily(ts_code: str, start_date: str = None, end_date: str = None) -> list[dict]:
    """
    获取港股日线行情

    Args:
        ts_code: 港股代码（5位数字，如 00700）
        start_date: 起始日期（YYYYMMDD），默认60天前
        end_date: 结束日期（YYYYMMDD），默认今天

    Returns:
        行情数据列表
    """
    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")
    if start_date is None:
        start_date = (date.today() - timedelta(days=60)).strftime("%Y%m%d")

    try:
        import akshare as ak
        df = ak.stock_hk_hist(
            symbol=normalize_hk_code(ts_code),
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )
        if df is None or df.empty:
            logger.warning(f"港股 {ts_code} 无行情数据")
            return []

        df = df.rename(columns={
            "日期": "trade_date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
            "成交额": "amount", "涨跌幅": "change_pct",
        })
        df["ts_code"] = normalize_hk_code(ts_code) + ".HK"
        df["trade_date"] = df["trade_date"].astype(str)
        return df.to_dict("records")
    except ImportError:
        logger.warning("akshare 未安装，港股数据采集跳过")
        return []
    except Exception as e:
        logger.warning(f"港股行情采集失败 {ts_code}: {e}")
        return []


def fetch_hk_index(index_code: str) -> list[dict]:
    """
    获取港股指数行情

    Args:
        index_code: 指数代码（HSI/HSCEI/HSTECH）

    Returns:
        指数行情数据
    """
    try:
        import akshare as ak
        df = ak.stock_hk_index_daily_em(symbol=index_code)
        if df is None or df.empty:
            return []

        df = df.tail(60)
        df = df.rename(columns={
            "date": "trade_date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume",
        })
        df["ts_code"] = index_code
        df["trade_date"] = df["trade_date"].astype(str)
        return df.to_dict("records")
    except ImportError:
        return []
    except Exception as e:
        logger.warning(f"港股指数采集失败 {index_code}: {e}")
        return []


def fetch_hk_financial(ts_code: str) -> dict:
    """
    获取港股财务指标

    Args:
        ts_code: 港股代码（5位数字）

    Returns:
        财务指标字典
    """
    try:
        import akshare as ak
        df = ak.stock_hk_financial_indicator_em(symbol=normalize_hk_code(ts_code))
        if df is None or df.empty:
            return {}

        latest = df.iloc[-1].to_dict()
        return {
            "ts_code": normalize_hk_code(ts_code) + ".HK",
            "report_date": str(latest.get("report_date", "")),
            "eps": float(latest.get("基本每股收益", 0) or 0),
            "bps": float(latest.get("每股净资产", 0) or 0),
            "roe": float(latest.get("净资产收益率", 0) or 0),
            "net_profit": float(latest.get("净利润", 0) or 0),
            "total_revenue": float(latest.get("营业总收入", 0) or 0),
            "profit_growth": float(latest.get("净利润同比增长率", 0) or 0),
        }
    except ImportError:
        return {}
    except Exception as e:
        logger.warning(f"港股财务指标采集失败 {ts_code}: {e}")
        return {}


def fetch_hk_realtime_quote(ts_code: str) -> dict:
    """
    获取港股实时行情快照

    Args:
        ts_code: 港股代码

    Returns:
        {"price": float, "change_pct": float, "volume": int, "name": str}
    """
    try:
        import akshare as ak
        df = ak.stock_hk_spot_em()
        if df is None or df.empty:
            return {}

        code = normalize_hk_code(ts_code)
        row = df[df["代码"] == code]
        if row.empty:
            return {}

        r = row.iloc[0]
        return {
            "ts_code": code + ".HK",
            "name": str(r.get("名称", "")),
            "price": float(r.get("最新价", 0) or 0),
            "change_pct": float(r.get("涨跌幅", 0) or 0),
            "volume": int(r.get("成交量", 0) or 0),
            "amount": float(r.get("成交额", 0) or 0),
            "high": float(r.get("最高价", 0) or 0),
            "low": float(r.get("最低价", 0) or 0),
            "open": float(r.get("今开", 0) or 0),
            "prev_close": float(r.get("昨收", 0) or 0),
        }
    except ImportError:
        return {}
    except Exception as e:
        logger.warning(f"港股实时行情失败 {ts_code}: {e}")
        return {}


def batch_fetch_hk_quotes(ts_codes: list[str]) -> dict[str, dict]:
    """
    批量获取港股实时行情

    Args:
        ts_codes: 港股代码列表

    Returns:
        {ts_code: quote_dict} 映射
    """
    try:
        import akshare as ak
        df = ak.stock_hk_spot_em()
        if df is None or df.empty:
            return {}

        result = {}
        for ts_code in ts_codes:
            code = normalize_hk_code(ts_code)
            row = df[df["代码"] == code]
            if not row.empty:
                r = row.iloc[0]
                result[ts_code] = {
                    "ts_code": code + ".HK",
                    "name": str(r.get("名称", "")),
                    "price": float(r.get("最新价", 0) or 0),
                    "change_pct": float(r.get("涨跌幅", 0) or 0),
                }
        return result
    except ImportError:
        return {}
    except Exception as e:
        logger.warning(f"港股批量行情失败: {e}")
        return {}


def store_hk_daily_to_db(records: list[dict]) -> int:
    """
    将港股日线行情写入 PostgreSQL

    Args:
        records: 行情数据列表

    Returns:
        写入条数
    """
    if not records:
        return 0

    import psycopg2
    from pgcrypto_migration import get_credential

    conn = psycopg2.connect(
        host="localhost", user="invest_admin",
        database="investpilot", password=get_credential("DB_PASSWORD"),
    )
    cur = conn.cursor()
    stored = 0
    try:
        for r in records:
            cur.execute("""
                INSERT INTO market.daily_quotes
                    (ts_code, trade_date, open, high, low, close, volume, change_pct)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ts_code, trade_date) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close,
                    volume = EXCLUDED.volume, change_pct = EXCLUDED.change_pct
            """, (
                r["ts_code"], r["trade_date"], r.get("open"), r.get("high"),
                r.get("low"), r.get("close"), r.get("volume"), r.get("change_pct"),
            ))
            stored += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"港股行情写入失败: {e}")
    finally:
        conn.close()

    return stored


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== 港股测试 ===")
    quote = fetch_hk_realtime_quote("00700")
    print(f"腾讯控股: {quote.get('price')} ({quote.get('change_pct')}%)")
    print(f"代码标准化: {normalize_hk_code('700')} = 00700")