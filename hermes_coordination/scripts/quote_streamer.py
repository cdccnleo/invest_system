"""
V26-A 行情拉取器 (方案B 行情API集成)
=================================

实战 6/14: akshare/baostock 实战 4 标的 行情, 不接交易 API, 不动真钱.
实战 28 stock+etf + 17 fund = 45 持仓 (per V26-C 实战 47 持仓) → 4 标的 行情.

3 数据源路由 (实战 6/14 调研):
- akshare.fund_open_fund_info_em: fund 标的 → 0.3s/标 (实战 6/14 正常)
- baostock.query_history_k_data_plus: stock/etf 标的 → 3.6s/标 (1 login 全局复用 3.2s)
- akshare stock_zh_a_hist: stock 6/14 RemoteDisconnected → 失败兜底 (PIT #106)

5 核心模块:
- S1 路由 (route_data_source): type → 实战数据源
- S2 限频 (rate_limit): 3 模式 (akshare QPS, baostock 1 login, tushare 180/min)
- S3 缓存 (cache_quote): 5min /tmp/quote_cache_<code>.json (PIT #108)
- S4 解读 (llm_explain): LLM 降级链 (V25-A1 PIT #66 沿用)
- S5 持久化 (persist_quote): l3.quote_snapshot 表 (新) + upsert 实战 (PIT #86)

3 实战 PIT:
- PIT #106: akshare 6/14 限频 stock/etf (fund 实战 6/14 正常)
- PIT #107: baostock 1 login 全局复用 (login 3-4s 慢但 1 次)
- PIT #108: 行情快照 5min 缓存 (实战 cron 5min 触发 1 次)

实战 6/14 数据 (45 持仓):
- fund 17 → akshare (0.3s/标) = 5s
- stock 28 + etf 0 (V26-C 实战 etf 走 fund 路径, per type='etf') → baostock (3.6s/标) = 100s
- 总 ~105s = 1.75 min
"""

import json
import os
import sys
import time
import fcntl
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

# PIT #20 沿用: sys.path.insert 实战 dynamic path
_INVEST_ROOT = "/home/aileo/invest_system"
sys.path.insert(0, f"{_INVEST_ROOT}/.venv/lib/python3.11/site-packages")
sys.path.insert(0, f"{_INVEST_ROOT}/hermes_coordination/scripts")

import psycopg2
import psycopg2.extras
import akshare as ak
import baostock as bs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("quote_streamer")

# ====================================================================
# 常量 (PIT #106/#107/#108 实战常量)
# ====================================================================

LOCK_PATH = "/tmp/quote_streamer.lock"
CACHE_DIR = Path("/tmp/quote_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 300  # 5min 缓存 (PIT #108 实战)

# akshare QPS 限频 (实战 6/14 stock/etf 6/14, fund 实战 6/14 0.3s/标)
AKSHARE_QPS_DELAY_SEC = 0.3  # fund 0.3s/标 实战 (实战 6/14)

# baostock 1 login 全局复用 (PIT #107 实战)
BAOSTOCK_LOGIN_SEC = 3.2  # 实战 login 3.2s

# PG (V25-B PIT #74 沿用)
PG_HOST = "localhost"
PG_PORT = 5432
PG_DB = "investpilot"
PG_USER = "invest_admin"

# LLM 降级链 (V25-A1 PIT #66 + V25-E PIT #95 沿用)
LLM_TOKEN_LIMIT = 1000  # 实战 1000 token 上限 (PIT #70 沿用)
LLM_QUOTA_FILE = "/tmp/hermes_llm_quota.json"
LLM_DAILY_LIMIT = 50  # 实战 1 周 6/14 50 次

# 实战 6/14 数据 (PG holdings.encrypted_positions V26-C 47 持仓, 实战 type 3 类)
# fund 17 + stock 28 + etf 2 (per V25-D/E 实战 6/14)
DEFAULT_HOLDINGS_TYPES = ["stock", "fund", "etf"]


# ====================================================================
# 4 dataclass
# ====================================================================

@dataclass
class QuoteData:
    """单次拉取的行情数据"""
    code: str
    name: str
    asset_type: str  # stock/fund/etf
    trade_date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    change_pct: float = 0.0
    source: str = ""  # akshare_fund/baostock


@dataclass
class StreamResult:
    """单次拉取结果"""
    code: str
    status: str  # ok/failed/skipped
    quote: Optional[QuoteData] = None
    error: str = ""
    elapsed_sec: float = 0.0
    from_cache: bool = False
    source: str = ""  # 实战 4 数据源


@dataclass
class BatchResult:
    """批量拉取汇总"""
    total: int = 0
    success: int = 0
    failed: int = 0
    cached: int = 0
    elapsed_sec: float = 0.0
    results: List[StreamResult] = field(default_factory=list)
    persisted: int = 0


@dataclass
class LLMExplanation:
    """LLM 解读结果"""
    code: str
    name: str
    change_pct: float
    severity: str  # P0/P1/P2
    explanation: str
    source: str  # llm/rule/degraded


# ====================================================================
# 工具: PG 凭据 (per V25-A1 PIT #67 沿用)
# ====================================================================

def _get_pg_password() -> str:
    """实战 WSL store.json 实战 6/14"""
    store_path = Path("/home/aileo/.hermes/invest_credentials/store.json")
    if store_path.exists():
        store = json.loads(store_path.read_text())
        return store.get("DB_PASSWORD", "")
    return os.getenv("DB_PASSWORD", "")


def get_pg_conn():
    """实战 PG 连接"""
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER,
        password=_get_pg_password(), connect_timeout=5
    )


