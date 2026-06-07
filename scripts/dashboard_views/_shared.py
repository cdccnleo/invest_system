"""
Dashboard shared utilities — 数据加载 / 数据库连接 / 行情查询

注意：本模块不包含任何 streamlit 配置（set_page_config）或认证逻辑，
      那些职责由 __main__.py 全权负责。
"""
import os
import streamlit as st
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
import sys as _sys
_sys.path.insert(0, str(ROOT / "scripts"))


def load_positions() -> list[dict]:
    """加载所有账号合并持仓（兼容旧接口）"""
    from account_manager import get_all_positions
    positions = get_all_positions()
    # 计算每条持仓的仓位%（市值/总市值*100），account_manager 不提供 weight
    total_mv = sum(p.get("market_value", 0) for p in positions) or 1
    # 转换为旧接口的列名格式
    return [
        {
            "代码": p["code"],
            "名称": p["name"],
            "类型": p.get("type", "stock"),
            "份额": p["shares"],
            "成本": p["avg_cost"],
            "市值": p["market_value"],
            "仓位%": (p["market_value"] / total_mv * 100) if p.get("market_value") else 0,
            "账号": p.get("account", "main"),
        }
        for p in positions
    ]


def get_db_connection():
    """获取 PostgreSQL 连接（每次新建，避免缓存的连接被 PG 服务器关闭）

    早期版本用 @st.cache_resource(ttl=3600) 缓存连接，但 cron job
    / PG idle timeout / 服务重启会导致缓存的连接对象已关闭，
    下次使用抛 'connection already closed' 异常。
    psycopg2.connect() 本身开销小（~10ms），用每次新建更安全。
    """
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


@st.cache_data(ttl=60)
def get_latest_quotes_from_db(codes: list[str]) -> dict:
    """从 PostgreSQL 读取最新行情（每次创建临时连接，不缓存连接对象）"""
    try:
        from storage_factory import get_storage
        storage = get_storage()
        conn = storage._pg_conn
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
        finally:
            cur.close()
            storage.close()
    except Exception:
        return {}


@st.cache_data(ttl=120)
def get_news_count() -> int:
    """统计7日内新闻数量（每次创建临时连接，不缓存连接对象）"""
    try:
        from storage_factory import get_storage
        storage = get_storage()
        # 必须先调 _ensure_pg() 懒加载连接，否则 _pg_conn 是 None
        if not storage._ensure_pg() or storage._pg_conn is None:
            return 0
        conn = storage._pg_conn
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT COUNT(*) FROM research.news_articles
                WHERE published_at >= CURRENT_DATE - INTERVAL '7 days'
            """)
            return cur.fetchone()[0] or 0
        finally:
            cur.close()
            storage.close()
    except Exception:
        return 0


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
