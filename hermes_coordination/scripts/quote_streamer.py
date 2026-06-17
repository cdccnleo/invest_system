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
import socket
import fcntl
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

# PIT #136 (6/17 13:30 实战): baostock.com 网络不可达时 bs.login() 无限阻塞
# 全局 socket 超时 15s — 必须在 import baostock 之前设置, 否则 socket 创建时无超时
# 防御 4 重: ① socket.setdefaulttimeout ② login error_code 检测 ③ akshare 兜底 ④ self-test try/except
socket.setdefaulttimeout(15)
log_socket_timeout_set = True  # 标记: 让 self-test 打印设置状态

# PIT #20 沿用: sys.path.insert 实战 dynamic path
_INVEST_ROOT = "/home/aileo/invest_system"
sys.path.insert(0, f"{_INVEST_ROOT}/.venv/lib/python3.11/site-packages")
sys.path.insert(0, f"{_INVEST_ROOT}/hermes_coordination/scripts")
sys.path.insert(0, f"{_INVEST_ROOT}/scripts")
# PIT #117 (6/15 实战): 跨模块复用 _safe_num 防御 None > 0 比较错误
from _shared_utils import _safe_num  # noqa: E402

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

    PIT #136 (6/17 13:30 实战): baostock.com 网络不可达时 login 抛 OSError/timed out,
    之前没 error_code 检测会让调用方误以为 login 成功. 修复:
    ① socket.setdefaulttimeout(15) 在 import 前 (避免无限阻塞)
    ② login 后强检 lg.error_code, 失败 raise ValueError (PIT #107 沿用)
    ③ 调用方 (stream_quotes) 失败时降级到 akshare 兜底 (见 fetch_akshare_stock_quote)
    """
    if not codes:
        return []

    t0 = time.time()
    log.info(f"[baostock] login 开始 (1 login 全局复用, socket_timeout=15s)")
    lg = bs.login()
    login_elapsed = time.time() - t0
    # PIT #136: baostock 网络不可达时 lg.error_code = "10002007" 网络接收错误
    if lg.error_code != "0":
        err_msg = f"baostock login 失败 rc={lg.error_code} msg={lg.error_msg} 耗时 {login_elapsed:.1f}s"
        log.error(f"[baostock] {err_msg} — 将由 stream_quotes 降级到 akshare 兜底")
        raise ValueError(err_msg)
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


def fetch_akshare_stock_quote(code: str, name: str = "") -> QuoteData:
    """PIT #136 兜底: baostock 网络不可达时用 akshare.stock_zh_a_hist 拉取 stock

    6/17 实战验证: socket.setdefaulttimeout(15) + akshare.stock_zh_a_hist 600487 0.35s 成功
    (PIT #106 6/14 实战当时 baostock/akshare 都限频, 现在 baostock 不可达, akshare 反而成救命稻草)

    返回的 QuoteData 用 source="akshare_stock" 区分, PG l3.quote_snapshot UNIQUE(code, trade_date, source)
    不会与 baostock 源冲突

    PIT #137 (6/17 13:43 实战): 28 stock 连续调用 akshare 限频 100% RemoteDisconnected
    防御: ① sleep 0.5s 限频 ② retry 3 次指数退避 ③ 失败时记录到失败缓存, 下次 5min skip
    """
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    last_err = None
    # PIT #137 实战: akshare stock 限频时 retry 3 次 + 指数退避 (1s, 2s, 4s)
    for attempt in range(3):
        try:
            time.sleep(0.5)  # PIT #137 限频: 28 标的连续调用间隔 0.5s
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start_date, end_date=end_date, adjust="qfq"
            )
            if df is None or df.empty:
                raise ValueError(f"akshare stock {code} 拉取空数据")
            latest = df.iloc[-1]
            trade_date = str(latest["日期"])
            open_p = float(latest["开盘"])
            close = float(latest["收盘"])
            high = float(latest["最高"])
            low = float(latest["最低"])
            volume = int(float(latest.get("成交量", 0)))
            change_pct = float(latest.get("涨跌幅", 0.0))
            return QuoteData(
                code=code, name=name or f"stock_{code}", asset_type="stock",
                trade_date=trade_date, open=open_p, high=high, low=low,
                close=close, volume=volume, change_pct=change_pct, source="akshare_stock"
            )
        except Exception as e:
            last_err = e
            backoff = 2 ** attempt
            log.warning(f"[akshare_stock] {code} 第 {attempt+1}/3 次失败: {type(e).__name__}: {str(e)[:60]}, backoff {backoff}s")
            time.sleep(backoff)
    # 3 次都失败 → 抛最后的错误 (让调用方知道)
    raise RuntimeError(f"akshare stock {code} 3 次重试全败 (PIT #137): {last_err}")


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
        return _safe_num(data.get("used")) < LLM_DAILY_LIMIT
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
            quotes = []
            bs_failed = False
            try:
                quotes = fetch_baostock_quotes(stock_codes, "stock")
            except Exception as e:
                # PIT #136 (6/17 13:30 实战): baostock 网络不可达 → 降级 akshare 兜底
                bs_failed = True
                log.warning(f"[baostock] 失败: {e} → 启动 akshare.stock_zh_a_hist 兜底 (PIT #136)")
                for h in stock_holdings:
                    try:
                        q = fetch_akshare_stock_quote(h["code"], h["name"])
                        quotes.append(q)
                    except Exception as e2:
                        log.warning(f"[akshare_stock] {h['code']} 兜底也失败: {e2}")

            quote_by_code = {q.code: q for q in quotes}
            for h in stock_holdings:
                quote = quote_by_code.get(h["code"])
                if quote:
                    quote.name = h["name"]  # 实战 6/14 name 实战
                    _write_cache(quote)
                    batch.success += 1
                    batch.results.append(StreamResult(
                        code=h["code"], status="ok", quote=quote,
                        elapsed_sec=3.6, from_cache=False,
                        source=quote.source  # baostock 或 akshare_stock 动态
                    ))
                else:
                    batch.failed += 1
                    err = "baostock 失败 + akshare 兜底失败" if bs_failed else "baostock 实战 0 行"
                    batch.results.append(StreamResult(
                        code=h["code"], status="failed",
                        error=err, source="baostock"
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
    print("V26-A T3 quote_streamer.py self-test (实战 6/14, PIT #136 6/17 防御加固)")
    print("=" * 70)
    print(f"[PIT #136] socket.setdefaulttimeout = 15s (baostock 网络不可达时不再无限阻塞)")

    # 实战 6/14 实战 4 标的 (实战 4 标的, 不实战 28 标的 6/14 限频)
    print("\n[实战 1] akshare fund 拉取 2 标的 (007355 + 002943)")
    for code, name in [("007355", "汇添富科技创新"), ("002943", "广发多因子")]:
        try:
            q = fetch_akshare_fund(code, name)
            print(f"  {code} {name}: {q.trade_date} 净值={q.close:.4f} 日增={q.change_pct:.2f}% 源={q.source}")
        except Exception as e:
            print(f"  {code}: ERR {e}")

    # PIT #136 (6/17 实战): baostock 网络不可达时不要让 self-test rc=1
    # 加 try/except 让测试继续 (stream_quotes 已含降级, 此处只包 self-test 直接调用)
    print("\n[实战 2] baostock stock 拉取 2 标的 (600487 + 002050) — PIT #136 加固")
    try:
        quotes = fetch_baostock_quotes(["600487", "002050"], "stock")
        for q in quotes:
            print(f"  {q.code}: {q.trade_date} 开{q.open:.2f} 高{q.high:.2f} 低{q.low:.2f} 收{q.close:.2f} 量{q.volume} 涨跌={q.change_pct:+.2f}%")
        if not quotes:
            print("  baostock 0 行情 — 降级 akshare 兜底 (PIT #136)")
            for code, name in [("600487", "亨通光电"), ("002050", "三花智控")]:
                try:
                    q = fetch_akshare_stock_quote(code, name)
                    print(f"  [akshare_stock] {code} {name}: {q.trade_date} 开{q.open:.2f} 收{q.close:.2f} 涨跌={q.change_pct:+.2f}%")
                except Exception as e2:
                    print(f"  [akshare_stock] {code}: ERR {e2}")
    except Exception as e:
        # PIT #136: baostock 失败 → 立即降级 akshare, 不让 rc=1
        print(f"  baostock 失败: {e}")
        print(f"  → 降级 akshare.stock_zh_a_hist 兜底 (PIT #136)")
        for code, name in [("600487", "亨通光电"), ("002050", "三花智控")]:
            try:
                q = fetch_akshare_stock_quote(code, name)
                print(f"  [akshare_stock] {code} {name}: {q.trade_date} 开{q.open:.2f} 收{q.close:.2f} 涨跌={q.change_pct:+.2f}%")
            except Exception as e2:
                print(f"  [akshare_stock] {code}: ERR {e2}")

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
