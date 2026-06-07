"""
fetch_financial.py — 个股财务数据采集模块
接口: https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew
数据写入: market.financial_indicators
覆盖: 每股收益(EPS)、ROE、营收、净利润、资产负债率等核心指标
"""

import logging
import time
from datetime import datetime

import requests
import psycopg2

logger = logging.getLogger("invest_system.fetch_financial")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://emweb.securities.eastmoney.com/",
}

TIMEOUT = 20

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",  # 运行时由 _get_password() 注入,
}

# 市场代码前缀映射（用于构建 Eastmoney 查询码）
MARKET_PREFIX = {
    "SH": "SH",   # 上交所
    "SZ": "SZ",   # 深交所
    "BJ": "BJ",   # 北交所
}

# 纯数字代码 → Eastmoney 6位码（自动判断市场）
def normalize_code(code: str) -> str:
    """将纯数字代码转换为 Eastmoney 格式（如 '300059' → 'SZ300059'）"""
    code = code.strip().split(".")[0]
    if not code:
        return ""
    # 根据代码前缀判断市场
    # 6开头 → 上海(SH), 000/002/003/300 → 深圳(SZ), 8开头 → 北交所(BJ)
    if code.startswith(("6", "9")):
        return f"SH{code}"
    elif code.startswith(("0", "2", "3")):
        return f"SZ{code}"
    elif code.startswith("8"):
        return f"BJ{code}"
    else:
        return f"SZ{code}"  # 默认深圳


def _get_password():
    from pgcrypto_migration import get_credential
    return get_credential("DB_PASSWORD")

def get_db_conn():
    cfg = dict(DB_CONFIG)
    cfg["password"] = _get_password()
    return psycopg2.connect(**cfg)


