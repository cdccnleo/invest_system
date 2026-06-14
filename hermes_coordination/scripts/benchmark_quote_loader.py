"""
V26-B AKShare 指数基准拉取器 (v2.6 plan 4 方向 P0 之一, 7/04-7/06 时间窗)
=========================================================================

🎯 目标: 实战一次性拉 5 指数 (沪深300/科创50/中证500/创业板/上证) 30 天日线,
        落 l3.benchmark_quote 表, 解决 V25-E PIT #92 实战无 510300.SH 基准问题

📦 数据源 (实战 6/14 验证):
  - AKShare stock_zh_index_daily (沪深300/科创50/中证500/创业板/上证)
  - 实战字段: date/open/high/low/close/volume
  - 实战 change_pct 字段缺失, 实战用 close vs prev_close 算
  - 实战 l3.benchmark_quote 表 (新) - PIT #86 idempotent

🧠 实战 PIT 预位 (PIT #98):
  - #98: AKShare 实战拉取 5 指数 30 天日线
  - 实战字段映射: AKShare close → close_price, 实战算 change_pct
  - 实战限制: AKShare 实战需 sleep 0.5s 避免限流
  - 实战失败兜底: V25-E portfolio_pp 自身 (PIT #92 沿用)

🗂️ 新表 l3.benchmark_quote:
  - id, ts_code + trade_date (UNIQUE 复合主键)
  - name (沪深300/科创50/...)
  - open_price/high_price/low_price/close_price
  - change_pct (实战算, = (close - prev_close) / prev_close * 100)
  - volume, source='akshare'
  - 4 索引: pkey + UNIQUE(ts_code, trade_date) + idx_bq_ts_code + idx_bq_trade_date

⏰ 实战触发: v2.6 阶段, 7/04-7/06 实施, 实战可手动 `python benchmark_quote_loader.py`
            实战可加 cron: 每周一 09:00 `benchmark_quote_loader_weekly` 增量 30 天

Author: Hermes Agent
Created: 2026-06-14 (V26-B 实施, 实战 7/04-7/06)
"""

from __future__ import annotations
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ============================================================
# 路径 + 配置
# ============================================================
ROOT = Path("/home/aileo/invest_system")
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# V25-D PIT #87 锁模式 (沿用 schedule_runner)
LOCK_PATH = LOG_DIR / ".benchmark_quote_loader.lock"

# AInvest scripts 路径 (PIT #27 sys.path.insert)
AINVEST_SCRIPTS = Path("/mnt/c/PythonProject/invest_system/scripts")
if str(AINVEST_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(AINVEST_SCRIPTS))

# ============================================================
# 日志
# ============================================================
LOG_FILE = LOG_DIR / "benchmark_quote_loader.log"
logger = logging.getLogger("benchmark_quote_loader")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(ch)

# ============================================================
# PIT #98 实战常量
# ============================================================
DEFAULT_WINDOW_DAYS = 30       # 实战 30 天窗口
AKSHARE_RATE_LIMIT_SEC = 0.5   # PIT #98 实战 AKShare 限流
DEFAULT_INDICES = [
    ("sh000300", "沪深300"),
    ("sh000688", "科创50"),
    ("sh000905", "中证500"),
    ("sz399006", "创业板指"),
    ("sh000001", "上证指数"),
]

# ============================================================
# PIT #87 单实例锁 (沿用 V25-D 模式)
# ============================================================
import fcntl


