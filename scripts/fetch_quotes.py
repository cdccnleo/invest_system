"""
fetch_quotes.py - Market Data Fetching Module
Multi-source: EastMoney (primary) -> Sina Finance (backup)
"""

import time
import re
import logging
import json
import urllib.request
from datetime import date


logger = logging.getLogger("invest_system.fetch_quotes")

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",  # noqa: E501
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
EM_TIMEOUT = 15


# HTTP client with retry
def _http_get(url: str, params: dict = None, retry: int = 3) -> dict | None:
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    for attempt in range(retry):
        try:
            req = urllib.request.Request(url, headers=EM_HEADERS)
            with urllib.request.urlopen(req, timeout=EM_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt < retry - 1:
                time.sleep(2 * (attempt + 1))
            else:
                logger.debug(f"HTTP GET failed ({attempt+1}x): {url[:60]} - {e}")
    return None


def _today_str() -> str:
    return date.today().strftime("%Y%m%d")


def _to_std_code(code: str) -> str:
    """Convert 6-digit code to standard format"""
    code = code.zfill(6)
    if code.startswith("15") or code.startswith("30") or code.startswith("00"):
        return f"{code}.XSHE"
    elif code.startswith("6") or code.startswith("5"):
        return f"{code}.XSHG"
    elif code.startswith("4") or code.startswith("8"):
        return f"{code}.BJ"
    else:
        return f"{code}.XSHE"


# EastMoney batch quotes API
def fetch_batch_em(symbols: list[str]) -> list[dict]:
    if not symbols:
        return []

    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fltt": 2, "invt": 2,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fields": "f1,f2,f3,f4,f5,f6,f7,f12,f14,f15,f16,f17,f18",
        "secids": ",".join(symbols),
        "_": int(time.time() * 1000),
    }

    data = _http_get(url, params)
    if data is None:
        return []

    results = []
    diff = (data.get("data") or {}).get("diff")
    if not diff:
        return []

    for item in diff:
        try:
            code = str(item.get("f12", ""))
            close_val = item.get("f2")
            if close_val == "-" or close_val is None:
                continue
            close = float(close_val)
            if close <= 0:
                continue
            results.append({
                "ts_code": _to_std_code(code),
                "trade_date": _today_str(),
                "open": float(item.get("f17", 0) or 0),
                "high": float(item.get("f15", 0) or 0),
                "low": float(item.get("f16", 0) or 0),
                "close": close,
                "volume": int(item.get("f5", 0) or 0),
                "amount": float(item.get("f6", 0) or 0),
                "change_pct": float(item.get("f3", 0) or 0),
                "source": "eastmoney",
            })
        except (ValueError, TypeError):
            continue

    logger.info(f"EastMoney fetched {len(results)}/{len(symbols)} quotes")
    return results


def fetch_single_em(ts_code: str) -> dict | None:
    try:
        code, market = ts_code.split(".")
        secid = f"{1 if market == 'XSHG' else 0}.{code}"
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "fltt": 2, "invt": 2,
            "fields": "f43,f57,f58,f50,f169,f170,f3",
            "secid": secid,
        }
        data = _http_get(url, params)
        if data is None:
            return None
        raw = data.get("data") or {}
        close = float(raw.get("f43", 0) or 0)
        if close <= 0:
            return None
        return {
            "ts_code": ts_code,
            "trade_date": _today_str(),
            "close": close,
            "open": float(raw.get("f50", 0) or 0),
            "high": float(raw.get("f170", 0) or 0),
            "low": float(raw.get("f169", 0) or 0),
            "change_pct": float(raw.get("f3", 0) or 0),
            "source": "eastmoney_single",
        }
    except Exception as e:
        logger.debug(f"Single quote failed {ts_code}: {e}")
        return None


# Sina Finance quotes (backup source)
def fetch_sina_quotes(symbols: list[str]) -> list[dict]:
    """
    Fetch real-time quotes via Sina Finance API.
    Sina format: https://hq.sinajs.cn/list=sz000858,sh600519
    """
    if not symbols:
        return []

    results = []
    batch_size = 50

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        # Convert to Sina format
        sina_codes = []
        for sym in batch:
            code, market = sym.split(".")
            prefix = "sz" if market == "XSHE" else "sh"
            sina_codes.append(f"{prefix}{code}")

        sina_str = ",".join(sina_codes)
        url = f"https://hq.sinajs.cn/list={sina_str}"

        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("gbk", errors="replace")

            # Parse with regex: hq_str_szXXXXX="..."
            pattern = re.compile(r'hq_str_([a-z]{2})(\d+)="([^"]+)"')
            for m in pattern.finditer(body):
                market_abbrev, code_raw, data_str = m.group(1), m.group(2), m.group(3)
                fields = data_str.split(",")
                market = "XSHE" if market_abbrev == "sz" else "XSHG"

                try:
                    prev_close = float(fields[2]) if fields[2] else 0
                    now = float(fields[3]) if fields[3] else 0
                    if now <= 0:
                        continue
                    change_pct = (now - prev_close) / prev_close * 100 if prev_close > 0 else 0

                    results.append({
                        "ts_code": f"{code_raw}.{market}",
                        "trade_date": _today_str(),
                        "open": float(fields[1]) if fields[1] else 0,
                        "close": now,
                        "high": float(fields[4]) if fields[4] else 0,
                        "low": float(fields[5]) if fields[5] else 0,
                        "volume": int(float(fields[8])) if fields[8] else 0,
                        "amount": float(fields[9]) if fields[9] else 0,
                        "change_pct": change_pct,
                        "source": "sina",
                    })
                except (ValueError, IndexError):
                    continue

        except Exception as e:
            logger.debug(f"Sina quote batch failed: {e}")

        time.sleep(0.3)

    logger.info(f"Sina fetched {len(results)}/{len(symbols)} quotes")
    return results


