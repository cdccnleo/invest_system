"""
dashboard.py — Phase 2 Web 仪表盘 (Streamlit)
Thin launcher: set_page_config + 认证 + PWA，视图逻辑全在 dashboard_views/。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # scripts/
os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
import streamlit as st

# ── PWA 初始化 ─────────────────────────────────────────────────────────────
def _init_pwa():
    try:
        from pwa_service import generate_sw
        generate_sw()
    except Exception:
        pass
    st.html("""<link rel="manifest" href="/static/manifest.json" />
<meta name="theme-color" content="#0ea5e9" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<script>if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/static/sw.js')
  .then(reg => console.log('[PWA] SW registered:', reg.scope))
  .catch(err => console.warn('[PWA] SW failed:', err)); }</script>""")

st.set_page_config(page_title="InvestPilot Dashboard", page_icon="📊",
                   layout="wide", initial_sidebar_state="expanded")
_init_pwa()

# ── 认证 ────────────────────────────────────────────────────────────────────
_cached_pw = None

def _get_dashboard_password():
    global _cached_pw
    if _cached_pw is not None:
        return _cached_pw
    try:
        import json
        with open(os.path.expanduser("~/.hermes/invest_credentials/store.json")) as f:
            _cached_pw = json.load(f).get("DASHBOARD_PASSWORD", "")
    except Exception:
        _cached_pw = ""
    return _cached_pw

def _check_auth():
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

if "dashboard_auth_ok" not in st.session_state:
    st.session_state["dashboard_auth_ok"] = False
if not st.session_state.get("dashboard_auth_ok"):
    _check_auth()
    st.stop()

# ── 委托 ─────────────────────────────────────────────────────────────────────
from dashboard_views.__main__ import main as _dm

if __name__ == "__main__":
    _dm()
