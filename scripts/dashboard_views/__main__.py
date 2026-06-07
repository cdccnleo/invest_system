"""
dashboard.py — Phase 2 Web 仪表盘 (Streamlit)
持仓视图 v0.1
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime

# ── 必须在 import streamlit 之前设置 ───────────────────────────────────────
os.environ[" STREAMLIT_SERVER_HEADLESS"] = "true"

import streamlit as st

# ── 路径设置 ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.utils import safe_float

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


def render_sidebar():
    # URL 参数优先（?page=calendar）
    import streamlit as st_caller
    query_params = st_caller.query_params
    VALID_PAGES = {"📋 持仓仪表板", "📰 新闻摘要", "📋 研报", "📢 公告", "📅 决策日历", "📝 计划审核", "📊 TAMF分析记忆", "📚 AInvest知识库", "🤖 L3 投资伙伴", "📈 策略回测", "📊 多因子评分", "⚙️ 设置"}
    PAGES = ["📋 持仓仪表板", "📰 新闻摘要", "📋 研报", "📢 公告", "📅 决策日历", "📝 计划审核", "📊 TAMF分析记忆", "📚 AInvest知识库", "🤖 L3 投资伙伴", "📈 策略回测", "📊 多因子评分", "⚙️ 设置"]

    if "current_page" not in st.session_state:
        st.session_state["current_page"] = "📋 持仓仪表板"

    # URL 参数优先读取
    url_page = query_params.get("page", None)
    if url_page:
        page_map = {
            "portfolio": "📋 持仓仪表板",
            "news": "📰 新闻摘要",
            "reports": "📋 研报",
            "announcements": "📢 公告",
            "calendar": "📅 决策日历",
            "tamf": "📊 TAMF分析记忆",
            "ainvest_kb": "📚 AInvest知识库",
            "l3": "🤖 L3 投资伙伴",
            "strategies": "📈 策略回测",
            "factors": "📊 多因子评分",
            "settings": "⚙️ 设置",
        }
        mapped = page_map.get(url_page)
        if mapped and mapped in VALID_PAGES:
            st.session_state["current_page"] = mapped
            query_params.clear()

    with st.sidebar:
        st.title("📊 InvestPilot")
        st.markdown("**Phase 2** | 个人投资分析系统")
        st.divider()

        st.markdown("### 🕐 系统状态")
        st.success("🟢 运行中")

        col1, col2 = st.columns(2)
        with col1:
            st.metric("持仓数", len(load_positions()))
        with col2:
            st.metric("新闻(7日)", get_news_count())

        st.divider()
        st.markdown("### 🔄 持仓同步")
        if st.button("📥 从券商文件同步持仓", help="读取 D:\\Hold 目录下的券商持仓文件，更新 positions.csv 和 PostgreSQL"):
            with st.spinner("正在解析持仓文件..."):
                try:
                    import csv as csv_lib
                    import json as json_lib
                    from pathlib import Path
                    def _read_gbk(p):
                        for enc in ['utf-8-sig', 'gbk', 'cp936', 'utf-8']:
                            try:
                                with open(p, encoding=enc) as f:
                                    return f.read()
                            except Exception:
                                continue
                        return ""
                    def _parse_csv_line(line):
                        reader = csv_lib.reader([line])
                        return list(reader)[0]

                    HOLD_DIR = "/mnt/d/Hold"
                    positions_map = {}

                    # 查找目录中最新实际存在的持仓文件日期
                    def _latest_hold_date():
                        import re
                        import os
                        best = None
                        for fname in os.listdir(HOLD_DIR):
                            m = re.search(r'(\d{8})\.csv$', fname)
                            if m:
                                d = m.group(1)
                                if best is None or d > best:
                                    best = d
                        return best  # e.g. "20260525"

                    latest_date = _latest_hold_date()
                    if not latest_date:
                        st.warning("未找到任何持仓文件（D:\\Hold\\*持仓*.csv）")
                        st.rerun()
                        return
                    date_str = f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:8]}"
                    file_date = latest_date  # 用于拼接文件名
                    st.caption(f"📂 检测到最新持仓日期: {date_str}")
                    date = date_str  # positions.csv 的 date 字段

                    _dbg = {"国金证券": 0, "天天基金": 0, "广发证券": 0, "汇添富基金": 0}

                    # 1. 国金证券
                    gj = _read_gbk(f"{HOLD_DIR}/国金证券持仓{file_date}.csv")
                    for line in gj.split('\n'):
                        row = _parse_csv_line(line)
                        if len(row) < 10 or not row[0].strip() or not row[0][0].isdigit():
                            continue
                        try:
                            code = row[0].strip().zfill(6)
                            name = row[1].strip()
                            shares = safe_float(row[3])
                            cost = abs(safe_float(row[7]))
                            mv = safe_float(row[9])
                            if shares <= 0 or mv <= 0:
                                continue
                            ptype = 'bond' if code.startswith('4') else ('fund' if code.startswith(('5','15')) else 'stock')
                            key = ('国金证券', code)
                            if key not in positions_map:
                                positions_map[key] = {'account':'国金证券','code':code,'name':name,'type':ptype,'shares':shares,'cost':cost,'date':date,'market_value':mv,'weight':0}
                                _dbg["国金证券"] += 1
                            else:
                                p = positions_map[key]
                                cs = p['shares'] + shares
                                p['shares'] = cs
                                p['market_value'] += mv
                                p['cost'] = (p['cost'] * p['shares'] + cost * shares) / cs if cs > 0 else p['cost']
                        except Exception:
                            continue

                    # 2. 天天基金
                    try:
                        with open(f"{HOLD_DIR}/天天基金持仓{file_date}.csv", encoding='utf-8-sig') as f:
                            for line in f.read().split('\n')[1:]:
                                row = _parse_csv_line(line)
                                if len(row) < 6 or not row[0].strip() or row[0] in ('产品代码','持仓收益(元)'):
                                    continue
                                try:
                                    code = row[0].strip()
                                    name = row[1].strip()
                                    nav = safe_float(row[3])
                                    amount = safe_float(row[5])
                                    if amount <= 0:
                                        continue
                                    key = ('天天基金', code)
                                    if key not in positions_map:
                                        positions_map[key] = {'account':'天天基金','code':code,'name':name,'type':'fund','shares':amount/nav if nav>0 else 0,'cost':nav,'date':date,'market_value':amount,'weight':0}
                                        _dbg["天天基金"] += 1
                                except Exception:
                                    continue
                    except Exception:
                        pass

                    # 3. 广发基金
                    try:
                        with open(f"{HOLD_DIR}/广发基金持仓{file_date}.csv", encoding='utf-8-sig') as f:
                            for line in f.read().split('\n')[1:]:
                                row = _parse_csv_line(line)
                                if len(row) < 10 or not row[0].strip() or row[0] in ('类型',''):
                                    continue
                                try:
                                    code = str(row[2].strip()).zfill(6)
                                    name = row[1].strip()
                                    shares = safe_float(row[3])
                                    cost = abs(safe_float(row[7]))
                                    mv = safe_float(row[9])
                                    if shares <= 0 or mv <= 0:
                                        continue
                                    ptype = 'stock' if row[0].strip() == '股票' else 'fund'
                                    key = ('广发证券', code)
                                    if key not in positions_map:
                                        positions_map[key] = {'account':'广发证券','code':code,'name':name,'type':ptype,'shares':shares,'cost':cost,'date':date,'market_value':mv,'weight':0}
                                        _dbg["广发证券"] += 1
                                    else:
                                        p = positions_map[key]
                                        cs = p['shares'] + shares
                                        p['shares'] = cs
                                        p['market_value'] += mv
                                        p['cost'] = (p['cost'] * p['shares'] + cost * shares) / cs if cs > 0 else p['cost']
                                except Exception:
                                    continue
                    except Exception:
                        pass

                    # 4. 汇添富基金
                    try:
                        fund_groups = {}
                        with open(f"{HOLD_DIR}/汇添富基金持仓{file_date}.csv", encoding='utf-8-sig') as f:
                            for line in f.read().split('\n')[1:]:
                                row = _parse_csv_line(line)
                                if len(row) < 10 or not row[0].strip() or row[0] in ('基金代码','人民币资产'):
                                    continue
                                try:
                                    code = str(row[0].strip()).zfill(6)
                                    name = row[1].strip()
                                    nav = safe_float(row[3])
                                    shares = safe_float(row[6])
                                    mv = safe_float(row[8])
                                    ct = safe_float(row[9])
                                    if shares <= 0:
                                        continue
                                    if code not in fund_groups:
                                        fund_groups[code] = {'name': name, 'nav': nav, 'ts': 0, 'tmv': 0, 'tct': 0}
                                    fund_groups[code]['ts'] += shares
                                    fund_groups[code]['tmv'] += mv
                                    fund_groups[code]['tct'] += ct
                                except Exception:
                                    continue
                        for code, d in fund_groups.items():
                            if d['ts'] <= 0:
                                continue
                            avg_cost = d['tct']/d['ts'] if d['tct'] > 0 else d['nav']
                            key = ('汇添富基金', code)
                            if key not in positions_map:
                                positions_map[key] = {'account':'汇添富基金','code':code,'name':d['name'],'type':'fund','shares':d['ts'],'cost':avg_cost,'date':date,'market_value':d['tmv'],'weight':0}
                                _dbg["汇添富基金"] += 1
                    except Exception:
                        pass

                    positions = list(positions_map.values())
                    total_mv = sum(p['market_value'] for p in positions)
                    for p in positions:
                        p['weight'] = round(p['market_value']/total_mv*100, 2) if total_mv > 0 else 0

                    # DEBUG: 打印各数据源记录数
                    st.caption(f"📊 各数据源: {_dbg}，合计 {len(positions)} 条 | 日期={date_str}")
                    if len(positions) < 30:
                        st.warning("⚠️ 记录数偏少（<30），检查各文件读取是否完整")

                    # 写入 positions.csv
                    csv_path = "/mnt/d/Hold/invest-data/positions.csv"
                    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                        writer = csv_lib.DictWriter(f, fieldnames=['account','code','name','type','shares','cost','date','market_value','weight'])
                        writer.writeheader()
                        writer.writerows(positions)

                    # UPSERT 到 holdings.encrypted_positions（追加表，is_current 标记当前）
                    import psycopg2
                    from credentials import get_credential
                    enc_key_path = Path.home()/".hermes"/"invest_credentials"/"store.json"
                    store = json_lib.loads(enc_key_path.read_text())
                    enc_key = store["DB_ENCRYPTION_KEY"]
                    conn = psycopg2.connect(host='localhost', database='investpilot', user='invest_admin', password=get_credential("DB_PASSWORD"))
                    cur = conn.cursor()
                    added = 0
                    for p in positions:
                        code = p['code']
                        name = p['name']
                        shares = float(p['shares'])
                        avg_cost = float(p['cost'])
                        mv = float(p['market_value'])
                        wt = float(p['weight'])
                        ptype = p['type']
                        profit_loss = mv - avg_cost * shares
                        profit_pct = min(max((mv/avg_cost - 1)*100, -9999.9999), 9999.9999) if avg_cost > 0 else 0
                        # 使用 upsert_positions 函数：自动标记旧记录 is_current=FALSE + 插入新记录
                        cur.execute("SELECT holdings.upsert_positions(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                    (code, name, ptype, shares, avg_cost, profit_loss, mv, avg_cost, wt, profit_pct, enc_key, enc_key, enc_key, enc_key))
                        added += 1
                    conn.commit()
                    conn.close()

                    st.session_state["sync_result"] = f"✅ 同步完成：{len(positions)} 条，总市值 ¥{total_mv:,.0f}，已写入 DB"
                    # 清除缓存
                    st.cache_data.clear()
                except Exception as e:
                    st.session_state["sync_result"] = f"❌ 同步失败：{e}"
            st.rerun()

        if "sync_result" in st.session_state:
            st.info(st.session_state["sync_result"])

        st.divider()
        st.markdown("### 📁 导航")

        current_idx = PAGES.index(st.session_state["current_page"])
        selected = st.selectbox(
            "📁 导航",
            PAGES,
            index=current_idx,
            label_visibility="visible",
        )
        st.divider()
        st.caption(f"最后更新: {datetime.now().strftime('%H:%M:%S')}")

        if selected != st.session_state["current_page"]:
            st.session_state["current_page"] = selected
            st.rerun()

        return st.session_state["current_page"]


# ── 视图 1：持仓仪表板 ──────────────────────────────────────────────────────

# ── 视图路由（从各子模块导入）─────────────────────────────────────────────────
from dashboard_views._portfolio import render_portfolio_dashboard
from dashboard_views._news      import render_news_summary, render_reports, render_announcements
from dashboard_views._tamf     import render_tamf_memory, render_history
from dashboard_views._l3_status import render_l3_status
from dashboard_views._settings import render_plan_review, render_settings
from dashboard_views._strategies import render_strategy_comparison
from dashboard_views._factors import render_factor_analysis
from dashboard_views._ainvest_kb import render_ainvest_kb

# ── 共享数据函数（来自 dashboard.py）────────────────────────────────────────
from pathlib import Path as _Path
# dashboard.py 在上一级目录
sys.path.insert(0, str(_Path(__file__).parent.parent))
from dashboard import load_positions, get_news_count

# ── 主程序 ──────────────────────────────────────────────────────────────────

def main():
    page = render_sidebar()

    if page == "📋 持仓仪表板":
        render_portfolio_dashboard()
    elif page == "📰 新闻摘要":
        render_news_summary()
    elif page == "📋 研报":
        render_reports()
    elif page == "📢 公告":
        render_announcements()
    elif page == "📅 决策日历":
        render_history()
    elif page == "📝 计划审核":
        render_plan_review()
    elif page == "📊 TAMF分析记忆":
        render_tamf_memory()
    elif page == "📚 AInvest知识库":
        render_ainvest_kb()
    elif page == "🤖 L3 投资伙伴":
        render_l3_status()
    elif page == "📈 策略回测":
        render_strategy_comparison()
    elif page == "📊 多因子评分":
        render_factor_analysis()
    elif page == "⚙️ 设置":
        render_settings()


if __name__ == "__main__":
    main()