# Index quotes
def fetch_index_em() -> list[dict]:
    index_codes = ["1.000300", "1.000001", "0.399006"]
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fltt": 2, "invt": 2,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fields": "f1,f2,f3,f12,f14",
        "secids": ",".join(index_codes),
        "_": int(time.time() * 1000),
    }

    data = _http_get(url, params)
    if data is None:
        return []

    name_map = {"000300": "CSI300", "000001": "SSE", "399006": "GEM"}
    results = []
    for item in (data.get("data") or {}).get("diff", []):
        try:
            raw = str(item.get("f12", ""))
            close = float(item.get("f2", 0) or 0)
            if close <= 0:
                continue
            idx_code = {"000300": "000300.XSHG", "000001": "000001.XSHG",
                        "399006": "399006.XSZ"}.get(raw, f"{raw}.XSHG")
            results.append({
                "index_code": idx_code,
                "index_name": name_map.get(raw, raw),
                "trade_date": _today_str(),
                "close": close,
                "change_pct": float(item.get("f3", 0) or 0),
                "source": "eastmoney",
            })
        except (ValueError, TypeError):
            continue

    logger.info(f"Index fetched {len(results)} quotes")
    return results


# Sector fund flow
def fetch_sector_flow_em() -> list[dict]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 30, "po": 1, "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2, "invt": 2, "fid": "f62",
        "fs": "m:90+t:2+f:!50",
        "fields": "f12,f14,f62,f184",
        "_": int(time.time() * 1000),
    }

    data = _http_get(url, params)
    if data is None:
        return []

    results = []
    for item in (data.get("data") or {}).get("diff", []):
        try:
            name = str(item.get("f14", ""))
            net_flow = float(item.get("f62", 0) or 0)
            net_flow_pct = float(item.get("f184", 0) or 0)
            if abs(net_flow) < 1000:
                continue
            results.append({
                "trade_date": _today_str(),
                "sector_name": name,
                "net_flow": net_flow,
                "net_flow_pct": net_flow_pct,
                "source": "eastmoney",
            })
        except (ValueError, TypeError):
            continue

    logger.info(f"Sector flow fetched {len(results)} entries")
    return results


# Main entry
def fetch_fund_nav(fund_codes: list[str]) -> list[dict]:
    """
    Fetch fund NAV (净值) via EastMoney fund API.
    API: https://fundgz.1234567.com.cn/js/{code}.js
    Returns: list of fund NAV dicts with ts_code=OFCODES.OF format
    """
    if not fund_codes:
        return []

    results = []
    pattern = re.compile(r'jsonpgz\((.+)\)')

    for fc in fund_codes:
        url = f"https://fundgz.1234567.com.cn/js/{fc}.js?rt={int(time.time() * 1000)}"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://fund.eastmoney.com/",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                m = pattern.search(body)
                if not m:
                    continue
                data = json.loads(m.group(1))
                fundcode = data.get("fundcode", "")
                name = data.get("name", "")
                dwjz = float(data.get("dwjz", 0) or 0)  # confirmed NAV
                gsz = float(data.get("gsz", 0) or 0)    # estimated NAV
                gszzl = float(data.get("gszzl", 0) or 0)
                jzrq = data.get("jzrq", "")

                # Use estimated NAV if available (more timely), else confirmed NAV
                nav = gsz if gsz > 0 else dwjz
                if nav <= 0:
                    continue

                results.append({
                    "ts_code": f"{fundcode}.OF",   # .OF = 场外基金
                    "trade_date": jzrq or _today_str(),
                    "close": nav,
                    "prev_close": dwjz,
                    "change_pct": gszzl,
                    "fund_name": name,
                    "source": "eastmoney_fund",
                })
        except Exception as e:
            logger.debug(f"Fund NAV fetch failed {fc}: {e}")
        time.sleep(0.3)

    logger.info(f"Fund NAV fetched {len(results)}/{len(fund_codes)}")
    return results


def collect_quotes(symbols: list[str]) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Fetch market data for STOCKS/ETFS only (multi-source redundancy).
    Priority: EastMoney -> Sina Finance
    Returns: (quotes, indices, sector_flows)
    """
    logger.info(f"Starting quote collection for {len(symbols)} symbols (stocks/ETFs)...")

    quotes = []

    # Source 1: EastMoney
    em_quotes = fetch_batch_em(symbols)
    if em_quotes and len(em_quotes) >= len(symbols) * 0.5:
        quotes = em_quotes
    else:
        # Source 2: Sina Finance (fallback)
        if not em_quotes:
            logger.warning("EastMoney 返回空，切换至 Sina 备用...")
        else:
            logger.warning(f"EastMoney 命中率低 ({len(em_quotes)}/{len(symbols)})，切换至 Sina 备用...")  # noqa: E501
        sina_quotes = fetch_sina_quotes(symbols)
        if sina_quotes and len(sina_quotes) >= len(symbols) * 0.5:
            quotes = sina_quotes
        else:
            # Source 3: EastMoney 单只轮询
            logger.warning(f"Sina 命中率低 ({len(sina_quotes) if sina_quotes else 0}/{len(symbols)})，切换单只轮询...")  # noqa: E501
            for sym in symbols:
                sym_raw = sym.split(".")[0]
                if not any(q["ts_code"].split(".")[0] == sym_raw for q in quotes):
                    single = fetch_single_em(sym)
                    if single and single.get("close", 0) > 0:
                        quotes.append(single)

    indices = fetch_index_em()
    sector_flows = fetch_sector_flow_em()

    logger.info(f"Collection complete: {len(quotes)} quotes, {len(indices)} indices, "
                f"{len(sector_flows)} sectors")

    return quotes, indices, sector_flows