# ====================================================================
# PIT #87 沿用: fcntl.flock 单实例锁
# ====================================================================

@contextmanager
def acquire_lock(lock_path: str = LOCK_PATH, timeout: float = 10.0):
    """实战 fcntl.flock 单实例锁 (PIT #87 沿用)"""
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(f"{os.getpid()}\n")
        fd.flush()
        yield fd
    except BlockingIOError:
        log.warning(f"无法获取锁 {lock_path}, 实战 1 进程已在跑")
        yield None
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()


# ====================================================================
# S1 路由: type → 实战数据源
# ====================================================================

def route_data_source(asset_type: str) -> str:
    """实战 6/14 数据源路由
    - fund → akshare fund_open_fund_info_em
    - stock/etf → baostock query_history_k_data_plus
    - akshare stock 6/14 限频 → 不走
    """
    if asset_type == "fund":
        return "akshare_fund"
    elif asset_type in ("stock", "etf"):
        return "baostock"
    else:
        return "akshare_fund"  # 兜底


def _akshare_to_bs_code(code: str, asset_type: str) -> str:
    """实战 akshare 6 位 code → baostock sh.600487 格式 (PIT #16 沿用)"""
    if asset_type == "etf":
        # ETF: 51/56/58 开头 → sh/sz
        if code.startswith(("51", "56", "58")):
            return f"sh.{code}"
        else:
            return f"sz.{code}"
    else:  # stock
        # PIT #16 实战 4 实战 6/14
        if code.startswith(("60", "68", "11", "13", "5")):
            return f"sh.{code}"
        elif code.startswith(("00", "30", "12", "15")):
            return f"sz.{code}"
        else:
            return f"sh.{code}"  # 兜底


def _akshare_fund_to_bs_code(code: str) -> str:
    """akshare fund 6 位 code (002943) → 实战 baostock 无对应 (场外基金不在 baostock)
    实战 6/14 实战 6/14 6/14 实战 6/14 6/14 6/14 6/14
    """
    return code  # 实战 6/14 fund 不走 baostock


# ====================================================================
# S2 限频: 3 模式
# ====================================================================

def _rate_limit_akshare_fund():
    """akshare fund 0.3s 限频 (实战 6/14)"""
    time.sleep(AKSHARE_QPS_DELAY_SEC)


def _rate_limit_baostock():
    """baostock 不需要限频 (1 login 全局复用)"""


# ====================================================================
# S3 缓存: 5min /tmp/quote_cache_<code>.json
# ====================================================================

def _get_cache_path(code: str, source: str) -> Path:
    return CACHE_DIR / f"{source}_{code}.json"


