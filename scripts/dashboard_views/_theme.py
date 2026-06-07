"""主题自动切换 — 盘中深色/盘后浅色"""
import streamlit as st
from datetime import time

TRADING_MORNING  = (time(9, 30), time(11, 30))
TRADING_AFTERNOON = (time(13, 0), time(15, 0))

def is_trading_time() -> bool:
    """判断当前是否为交易时间（粗略，非严格交易日检查）"""
    from datetime import datetime
    now = datetime.now().time()
    in_morning = TRADING_MORNING[0] <= now <= TRADING_MORNING[1]
    in_afternoon = TRADING_AFTERNOON[0] <= now <= TRADING_AFTERNOON[1]
    return in_morning or in_afternoon

def get_auto_theme() -> str:
    """返回 'dark' 或 'light'"""
    return "dark" if is_trading_time() else "light"

def apply_auto_theme():
    """在 Streamlit 页面初始化时自动注入主题"""
    # 优先使用手动覆盖
    if st.session_state.get("manual_theme_override"):
        theme = st.session_state["manual_theme_override"]
    else:
        theme = get_auto_theme()

    if theme == "dark":
        st.markdown("""
        <style>
        /* 强制深色主题覆盖 */
        [data-testid="stAppViewContainer"] {
            background-color: #0e1117;
            color: #fafafa;
        }
        [data-testid="stHeader"] { background-color: #1a1d23; }
        [data-testid="stSidebar"] { background-color: #1a1d23; }
        </style>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <style>
        /* 浅色主题 — 使用浏览器默认 */
        </style>
        """, unsafe_allow_html=True)

    return theme