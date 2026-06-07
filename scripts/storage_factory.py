"""
storage_factory.py — 存储后端工厂
支持 PostgreSQL（主路径）→ SQLite（降级路径）
"""

import os
import json
import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from contextlib import contextmanager

import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# 优先使用 credentials 模块（支持 WCM / 本地文件 / 环境变量）
try:
    from credentials import get_credential
    _HAS_CREDENTIALS = True
except ImportError:
    _HAS_CREDENTIALS = False

DATABASE_URL = os.environ.get("DATABASE_URL", "")
POSITIONS_CSV = os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv")
HISTORY_DIR = os.environ.get("HISTORY_DIR", "/mnt/d/Hold/invest-data/history")
FALLBACK_DB = "/tmp/investpilot_fallback.db"


# ─── 数据库连接参数解析 ──────────────────────────────────────────────────────

def _parse_database_url(url: str) -> Optional[dict]:
    """解析 postgresql:// URL，提取连接参数。失败返回 None。"""
    if not url or "***" in url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme != "postgresql":
            return None
        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "user": parsed.username or "postgres",
            "password": parsed.password or "",
            "dbname": parsed.path.lstrip("/") or "postgres",
        }
    except Exception:
        return None


def _get_db_connection_params() -> Optional[dict]:
    """获取数据库连接参数。优先解析 DATABASE_URL，否则使用 DB_* 环境变量。"""
    # 1. 尝试从 credentials 模块获取 DATABASE_URL
    db_url = None
    if _HAS_CREDENTIALS:
        db_url = get_credential("DATABASE_URL")
    if not db_url:
        db_url = DATABASE_URL

    # 2. 解析 DATABASE_URL
    params = _parse_database_url(db_url)
    if params:
        return params

    # 3. 回退到 DB_* 环境变量
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "dbname": os.environ.get("DB_NAME", "investpilot"),
    }


# ─── PostgreSQL 连接 ────────────────────────────────────────────────────────

def get_pg_connection():
    """建立 PostgreSQL 连接，失败则降级到 SQLite"""
    params = _get_db_connection_params()
    if params is None:
        print("[WARN] 无法获取数据库连接参数，降级到 SQLite")
        return None

    try:
        conn = psycopg2.connect(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            dbname=params["dbname"],
            connect_timeout=5,
        )
        conn.autocommit = True
        return conn
    except psycopg2.OperationalError as e:
        print(f"[WARN] PostgreSQL 连接失败: {e}，降级到 SQLite")
        return None


@contextmanager
def pg_cursor():
    """PostgreSQL 游标上下文管理器"""
    conn = get_pg_connection()
    if conn is None:
        yield None, None
        return
    try:
        cursor = conn.cursor()
        yield conn, cursor
    finally:
        cursor.close()
        conn.close()


# ─── SQLite 降级 ────────────────────────────────────────────────────────────

def get_fallback_conn():
    os.makedirs(os.path.dirname(FALLBACK_DB), exist_ok=True)
    conn = sqlite3.connect(FALLBACK_DB)
    _init_fallback_schema(conn)
    return conn


def _init_fallback_schema(conn):
    """初始化 SQLite 降级 schema（仅保留最近 7 天数据）"""
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS daily_quotes_fallback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_code TEXT, trade_date TEXT, close_price REAL,
            change_pct REAL, volume INTEGER, source TEXT,
            UNIQUE(ts_code, trade_date)
        );
        CREATE TABLE IF NOT EXISTS news_fallback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, content TEXT, source TEXT,
            published_at TEXT, severity TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_fallback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT, operator TEXT, detail TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


# ─── 存储工厂 ───────────────────────────────────────────────────────────────