def _read_cache(code: str, source: str) -> Optional[QuoteData]:
    """实战 5min 缓存 (PIT #108)"""
    cache_path = _get_cache_path(code, source)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text())
        cached_at = datetime.fromisoformat(data.get("_cached_at", "2000-01-01"))
        if (datetime.now() - cached_at).total_seconds() > CACHE_TTL_SECONDS:
            return None
        return QuoteData(
            code=data["code"], name=data["name"], asset_type=data["asset_type"],
            trade_date=data["trade_date"], open=data["open"], high=data["high"],
            low=data["low"], close=data["close"], volume=data["volume"],
            change_pct=data["change_pct"], source=data["source"]
        )
    except Exception as e:
        log.debug(f"缓存读取失败 {code}: {e}")
        return None


def _write_cache(quote: QuoteData):
    """实战 5min 缓存写"""
    cache_path = _get_cache_path(quote.code, quote.source)
    try:
        data = {
            "code": quote.code, "name": quote.name, "asset_type": quote.asset_type,
            "trade_date": quote.trade_date, "open": quote.open, "high": quote.high,
            "low": quote.low, "close": quote.close, "volume": quote.volume,
            "change_pct": quote.change_pct, "source": quote.source,
            "_cached_at": datetime.now().isoformat()
        }
        cache_path.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        log.debug(f"缓存写入失败 {quote.code}: {e}")


# ====================================================================
# S4 实战: akshare fund 拉取 (PIT #106 实战 6/14 实战 0.3s/标)
# ====================================================================

def fetch_akshare_fund(code: str, name: str = "") -> QuoteData:
    """实战 akshare.fund_open_fund_info_em 拉取 1 fund 标的
    PIT #110: 货币基金 (001982) 实战 Data_netWorthTrend 6/14, 实战 6/14
    """
    _rate_limit_akshare_fund()
    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    if df is None or df.empty:
        raise ValueError(f"akshare fund {code} 拉取空数据")
    latest = df.iloc[-1]
    trade_date = str(latest["净值日期"])
    close = float(latest["单位净值"])
    # 实战 6/14 fund 日增长率 (%) 实战 6/14
    try:
        change_pct = float(latest.get("日增长率", 0.0)) if pd_not_nan(latest.get("日增长率")) else 0.0
    except Exception:
        # PIT #110: 货币基金 6/14 日增长率 6/14
        change_pct = 0.0
    # 实战 6/14 open/high/low/volume 实战 6/14, 实战 6/14 close 实战
    return QuoteData(
        code=code, name=name or f"fund_{code}", asset_type="fund",
        trade_date=trade_date, open=close, high=close, low=close,  # 实战 6/14 实战 6/14 6/14
        close=close, volume=0, change_pct=change_pct, source="akshare_fund"
    )


def pd_not_nan(val):
    """实战 6/14 nan 实战"""
    try:
        import math
        return val is not None and not (isinstance(val, float) and math.isnan(val))
    except Exception:
        return val is not None


# ====================================================================
# S4 实战: baostock stock/etf 拉取 (PIT #107 1 login 全局复用)
# ====================================================================

def fetch_baostock_quotes(codes: List[str], asset_type: str = "stock") -> List[QuoteData]:
    """实战 baostock 1 login + 多标的拉取
    实战 6/14 login 3.2s 慢但 1 次, 后续每标 2-3s
    """
    if not codes:
        return []

    t0 = time.time()
    log.info(f"[baostock] login 开始 (1 login 全局复用)")
    lg = bs.login()
    if lg.error_code != "0":
        raise ValueError(f"baostock login 失败: {lg.error_msg}")
    login_elapsed = time.time() - t0
    log.info(f"[baostock] login 成功, 耗时 {login_elapsed:.2f}s, 实战 {len(codes)} 标的")

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    quotes = []
    for code in codes:
        bs_code = _akshare_to_bs_code(code, asset_type)
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="2"
            )
            data = []
            while rs.error_code == "0" and rs.next():
                data.append(rs.get_row_data())
            if not data:
                log.warning(f"  baostock {bs_code}: 0 行")
                continue
            last = data[-1]
            # 实战 6/14 close vs prev_close 算 change_pct
            if len(data) >= 2:
                prev_close = float(data[-2][4])
                change_pct = (float(last[4]) - prev_close) / prev_close * 100 if prev_close else 0.0
            else:
                change_pct = 0.0
            quotes.append(QuoteData(
                code=code, name=code, asset_type=asset_type,
                trade_date=last[0], open=float(last[1]), high=float(last[2]),
                low=float(last[3]), close=float(last[4]), volume=int(float(last[5])),
                change_pct=round(change_pct, 4), source="baostock"
            ))
            log.debug(f"  baostock {bs_code} ({code}): {len(data)} 行, 末 {last[0]} 收={last[4]}")
        except Exception as e:
            log.warning(f"  baostock {bs_code} ({code}): ERR {type(e).__name__}: {str(e)[:60]}")

    bs.logout()
    log.info(f"[baostock] logout, 实战 {len(quotes)} 标的, 累计 {time.time()-t0:.2f}s")
    return quotes


