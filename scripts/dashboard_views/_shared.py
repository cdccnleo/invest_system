"""
dashboard.py — Phase 2 Web 仪表盘 (Streamlit)
持仓视图 v0.1
"""

import os, sys, json, csv
from pathlib import Path
from datetime import datetime

# ── 必须在 import streamlit 之前设置 ───────────────────────────────────────
os.environ[" STREAMLIT_SERVER_HEADLESS"] = "true"

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# ── 路径设置 ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# ── Dashboard 主程序（st.set_page_config 必须最早执行）─────────────────────────
POSITIONS_CSV = os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv")

st.set_page_config(
    page_title="InvestPilot Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 仪表盘密码保护（session 级一次认证）───────────────────────────────────────
_DASHBOARD_PASSWORD = os.environ.get(
    "DASHBOARD_PASSWORD",
    os.path.expanduser("~/.hermes/invest_credentials/store.json"),
)
_cached_pw = None


def _get_dashboard_password():
    """从凭据文件读取仪表盘密码，不依赖环境变量"""
    global _cached_pw
    if _cached_pw is not None:
        return _cached_pw
    import json

    cred_file = os.path.expanduser("~/.hermes/invest_credentials/store.json")
    try:
        with open(cred_file) as f:
            store = json.load(f)
        _cached_pw = store.get("DASHBOARD_PASSWORD", "")
    except Exception:
        _cached_pw = ""
    return _cached_pw


def _check_auth():
    """Session 级认证：首次访问时检查密码，之后跳过。"""
    if st.session_state.get("dashboard_auth_ok"):
        return
    pw = _get_dashboard_password()
    if not pw:
        st.session_state["dashboard_auth_ok"] = True
        return

    st.markdown("## 🔐 InvestPilot 认证")
    pw_input = st.text_input("请输入访问密码", type="password", key="auth_pw_input")

    if pw_input:
        if pw_input == pw:
            st.session_state["dashboard_auth_ok"] = True
            st.rerun()
        else:
            st.error("密码错误，请重试")
            st.stop()
    else:
        st.info("请输入密码以访问仪表盘")
        st.stop()


# 初始化 session_state 中的 auth flag
if "dashboard_auth_ok" not in st.session_state:
    st.session_state["dashboard_auth_ok"] = False

# 认证未通过则展示登录页（set_page_config 已执行，不违规）
if not st.session_state.get("dashboard_auth_ok"):
    _check_auth()
    st.stop()


# ── 数据加载 ───────────────────────────────────────────────────────────────


def load_positions():
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
    except:
        return 0
    finally:
        conn.close()


# ── 侧边栏 ─────────────────────────────────────────────────────────────────