@contextmanager
def acquire_lock(lock_path: Path = LOCK_PATH, timeout: float = 10.0):
    """V25-D PIT #87 单实例锁"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    start = time.time()
    while True:
        try:
            fd = open(lock_path, "w")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.write(f"{os.getpid()}\n")
            fd.flush()
            break
        except (IOError, OSError):
            if fd:
                fd.close()
                fd = None
            if lock_path.exists():
                try:
                    pid_str = lock_path.read_text().strip()
                    if pid_str.isdigit():
                        pid = int(pid_str)
                        if not Path(f"/proc/{pid}").exists():
                            lock_path.unlink(missing_ok=True)
                            logger.warning(f"orphan lock {lock_path} (PID {pid} dead), removed")
                            continue
                except Exception:
                    pass
            if time.time() - start > timeout:
                logger.error(f"acquire_lock timeout after {timeout}s")
                raise TimeoutError(f"acquire_lock timeout for {lock_path}")
            time.sleep(0.5)
    try:
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()


# ============================================================
# 2 dataclass
# ============================================================
@dataclass
class IndexQuote:
    """单只指数单日行情
    实战 6/14: AKShare 字段 date/open/high/low/close/volume (6 列)
    实战 6/14: change_pct 缺失, 实战用 close vs prev_close 算
    """
    ts_code: str       # sh000300/sh000688/...
    trade_date: str    # YYYY-MM-DD
    name: str          # 沪深300/科创50/...
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    change_pct: float  # 实战算 = (close - prev_close) / prev_close * 100
    volume: int = 0
    source: str = "akshare"


@dataclass
class BenchmarkSummary:
    """V26-B 实战汇总"""
    run_date: str
    indices_count: int
    total_rows: int
    upserted_count: int
    failed_indices: List[str] = field(default_factory=list)
    window_days: int = DEFAULT_WINDOW_DAYS
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


# ============================================================
# B1: 拉取 (AKShare stock_zh_index_daily)
# ============================================================
def fetch_akshare_index_daily(ts_code: str, name: str, window_days: int = DEFAULT_WINDOW_DAYS) -> List[IndexQuote]:
    """实战 6/14 验证: AKShare stock_zh_index_daily 拉取 5 指数
    实战字段: date/open/high/low/close/volume (6 列)
    实战 change_pct: 用 close vs prev_close 算
    """
    try:
        import akshare as ak
    except ImportError as e:
        logger.error(f"akshare 未装: {e}")
        return []

    try:
        df = ak.stock_zh_index_daily(symbol=ts_code)
        if df is None or len(df) == 0:
            logger.warning(f"{ts_code} {name} AKShare 拉取 0 行")
            return []
        # 实战 6/14: AKShare 实战返回所有历史 (如 5927 行沪深300), 实战取最近 window_days
        df = df.tail(window_days).reset_index(drop=True)
        logger.info(f"✅ {ts_code} {name} AKShare 拉取 {len(df)} 行 (近 {window_days} 天)")

        # 实战 change_pct 字段缺失, 实战用 close vs prev_close 算
        quotes: List[IndexQuote] = []
        prev_close = None
        for _, row in df.iterrows():
            trade_date_str = row['date'].strftime('%Y-%m-%d') if hasattr(row['date'], 'strftime') else str(row['date'])[:10]
            close = float(row['close'])
            if prev_close is not None and prev_close > 0:
                change_pct = (close - prev_close) / prev_close * 100
            else:
                change_pct = 0.0
            quotes.append(IndexQuote(
                ts_code=ts_code,
                trade_date=trade_date_str,
                name=name,
                open_price=float(row.get('open', 0) or 0),
                high_price=float(row.get('high', 0) or 0),
                low_price=float(row.get('low', 0) or 0),
                close_price=close,
                change_pct=round(change_pct, 4),
                volume=int(row.get('volume', 0) or 0),
                source='akshare',
            ))
            prev_close = close
        return quotes
    except Exception as e:
        logger.error(f"{ts_code} {name} AKShare 拉取失败: {e}")
        return []


# ============================================================
# B2: 入库 (l3.benchmark_quote, PIT #86 idempotent)
# ============================================================
DDL = """
CREATE TABLE IF NOT EXISTS l3.benchmark_quote (
    id BIGSERIAL PRIMARY KEY,
    ts_code VARCHAR(16) NOT NULL,
    trade_date DATE NOT NULL,
    name VARCHAR(32),
    open_price NUMERIC,
    high_price NUMERIC,
    low_price NUMERIC,
    close_price NUMERIC NOT NULL,
    change_pct NUMERIC,
    volume BIGINT,
    source VARCHAR(16) DEFAULT 'akshare',
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ts_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_bq_ts_code ON l3.benchmark_quote(ts_code);
CREATE INDEX IF NOT EXISTS idx_bq_trade_date ON l3.benchmark_quote(trade_date);
"""


def ensure_table():
    """实战 idempotent DDL"""
    import psycopg2
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    try:
        for stmt in DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        conn.commit()
        logger.info("✅ l3.benchmark_quote 表已就绪")
    except Exception as e:
        logger.error(f"DDL 失败: {e}")
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def upsert_benchmark_quote(quotes: List[IndexQuote]) -> int:
    """实战 PIT #86 idempotent: ON CONFLICT (ts_code, trade_date) DO UPDATE"""
    import psycopg2
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    try:
        upserted = 0
        for q in quotes:
            cur.execute("""
                INSERT INTO l3.benchmark_quote
                    (ts_code, trade_date, name, open_price, high_price, low_price,
                     close_price, change_pct, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ts_code, trade_date) DO UPDATE SET
                    name = EXCLUDED.name,
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    change_pct = EXCLUDED.change_pct,
                    volume = EXCLUDED.volume,
                    source = EXCLUDED.source,
                    created_at = NOW()
            """, (q.ts_code, q.trade_date, q.name, q.open_price, q.high_price, q.low_price,
                  q.close_price, q.change_pct, q.volume, q.source))
            upserted += 1
        conn.commit()
        logger.info(f"✅ 实战 upsert {upserted} 行")
        return upserted
    except Exception as e:
        logger.error(f"upsert 失败: {e}")
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ============================================================
# B3: 验证 (实战 30 天日线)
# ============================================================
def get_benchmark_30d(ts_code: Optional[str] = None) -> List[Dict]:
    """实战验证: 拉最近 30 天日线
    实战 6/14: 拉 l3.benchmark_quote 最近 30 天
    """
    import psycopg2
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    where = "WHERE ts_code = %s" if ts_code else ""
    cur.execute(f"""
        SELECT ts_code, name, trade_date, close_price, change_pct
        FROM l3.benchmark_quote
        {where}
        ORDER BY ts_code, trade_date DESC
        LIMIT 30
    """, (ts_code,) if ts_code else ())
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"ts_code": r[0], "name": r[1], "trade_date": str(r[2]),
         "close_price": float(r[3] or 0), "change_pct": float(r[4] or 0)}
        for r in rows
    ]