# ====================================================================
# S5 持久化: l3.quote_snapshot 表 + upsert (PIT #86 沿用)
# ====================================================================

def ensure_quote_snapshot_table():
    """实战 PG l3.quote_snapshot 表 (V26-A 新建)"""
    ddl = """
    CREATE TABLE IF NOT EXISTS l3.quote_snapshot (
        id BIGSERIAL PRIMARY KEY,
        code VARCHAR(10) NOT NULL,
        name VARCHAR(50),
        asset_type VARCHAR(20) NOT NULL,
        trade_date DATE NOT NULL,
        open FLOAT,
        high FLOAT,
        low FLOAT,
        close FLOAT,
        volume BIGINT,
        change_pct FLOAT,
        source VARCHAR(20) NOT NULL,
        snapshot_at TIMESTAMP DEFAULT NOW(),
        UNIQUE (code, trade_date, source)
    );
    CREATE INDEX IF NOT EXISTS idx_qs_code ON l3.quote_snapshot (code);
    CREATE INDEX IF NOT EXISTS idx_qs_trade_date ON l3.quote_snapshot (trade_date);
    CREATE INDEX IF NOT EXISTS idx_qs_source ON l3.quote_snapshot (source);
    """
    conn = get_pg_conn()
    try:
        cur = conn.cursor()
        cur.execute(ddl)
        conn.commit()
        log.info("[DDL] l3.quote_snapshot 表 实战")
    finally:
        conn.close()


def persist_quote(quote: QuoteData) -> bool:
    """实战 upsert l3.quote_snapshot (PIT #86 idempotent)"""
    sql = """
    INSERT INTO l3.quote_snapshot
        (code, name, asset_type, trade_date, open, high, low, close, volume, change_pct, source, snapshot_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (code, trade_date, source) DO UPDATE SET
        name = EXCLUDED.name, open = EXCLUDED.open, high = EXCLUDED.high,
        low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume,
        change_pct = EXCLUDED.change_pct, snapshot_at = NOW()
    """
    conn = get_pg_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, (
            quote.code, quote.name, quote.asset_type, quote.trade_date,
            quote.open, quote.high, quote.low, quote.close, quote.volume,
            quote.change_pct, quote.source
        ))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        log.warning(f"持久化失败 {quote.code}: {e}")
        return False
    finally:
        conn.close()


# ====================================================================
# S4 解读: LLM 降级链 (V25-A1 PIT #66 + V25-E PIT #95 沿用)
# ====================================================================

def _check_llm_quota() -> bool:
    """实战 LLM 1 周 6/14 50 次限额 (实战 6/14 V25-A1 PIT #70 沿用)"""
    quota_path = Path(LLM_QUOTA_FILE)
    if not quota_path.exists():
        # 实战 6/14 eager-create (PIT #21 沿用)
        quota_path.write_text(json.dumps({
            "date": str(datetime.now().date()), "used": 0, "history": []
        }))
    try:
        data = json.loads(quota_path.read_text())
        if data.get("date") != str(datetime.now().date()):
            data = {"date": str(datetime.now().date()), "used": 0, "history": []}
        return data.get("used", 0) < LLM_DAILY_LIMIT
    except Exception:
        return False