def ensure_table():
    """确保财务指标表存在"""
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market.financial_indicators (
                id BIGSERIAL PRIMARY KEY,
                ts_code VARCHAR(20) NOT NULL,          -- 股票代码，如 '300059'
                report_date DATE NOT NULL,             -- 报告期
                report_type VARCHAR(20),                -- 报告类型：年报/半年报/三季报/一季报/一季报
                eps DECIMAL(20, 6),                   -- 每股收益(元)
                bps DECIMAL(20, 6),                   -- 每股净资产(元)
                roe DECIMAL(10, 4),                   -- 净资产收益率(%)
                roe_kcj DECIMAL(10, 4),              -- 扣非净资产收益率(%)
                net_profit DECIMAL(20, 2),           -- 归母净利润(元)
                total_revenue DECIMAL(20, 2),        -- 营业总收入(元)
                total_operate_income DECIMAL(20, 2), -- 营业收入(元)
                gross_margin DECIMAL(10, 4),          -- 毛利率(%)
                net_margin DECIMAL(10, 4),           -- 净利率(%)
                debt_ratio DECIMAL(10, 4),           -- 资产负债率(%)
                current_ratio DECIMAL(10, 4),        -- 流动比率
                quick_ratio DECIMAL(10, 4),           -- 速动比率
                cash_ratio DECIMAL(10, 4),            -- 现金比率(%)
                yoy_growth DECIMAL(10, 4),           -- 营收同比增长率(%)
                profit_growth DECIMAL(10, 4),         -- 净利润同比增长率(%)
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(ts_code, report_date)
            )
        """)
        conn.commit()
        logger.info("market.financial_indicators 表就绪")
    finally:
        cur.close()
        conn.close()


def fetch_financial_data(em_code: str) -> list[dict]:
    """
    获取单只股票的财务指标历史数据

    Args:
        em_code: Eastmoney 格式代码，如 'SZ300059'

    Returns:
        list[dict]: 财务指标列表（最新优先）
    """
    url = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew"
    params = {"type": 0, "code": em_code, "start": "", "end": ""}

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        data = resp.json()

        if data.get("status") == -1:
            logger.warning(f"Eastmoney F10 API 错误: {data.get('message')} (code={em_code})")
            return []

        raw_list = data.get("data", [])
        if not raw_list or not isinstance(raw_list, list):
            return []

        results = []
        for row in raw_list:
            report_date_str = row.get("REPORT_DATE", "")
            try:
                report_dt = datetime.strptime(report_date_str[:10], "%Y-%m-%d").date()
            except Exception:
                continue

            results.append({
                "ts_code": row.get("SECURITY_CODE", ""),
                "report_date": report_dt,
                "report_type": row.get("REPORT_DATE_NAME", ""),
                "eps": row.get("EPSJB"),                    # 每股收益
                "bps": row.get("BPS"),                     # 每股净资产
                "roe": row.get("ROEJQ"),                   # ROE（加权）
                "roe_kcj": row.get("ROEKCJQ"),             # ROE（扣非）
                "net_profit": row.get("PARENTNETPROFIT"),  # 归母净利润
                "total_revenue": row.get("TOTALOPERATEREVE"),  # 营业总收入
                "total_operate_income": row.get("MLR"),     # 毛利润
                "gross_margin": row.get("XSMLL"),           # 销售毛利率
                "net_margin": row.get("XSJLL"),             # 销售净利率
                "debt_ratio": row.get("ZCFZL"),             # 资产负债率
                "current_ratio": row.get("LD"),              # 流动比率
                "quick_ratio": row.get("SD"),                # 速动比率
                "cash_ratio": row.get("XJLLB"),             # 现金比率
                "yoy_growth": row.get("DJD_TOI_YOY"),      # 营收同比
                "profit_growth": row.get("DJD_DPNP_YOY"),  # 净利润同比
            })

        return results

    except Exception as e:
        logger.warning(f"获取财务数据失败 ({em_code}): {e}")
        return []


def save_financial_data(ts_code: str, records: list[dict]) -> int:
    """写入财务指标数据，返回新增记录数"""
    if not records:
        return 0

    conn = get_db_conn()
    cur = conn.cursor()
    saved = 0
    try:
        for r in records:
            try:
                cur.execute("""
                    INSERT INTO market.financial_indicators
                        (ts_code, report_date, report_type, eps, bps, roe, roe_kcj,
                         net_profit, total_revenue, total_operate_income, gross_margin,
                         net_margin, debt_ratio, current_ratio, quick_ratio, cash_ratio,
                         yoy_growth, profit_growth)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ts_code, report_date) DO UPDATE SET
                        report_type = EXCLUDED.report_type,
                        eps = EXCLUDED.eps,
                        bps = EXCLUDED.bps,
                        roe = EXCLUDED.roe,
                        roe_kcj = EXCLUDED.roe_kcj,
                        net_profit = EXCLUDED.net_profit,
                        total_revenue = EXCLUDED.total_revenue,
                        gross_margin = EXCLUDED.gross_margin,
                        net_margin = EXCLUDED.net_margin,
                        debt_ratio = EXCLUDED.debt_ratio,
                        yoy_growth = EXCLUDED.yoy_growth,
                        profit_growth = EXCLUDED.profit_growth
                """, (
                    ts_code,
                    r["report_date"],
                    r.get("report_type"),
                    r.get("eps"),
                    r.get("bps"),
                    r.get("roe"),
                    r.get("roe_kcj"),
                    r.get("net_profit"),
                    r.get("total_revenue"),
                    r.get("total_operate_income"),
                    r.get("gross_margin"),
                    r.get("net_margin"),
                    r.get("debt_ratio"),
                    r.get("current_ratio"),
                    r.get("quick_ratio"),
                    r.get("cash_ratio"),
                    r.get("yoy_growth"),
                    r.get("profit_growth"),
                ))
                saved += 1
            except Exception as e:
                logger.warning(f"写入财务数据异常 ({ts_code}, {r.get('report_date')}): {e}")
        conn.commit()
        return saved
    finally:
        cur.close()
        conn.close()


def collect_financial_for_positions(ts_codes: list[str]) -> dict:
    """
    为持仓股票批量采集财务数据

    Args:
        ts_codes: 股票代码列表（纯数字，如 ["300059", "002149"]）

    Returns:
        dict: {code: {"fetched": int, "saved": int, "records": list}}
    """
    ensure_table()
    results = {}
    for code in ts_codes:
        code = code.strip()
        if not code or len(code) != 6:
            continue

        em_code = normalize_code(code)
        logger.info(f"获取财务数据: {code} ({em_code})...")
        try:
            records = fetch_financial_data(em_code)
            saved = save_financial_data(code, records)
            results[code] = {"fetched": len(records), "saved": saved}
            logger.info(f"  {code}: 获取 {len(records)} 条, 写入 {saved} 条")
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f"  {code} 异常: {e}")
            results[code] = {"error": str(e)}

    total = sum(v.get("saved", 0) for v in results.values())
    logger.info(f"财务数据采集完成: 共 {total} 条记录写入")
    return results


# ─── 辅助函数 ──────────────────────────────────────────────────────────────

def get_latest_financial(ts_code: str, n: int = 4) -> list[dict]:
    """
    获取某股票最近 N 期财务数据（用于基本面分析）

    Returns:
        list[dict]: 按报告期降序排列
    """
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT report_date, report_type, eps, bps, roe,
                   total_revenue, net_profit, gross_margin,
                   debt_ratio, yoy_growth, profit_growth
            FROM market.financial_indicators
            WHERE ts_code = %s
            ORDER BY report_date DESC
            LIMIT %s
        """, (ts_code, n))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def format_financial_summary(ts_code: str, n: int = 4) -> str:
    """生成用于 LLM Prompt 的财务摘要文本"""
    records = get_latest_financial(ts_code, n)
    if not records:
        return f"股票 {ts_code}: 暂无财务数据"

    lines = [f"## {ts_code} 财务摘要（近{n}期）"]
    for r in records:
        dt = r["report_date"].isoformat() if hasattr(r["report_date"], "isoformat") else str(r["report_date"])
        lines.append(
            f"- {dt} [{r.get('report_type', '')}]: "
            f"EPS={r.get('eps', 'N/A'):.3f} | "
            f"ROE={r.get('roe', 'N/A'):.2f}% | "
            f"营收={_fmt(r.get('total_revenue'))} | "
            f"净利={_fmt(r.get('net_profit'))} | "
            f"毛利率={r.get('gross_margin', 'N/A'):.2f}% | "
            f"负债率={r.get('debt_ratio', 'N/A'):.2f}%"
        )
    return "\n".join(lines)


def _fmt(v) -> str:
    """格式化金额（亿元）"""
    if v is None:
        return "N/A"
    try:
        val = float(v)
        if abs(val) >= 1e8:
            return f"{val/1e8:.2f}亿"
        elif abs(val) >= 1e4:
            return f"{val/1e4:.2f}万"
        else:
            return f"{val:.2f}"
    except Exception:
        return str(v)


# ─── 可独立运行 ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    codes = sys.argv[1:] if len(sys.argv) > 1 else ["300059", "002149"]
    results = collect_financial_for_positions(codes)
    print(f"\n采集结果: {results}")