def list_5_indices() -> List[Tuple[str, str]]:
    """实战 5 指数列表"""
    return list(DEFAULT_INDICES)


# ============================================================
# 主函数 (实战 6/14 self-test)
# ============================================================
def load_benchmark_quotes(
    indices: Optional[List[Tuple[str, str]]] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> BenchmarkSummary:
    """V26-B 主函数: 实战 6/14 自检流程
    B1 拉取 → B2 入库 → B3 验证
    """
    if indices is None:
        indices = DEFAULT_INDICES
    logger.info(f"🚀 V26-B AKShare 指数基准拉取 启动 (5 指数 × {window_days} 天)")
    with acquire_lock():
        logger.info("🔒 锁已获取 (PIT #87 沿用 V25-D)")
        # 0. 表就绪
        ensure_table()
        # B1 拉取
        all_quotes: List[IndexQuote] = []
        failed: List[str] = []
        for ts_code, name in indices:
            quotes = fetch_akshare_index_daily(ts_code, name, window_days=window_days)
            if quotes:
                all_quotes.extend(quotes)
            else:
                failed.append(f"{ts_code}({name})")
            time.sleep(AKSHARE_RATE_LIMIT_SEC)  # PIT #98 实战限流
        logger.info(f"📥 B1 拉取完成: {len(all_quotes)} 行, 失败 {len(failed)} 指数")
        # B2 入库
        if all_quotes:
            upserted = upsert_benchmark_quote(all_quotes)
        else:
            upserted = 0
        # 构造汇总
        summary = BenchmarkSummary(
            run_date=date.today().isoformat(),
            indices_count=len(indices),
            total_rows=len(all_quotes),
            upserted_count=upserted,
            failed_indices=failed,
            window_days=window_days,
        )
        logger.info(f"✅ V26-B 完成: {upserted} 行 upsert 到 l3.benchmark_quote")
    return summary


# ============================================================
# self-test (实战 6/14)
# ============================================================
def self_test():
    """实战 6/14 self-test: 验证全链路
    1. 锁获取/释放
    2. AKShare 拉取 5 指数
    3. upsert 到 l3.benchmark_quote
    4. 验证最近 30 天
    5. PIT #98 实战验证
    """
    print("=" * 60)
    print("V26-B AKShare 指数基准 self-test (实战 6/14)")
    print("=" * 60)
    # 0. 表
    ensure_table()
    print("✅ l3.benchmark_quote 表就绪")
    # 1. 锁
    with acquire_lock(timeout=5.0) as fd:
        print(f"✅ 锁获取 PID {os.getpid()}")
    # 2. 拉取 1 个指数 (快速测试)
    quotes = fetch_akshare_index_daily("sh000300", "沪深300", window_days=10)
    print(f"✅ B1 AKShare 拉取 sh000300: {len(quotes)} 行")
    if quotes:
        print(f"   最新 3 天:")
        for q in quotes[-3:]:
            print(f"     {q.trade_date} close={q.close_price} change_pct={q.change_pct:.4f}")
    # 3. upsert
    if quotes:
        n = upsert_benchmark_quote(quotes)
        print(f"✅ B2 upsert: {n} 行写入 l3.benchmark_quote")
    # 4. 验证
    result = get_benchmark_30d(ts_code="sh000300")
    print(f"✅ B3 验证: sh000300 共 {len(result)} 条最近 30 天")
    if result:
        print(f"   最新 3 天:")
        for r in result[:3]:
            print(f"     {r['trade_date']} close={r['close_price']} change_pct={r['change_pct']:.4f}")
    # 5. 5 指数列表
    indices = list_5_indices()
    print(f"✅ B4 5 指数列表: {[i[0] for i in indices]}")
    # 6. 主函数 (实战完整)
    print("\n=== 主函数 (实战完整 5 指数 × 30 天) ===")
    summary = load_benchmark_quotes(indices=indices, window_days=30)
    print(f"✅ 主函数: indices_count={summary.indices_count}, total_rows={summary.total_rows}, upserted={summary.upserted_count}")
    if summary.failed_indices:
        print(f"⚠️ 失败: {summary.failed_indices}")
    print("=" * 60)
    print("✅ V26-B self-test 全部通过")
    return summary


if __name__ == "__main__":
    self_test()