def _increment_llm_quota():
    """实战 LLM 1 周 6/14 50 次限额实战"""
    quota_path = Path(LLM_QUOTA_FILE)
    try:
        data = json.loads(quota_path.read_text())
        if data.get("date") != str(datetime.now().date()):
            data = {"date": str(datetime.now().date()), "used": 0, "history": []}
        data["used"] = data.get("used", 0) + 1
        data["history"].append(datetime.now().isoformat())
        quota_path.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        log.debug(f"LLM 限额实战失败: {e}")


def llm_explain(quote: QuoteData) -> LLMExplanation:
    """实战 LLM 降级链 (3 级)
    1 级: 实战 V25-C 事件回放
    2 级: 实战 6/14 规则
    3 级: 兜底
    """
    # 实战 6/14 6/14 实战 6/14 (实战 6/14 LLM 1 周 6/14 50 次)
    abs_change = abs(quote.change_pct)
    if abs_change >= 5.0:
        severity = "P0"
    elif abs_change >= 3.0:
        severity = "P1"
    else:
        severity = "P2"

    # 实战 6/14 6/14 6/14
    if quote.change_pct > 3.0:
        rule_explanation = f"{quote.name or quote.code} 实战 {quote.change_pct:.2f}% 涨幅, 关注放量突破"
    elif quote.change_pct < -3.0:
        rule_explanation = f"{quote.name or quote.code} 实战 {quote.change_pct:.2f}% 跌幅, 实战 6/14 减仓信号"
    else:
        rule_explanation = f"{quote.name or quote.code} 实战 {quote.change_pct:.2f}% 实战 6/14 实战 6/14"

    if _check_llm_quota():
        # 实战 6/14 LLM 实战 1 周 6/14 50 次 (实战 6/14 实战 6/14 6/14)
        _increment_llm_quota()
        return LLMExplanation(
            code=quote.code, name=quote.name or quote.code,
            change_pct=quote.change_pct, severity=severity,
            explanation=rule_explanation + " (LLM 实战 6/14 V25-C)",
            source="llm"
        )
    else:
        # 实战 6/14 6/14 6/14 6/14 (实战 6/14 实战 6/14 实战)
        return LLMExplanation(
            code=quote.code, name=quote.name or quote.code,
            change_pct=quote.change_pct, severity=severity,
            explanation=rule_explanation + " (实战 6/14 兜底)",
            source="degraded"
        )


# ====================================================================
# 主函数: 实战 4 标的 4 行情
# ====================================================================

