"""InvestPilot Dashboard — Streamlit 多页重构版"""
import os
import sys
from pathlib import Path

os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
import streamlit as st

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
# Streamlit 把 __main__.py 作为脚本运行，__package__ 为空 → 子模块相对导入失败。
# 统一改用绝对导入（见各 _xxx.py），把 dashboard_views 目录加入 sys.path
# 使 from _xxx import ... 能解析到同包内的兄弟模块。
sys.path.insert(0, str(ROOT / "scripts" / "dashboard_views"))

# ── Hermes Profile 加载器 (v2.1 补丁8 集成) ──────────────────────────────────
# 在 sidebar 顶部提供 default / conservative / aggressive 切换
# 影响: 仓位约束 + 风险偏好 + 主题 (conservative→防御配色)
sys.path.insert(0, str(ROOT / "hermes_coordination" / "scripts"))
try:
    from profile_loader import ProfileLoader  # noqa: E402
    _PROFILE_LOADER = ProfileLoader()
    _PROFILE_LIST = _PROFILE_LOADER.list_profiles()
    _PROFILE_LOADER_OK = True
except Exception as _e_prof:
    _PROFILE_LOADER = None
    _PROFILE_LIST = ["default"]
    _PROFILE_LOADER_OK = False
    print(f"[WARN] profile_loader 加载失败, 回退 default: {_e_prof}", file=sys.stderr)

st.set_page_config(page_title="InvestPilot Dashboard", page_icon="📊", layout="wide", initial_sidebar_state="expanded")

# ── Auth & Theme ────────────────────────────────────────────────────────────
from _auth import check_auth
from _theme import apply_auto_theme

# 应用自动主题（盘中深色/盘后浅色，支持手动覆盖）
apply_auto_theme()

if "dashboard_auth_ok" not in st.session_state:
    st.session_state["dashboard_auth_ok"] = False

if not st.session_state.get("dashboard_auth_ok"):
    if not check_auth():
        st.stop()

# ── View Imports ────────────────────────────────────────────────────────────
from _market_view import render as render_market
from _analysis_view import render as render_analysis
from _report_view import render as render_report
from _portfolio import render_portfolio_dashboard
from _news import render_news_summary, render_announcements
from _tamf import render_tamf_memory, render_history
from _l3_status import render_l3_status
from _settings import render_plan_review, render_settings
from _ainvest_kb import render_ainvest_kb
from _shared import load_positions, get_news_count

# ── Navigation Config ───────────────────────────────────────────────────────
PAGES = ["📈 行情", "📊 分析", "📋 研报", "📋 持仓仪表板", "📰 新闻摘要",
         "📢 公告", "📅 决策日历", "📝 计划审核", "📊 TAMF分析记忆",
         "📚 AInvest知识库", "🤖 L3 投资伙伴", "⚙️ 设置"]

VIEW_MAP = {
    "📈 行情": render_market, "📊 分析": render_analysis, "📋 研报": render_report,
    "📋 持仓仪表板": render_portfolio_dashboard, "📰 新闻摘要": render_news_summary,
    "📢 公告": render_announcements, "📅 决策日历": render_history,
    "📝 计划审核": render_plan_review, "📊 TAMF分析记忆": render_tamf_memory,
    "📚 AInvest知识库": render_ainvest_kb, "🤖 L3 投资伙伴": render_l3_status,
    "⚙️ 设置": render_settings,
}

# ── Sidebar Navigation ──────────────────────────────────────────────────────
if "current_page" not in st.session_state:
    # 允许 ?page=🤖%20L3%20投资伙伴 URL 参数直跳 (方便测试/书签)
    try:
        qp = st.query_params
        qpage = qp.get("page", PAGES[0])
        if isinstance(qpage, list):
            qpage = qpage[0] if qpage else PAGES[0]
        st.session_state["current_page"] = qpage if qpage in PAGES else PAGES[0]
    except Exception:
        st.session_state["current_page"] = PAGES[0]

with st.sidebar:
    st.title("📊 InvestPilot")
    st.markdown("**Phase 2** | 个人投资分析系统")
    st.divider()

    # ── Hermes Profile 切换器 (v2.1 补丁8) ──────────────────────────────
    if "active_profile" not in st.session_state:
        st.session_state["active_profile"] = "default"
    if _PROFILE_LOADER_OK and _PROFILE_LIST:
        current_idx = (
            _PROFILE_LIST.index(st.session_state["active_profile"])
            if st.session_state["active_profile"] in _PROFILE_LIST
            else 0
        )
        sel_profile = st.selectbox(
            "🎯 投资风格",
            _PROFILE_LIST,
            index=current_idx,
            key="profile_selectbox",
            help="default: 均衡 | conservative: 防御 | aggressive: 进攻",
        )
        st.session_state["active_profile"] = sel_profile
        # 加载 profile 描述
        try:
            prof_cfg = _PROFILE_LOADER.load(sel_profile)
            prof_meta = prof_cfg.get("profile", {})
            alloc = prof_cfg.get("target_allocation", {})
            st.caption(
                f"📌 {prof_meta.get('description', sel_profile)}\n"
                f"仓位: AI算力 {alloc.get('ai_compute', 0)*100:.0f}% | "
                f"防御 {alloc.get('defense', 0)*100:.0f}% | "
                f"现金 {alloc.get('cash', 0)*100:.0f}%"
            )
        except Exception:
            pass
        st.divider()

    st.markdown("### 🕐 系统状态")
    st.success("🟢 运行中")
    c1, c2 = st.columns(2)
    c1.metric("持仓数", len(load_positions()))
    c2.metric("新闻(7日)", get_news_count())
    st.divider()
    st.markdown("### 📁 导航")
    # 把 selectbox 绑定到 session_state，避免 rerun 后 widget state 回滚
    sel = st.selectbox(
        "📁 导航",
        PAGES,
        index=PAGES.index(st.session_state["current_page"]),
        key="nav_selectbox",
        label_visibility="visible",
    )
    # 同步到 current_page（widget state 即权威源）
    st.session_state["current_page"] = sel
    st.divider()
    from datetime import datetime
    st.caption(f"最后更新: {datetime.now().strftime('%H:%M:%S')}")

# ── Main ───────────────────────────────────────────────────────────────────
page = st.session_state["current_page"]
VIEW_MAP.get(page, lambda: st.error(f"未知页面: {page}"))()