class StorageBackend:
    """统一存储接口，PostgreSQL 不可用时自动降级"""

    def __init__(self):
        self.pg_available = get_pg_connection() is not None
        self._pg_conn = None

    def _ensure_pg(self):
        if not self.pg_available:
            return False
        self._pg_conn = get_pg_connection()
        return self._pg_conn is not None

    # ── 行情数据写入 ────────────────────────────────────────────────────────

    def write_quotes(self, quotes: list[dict]) -> int:
        """写入日行情数据，返回写入行数"""
        if not quotes:
            return 0
        if self._ensure_pg():
            return self._write_quotes_pg(quotes)
        else:
            return self._write_quotes_sqlite(quotes)

    def _write_quotes_pg(self, quotes: list[dict]) -> int:
        conn = self._pg_conn
        cur = conn.cursor()
        rows = 0
        for q in quotes:
            try:
                cur.execute("""
                    INSERT INTO market.daily_quotes
                        (ts_code, trade_date, open_price, high_price, low_price,
                         close_price, volume, amount, change_pct, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ts_code, trade_date) DO UPDATE SET
                        close_price = EXCLUDED.close_price,
                        change_pct = EXCLUDED.change_pct,
                        volume = CASE WHEN EXCLUDED.volume > 0 THEN EXCLUDED.volume ELSE market.daily_quotes.volume END,  # noqa: E501
                        source = EXCLUDED.source
                """, (
                    q["ts_code"], q["trade_date"],
                    q.get("open", 0), q.get("high", 0), q.get("low", 0),
                    q["close"], q.get("volume", 0), q.get("amount", 0),
                    q.get("change_pct", 0), q.get("source", "unknown")
                ))
                rows += 1
            except Exception as e:
                print(f"[WARN] 写入行情失败 {q.get('ts_code')}: {e}")
        conn.commit()
        return rows

    def _write_quotes_sqlite(self, quotes: list[dict]) -> int:
        conn = get_fallback_conn()
        cur = conn.cursor()
        rows = 0
        for q in quotes:
            try:
                cur.execute("""
                    INSERT OR REPLACE INTO daily_quotes_fallback
                        (ts_code, trade_date, close_price, change_pct, volume, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (q["ts_code"], q["trade_date"], q["close"],
                      q.get("change_pct", 0), q.get("volume", 0),
                      q.get("source", "akshare")))
                rows += 1
            except Exception:
                pass
        conn.commit()
        conn.close()
        return rows

    # ── 指数数据写入 ────────────────────────────────────────────────────────

    def write_indices(self, indices: list[dict]) -> int:
        if not indices:
            return 0
        if self._ensure_pg():
            return self._write_indices_pg(indices)
        return 0

    def _write_indices_pg(self, indices: list[dict]) -> int:
        conn = self._pg_conn
        cur = conn.cursor()
        rows = 0
        for idx in indices:
            try:
                cur.execute("""
                    INSERT INTO market.indices
                        (index_code, trade_date, close_price, change_pct, volume)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (index_code, trade_date) DO UPDATE SET
                        close_price = EXCLUDED.close_price,
                        change_pct = EXCLUDED.change_pct
                """, (idx["index_code"], idx["trade_date"],
                      idx["close"], idx.get("change_pct", 0),
                      idx.get("volume", 0)))
                rows += 1
            except Exception as e:
                print(f"[WARN] 写入指数失败: {e}")
        conn.commit()
        return rows

    # ── 新闻写入 ───────────────────────────────────────────────────────────

    def write_news(self, news: list[dict]) -> int:
        if not news:
            return 0
        if self._ensure_pg():
            return self._write_news_pg(news)
        return 0

    # ── 公告写入 ───────────────────────────────────────────────────────────

    def write_announcements(self, announcements: list[dict]) -> int:
        """写入公告列表，ON CONFLICT (ts_code, ann_id) DO NOTHING"""
        if not announcements:
            return 0
        if self._ensure_pg():
            return self._write_announcements_pg(announcements)
        return 0

    def _write_news_pg(self, news: list[dict]) -> int:
        conn = self._pg_conn
        cur = conn.cursor()
        rows = 0
        for n in news:
            try:
                cur.execute("""
                    INSERT INTO research.news_articles
                        (title, content, source, url, published_at, severity, keywords)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (title, published_at) DO NOTHING
                    RETURNING id
                """, (
                    n["title"],
                    n.get("content", ""),
                    n.get("source", ""),
                    n.get("url", ""),
                    n.get("published_at"),
                    n.get("severity", "LOW"),
                    n.get("keywords", []),
                ))
                if cur.fetchone():
                    rows += 1
            except Exception:
                pass
        conn.commit()
        return rows

    # ── 审计日志 ───────────────────────────────────────────────────────────

    def write_audit(self, event_type: str, operator: str,
                    target_type: str = "", target_id: str = "",
                    detail: dict = None, result: str = "SUCCESS") -> bool:
        detail = detail or {}
        if self._ensure_pg():
            return self._write_audit_pg(event_type, operator, target_type, target_id, detail, result)  # noqa: E501
        return self._write_audit_sqlite(event_type, operator, detail)

    def _write_audit_pg(self, event_type, operator, target_type, target_id, detail, result) -> bool:
        conn = self._pg_conn
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO audit.audit_log
                    (event_type, operator, target_type, target_id, detail, result)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (event_type, operator, target_type, target_id,
                  json.dumps(detail, ensure_ascii=False), result))
            conn.commit()
            return True
        except Exception as e:
            print(f"[WARN] 审计日志写入失败: {e}")
            return False

    def _write_announcements_pg(self, announcements: list[dict]) -> int:
        """写入公告到 PostgreSQL，ON CONFLICT (ts_code, ann_id) DO NOTHING"""
        conn = self._pg_conn
        cur = conn.cursor()
        rows = 0
        for a in announcements:
            try:
                cur.execute("""
                    INSERT INTO research.announcements
                        (ts_code, notice_date, title, ann_type, url, ann_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ts_code, ann_id) DO NOTHING
                    RETURNING id
                """, (
                    a["ts_code"],
                    a["notice_date"],
                    a["title"],
                    a.get("ann_type", "一般公告"),
                    a.get("url", ""),
                    a.get("ann_id", ""),
                ))
                if cur.fetchone():
                    rows += 1
            except Exception as e:
                print(f"[WARN] 公告写入失败 ({a.get('title','?')[:30]}): {e}")
        conn.commit()
        cur.close()
        return rows

    def _write_international_research_pg(self, articles: list[dict]) -> int:
        """写入国际投行研究到 PostgreSQL，ON CONFLICT (title, source) DO NOTHING"""
        conn = self._pg_conn
        cur = conn.cursor()
        rows = 0
        for a in articles:
            try:
                cur.execute("""
                    INSERT INTO research.international_bank_research
                        (title, content, link, published_at, author, source,
                         source_type, cited_institutions, article_type, is_bank_related)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (title, source) DO NOTHING
                    RETURNING id
                """, (
                    a.get("title", ""),
                    a.get("desc", ""),      # content
                    a.get("link", ""),
                    a.get("published_at", ""),
                    a.get("author", ""),
                    a.get("source", ""),
                    a.get("source_type", ""),
                    a.get("cited_institutions", []),
                    a.get("article_type", ""),
                    a.get("is_bank_related", False),
                ))
                if cur.fetchone():
                    rows += 1
            except Exception as e:
                print(f"[WARN] 国际投行研究写入失败 ({a.get('title','?')[:30]}): {e}")
        conn.commit()
        cur.close()
        return rows

    def write_international_research(self, articles: list[dict]) -> int:
        """写入国际投行研究，ON CONFLICT (title, source) DO NOTHING"""
        if not articles:
            return 0
        if self._ensure_pg():
            return self._write_international_research_pg(articles)
        return 0

    def _write_audit_sqlite(self, event_type, operator, detail) -> bool:
        conn = get_fallback_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO audit_fallback (event_type, operator, detail)
                VALUES (?, ?, ?)
            """, (event_type, operator, json.dumps(detail, ensure_ascii=False)))
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False

    # ── 数据查询 ───────────────────────────────────────────────────────────

    def get_latest_quotes(self, ts_codes: list[str], days: int = 5) -> list[dict]:
        """查询最近 N 日行情"""
        if not ts_codes:
            return []
        if self._ensure_pg():
            return self._get_latest_quotes_pg(ts_codes, days)
        return []

    def _get_latest_quotes_pg(self, ts_codes: list[str], days: int) -> list[dict]:
        conn = self._pg_conn
        cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(ts_codes))
        try:
            cur.execute(f"""
                SELECT ts_code, trade_date, close_price, change_pct, volume
                FROM market.daily_quotes
                WHERE ts_code IN ({placeholders})
                  AND trade_date >= CURRENT_DATE - INTERVAL '{days} days'
                ORDER BY ts_code, trade_date DESC
            """, ts_codes)
            cols = ["ts_code", "trade_date", "close_price", "change_pct", "volume"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            print(f"[WARN] 查询行情失败: {e}")
            return []

    def get_index_history(self, index_code: str, days: int = 5) -> list[dict]:
        """查询指数历史"""
        if self._ensure_pg():
            return self._get_index_history_pg(index_code, days)
        return []

    def _get_index_history_pg(self, index_code: str, days: int) -> list[dict]:
        conn = self._pg_conn
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT index_code, trade_date, close_price, change_pct
                FROM market.indices
                WHERE index_code = %s
                  AND trade_date >= CURRENT_DATE - (INTERVAL '1 day' * %s)
                ORDER BY trade_date DESC
            """, (index_code, days))
            cols = ["index_code", "trade_date", "close_price", "change_pct"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            print(f"[WARN] 查询指数失败: {e}")
            return []

    def get_recent_news(self, limit: int = 15) -> list[dict]:
        """查询最近 N 条新闻"""
        if self._ensure_pg():
            return self._get_recent_news_pg(limit)
        return []

    def _get_recent_news_pg(self, limit: int) -> list[dict]:
        conn = self._pg_conn
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT title, content, source, published_at, severity
                FROM research.news_articles
                ORDER BY published_at DESC
                LIMIT %s
            """, (limit,))
            cols = ["title", "content", "source", "published_at", "severity"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            print(f"[WARN] 查询新闻失败: {e}")
            return []

    def close(self):
        if self._pg_conn:
            self._pg_conn.close()


# ─── 全局单例 ────────────────────────────────────────────────────────────────

_storage: Optional[StorageBackend] = None

def get_storage() -> StorageBackend:
    global _storage
    if _storage is None:
        _storage = StorageBackend()
    return _storage