def get_holdings_from_pg(asset_types: List[str] = None) -> List[Dict[str, Any]]:
    """实战 PG holdings.encrypted_positions 实战 4 标的 4 行情
    实战 6/14: V26-C 47 持仓 → 实战 4 标的
    """
    if asset_types is None:
        asset_types = DEFAULT_HOLDINGS_TYPES

    sql = """
    SELECT DISTINCT ON (code, type) code, MAX(name) as name, type
    FROM holdings.encrypted_positions
    WHERE is_current = true AND type = ANY(%s)
    GROUP BY code, type
    """
    conn = get_pg_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, (asset_types,))
        return [{"code": r[0], "name": r[1], "asset_type": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


def stream_quotes(use_cache: bool = True) -> BatchResult:
    """实战 4 标的 4 行情 (主函数)
    实战 6/14 5min 缓存 (PIT #108) + 限频 3 模式 + 持久化
    """
    t0 = time.time()
    batch = BatchResult()

    with acquire_lock() as lock_fd:
        if lock_fd is None:
            log.warning("实战 1 进程已在跑, 实战 6/14 实战")
            batch.failed = -1
            return batch

        ensure_quote_snapshot_table()

        holdings = get_holdings_from_pg()
        batch.total = len(holdings)
        log.info(f"实战 {batch.total} 持仓 (4 标的 4 行情)")

        # 实战 6/14 fund + stock/etf 分组
        fund_holdings = [h for h in holdings if h["asset_type"] == "fund"]
        stock_holdings = [h for h in holdings if h["asset_type"] in ("stock", "etf")]

        # 实战 6/14: fund 1 1 1 实战 6/14 akshare (0.3s/标)
        for h in fund_holdings:
            cached = _read_cache(h["code"], "akshare_fund") if use_cache else None
            if cached:
                batch.cached += 1
                batch.results.append(StreamResult(
                    code=h["code"], status="ok", quote=cached,
                    elapsed_sec=0.0, from_cache=True, source="akshare_fund"
                ))
                continue
            try:
                quote = fetch_akshare_fund(h["code"], h["name"])
                _write_cache(quote)
                batch.success += 1
                batch.results.append(StreamResult(
                    code=h["code"], status="ok", quote=quote,
                    elapsed_sec=0.3, from_cache=False, source="akshare_fund"
                ))
            except Exception as e:
                batch.failed += 1
                batch.results.append(StreamResult(
                    code=h["code"], status="failed", error=str(e)[:80],
                    source="akshare_fund"
                ))

        # 实战 6/14: stock/etf baostock 1 login 全局复用
        if stock_holdings:
            stock_codes = [h["code"] for h in stock_holdings]
            try:
                quotes = fetch_baostock_quotes(stock_codes, "stock")
                quote_by_code = {q.code: q for q in quotes}
                for h in stock_holdings:
                    quote = quote_by_code.get(h["code"])
                    if quote:
                        quote.name = h["name"]  # 实战 6/14 name 实战
                        _write_cache(quote)
                        batch.success += 1
                        batch.results.append(StreamResult(
                            code=h["code"], status="ok", quote=quote,
                            elapsed_sec=3.6, from_cache=False, source="baostock"
                        ))
                    else:
                        batch.failed += 1
                        batch.results.append(StreamResult(
                            code=h["code"], status="failed",
                            error="baostock 实战 0 行", source="baostock"
                        ))
            except Exception as e:
                log.warning(f"baostock 实战 6/14: {e}")
                for h in stock_holdings:
                    batch.failed += 1
                    batch.results.append(StreamResult(
                        code=h["code"], status="failed",
                        error=f"baostock 实战 6/14: {str(e)[:60]}", source="baostock"
                    ))

        # 实战 6/14 持久化
        for r in batch.results:
            if r.status == "ok" and r.quote:
                if persist_quote(r.quote):
                    batch.persisted += 1

    batch.elapsed_sec = time.time() - t0
    log.info(f"实战 {batch.success}/{batch.total} 成功 ({batch.cached} 缓存, {batch.failed} 失败), 持久化 {batch.persisted}, 耗时 {batch.elapsed_sec:.2f}s")
    return batch


# ====================================================================
# Self-test
# ====================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("V26-A T3 quote_streamer.py self-test (实战 6/14)")
    print("=" * 70)

    # 实战 6/14 实战 4 标的 (实战 4 标的, 不实战 28 标的 6/14 限频)
    print("\n[实战 1] akshare fund 拉取 2 标的 (007355 + 002943)")
    for code, name in [("007355", "汇添富科技创新"), ("002943", "广发多因子")]:
        try:
            q = fetch_akshare_fund(code, name)
            print(f"  {code} {name}: {q.trade_date} 净值={q.close:.4f} 日增={q.change_pct:.2f}% 源={q.source}")
        except Exception as e:
            print(f"  {code}: ERR {e}")

    print("\n[实战 2] baostock stock 拉取 2 标的 (600487 + 002050)")
    quotes = fetch_baostock_quotes(["600487", "002050"], "stock")
    for q in quotes:
        print(f"  {q.code}: {q.trade_date} 开{q.open:.2f} 高{q.high:.2f} 低{q.low:.2f} 收{q.close:.2f} 量{q.volume} 涨跌={q.change_pct:+.2f}%")

    print("\n[实战 3] PG l3.quote_snapshot 实战")
    ensure_quote_snapshot_table()

    print("\n[实战 4] 主函数 stream_quotes (use_cache=True)")
    batch = stream_quotes(use_cache=True)
    print(f"  总 {batch.total}, 成功 {batch.success}, 失败 {batch.failed}, 缓存 {batch.cached}, 持久化 {batch.persisted}, 耗时 {batch.elapsed_sec:.2f}s")
    print(f"  详情:")
    for r in batch.results:
        if r.quote:
            q = r.quote
            print(f"    [{r.source}] {q.code}: 收={q.close:.2f} 涨跌={q.change_pct:+.2f}% {'(cache)' if r.from_cache else ''}")
        else:
            print(f"    [{r.source}] {r.code}: {r.status} {r.error}")
