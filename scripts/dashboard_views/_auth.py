"""Dashboard 认证模块"""
import json
import os
import streamlit as st

_cached_pw = None


def get_dashboard_password():
    """从凭据文件读取仪表盘密码"""
    global _cached_pw
    if _cached_pw is not None:
        return _cached_pw

    cred_file = os.path.expanduser("~/.hermes/invest_credentials/store.json")
    try:
        with open(cred_file) as f:
            store = json.load(f)
        _cached_pw = store.get("DASHBOARD_PASSWORD", "")
    except Exception:
        _cached_pw = ""
    return _cached_pw


def check_auth():
    """Session 级认证：首次访问时检查密码，之后跳过。"""
    if st.session_state.get("dashboard_auth_ok"):
        return True

    pw = get_dashboard_password()
    if not pw:
        st.session_state["dashboard_auth_ok"] = True
        return True

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

    return False