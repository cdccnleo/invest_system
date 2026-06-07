"""
Dashboard shared utilities — 数据加载 / 数据库连接 / 行情查询

注意：本模块不包含任何 streamlit 配置（set_page_config）或认证逻辑，
      那些职责由 __main__.py 全权负责。
"""
import os
import csv
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
import sys as _sys
_sys.path.insert(0, str(ROOT / "scripts"))

POSITIONS_CSV = os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv")


def load_positions() -> list[dict]:
    positions = []
    if os.path.exists(POSITIONS_CSV):
        with open(POSITIONS_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not row.get("code"):
                    continue
                positions.append({
                    "代码": str(row["code"]).zfill(6),
                    "名称": row.get("name", ""),
                    "类型": row.get("type", "stock"),
                    "份额": float(row.get("shares", 0)),
                    "成本": float(row.get("cost", 0)),
                    "市值": float(row.get("market_value", 0)),
                    "仓位%": float(row.get("weight", 0)),
                })
    return positions


def get_db_connection():
    import psycopg2
    try:
        from credentials import get_credential
        pwd = get_credential("DB_PASSWORD")
        if pwd:
            return psycopg2.connect(
                host="localhost",
                user="invest_admin",
                database="investpilot",
                password=pwd,
            )
    except ImportError:
        pass
    return psycopg2.connect(
        host="localhost",
        user="invest_admin",
        database="investpilot",
        password=os.environ.get("DB_PASSWORD", ""),
    )


def get_latest_quotes_from_db(codes: list[str]) -> dict:
    """从 PostgreSQL 读取最新行情"""
    conn = get_db_connection()
    if conn is None:
        return {}

    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(codes))
    try:
        cur.execute(f"""
            SELECT ts_code, close_price, change_pct, trade_date
            FROM market.daily_quotes
            WHERE ts_code IN ({placeholders})
              AND trade_date = CURRENT_DATE
        """, codes)
        return {row[0]: {"close": row[1], "change_pct": row[2], "date": row[3]}
                for row in cur.fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def get_news_count() -> int:
    conn = get_db_connection()
    if conn is None:
        return 0
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT COUNT(*) FROM research.news_articles
            WHERE published_at >= CURRENT_DATE - INTERVAL '7 days'
        """)
        return cur.fetchone()[0] or 0
    except Exception:
        return 0
    finally:
        conn.close()


# ── 手动同步状态管理 ────────────────────────────────────────────────────────

def init_sync_status():
    """初始化同步状态到 session_state"""
    import streamlit as st
    if "sync_status" not in st.session_state:
        st.session_state["sync_status"] = {
            "news": {"last_sync": None, "syncing": False},
            "reports": {"last_sync": None, "syncing": False},
            "announcements": {"last_sync": None, "syncing": False},
        }


def get_sync_status(data_type: str) -> dict:
    """获取指定数据类型的同步状态"""
    import streamlit as st
    init_sync_status()
    return st.session_state["sync_status"].get(data_type, {"last_sync": None, "syncing": False})


def set_sync_status(data_type: str, last_sync=None, syncing=None):
    """更新指定数据类型的同步状态"""
    import streamlit as st
    init_sync_status()
    status = st.session_state["sync_status"][data_type]
    if last_sync is not None:
        status["last_sync"] = last_sync
    if syncing is not None:
        status["syncing"] = syncing


# ── 数据库表初始化 ────────────────────────────────────────────────────────────

def ensure_plan_review_table(conn):
    """确保 analysis schema 和 plan_review 表存在"""
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA IF NOT EXISTS analysis")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS analysis.plan_reviews (
            id SERIAL PRIMARY KEY,
            run_id TEXT NOT NULL,
            plan_index INTEGER NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
            position_pct INTEGER NOT NULL,
            reason TEXT DEFAULT '',
            reviewed_by TEXT DEFAULT 'manual',
            reviewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, plan_index)
        )
    """)
    conn.commit()
