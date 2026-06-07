"""
dashboard.py — Phase 2 Web 仪表盘 (Streamlit)
持仓视图 v0.1
"""

import os
import sys
import json
import csv
from pathlib import Path
from datetime import datetime

# ── 必须在 import streamlit 之前设置 ───────────────────────────────────────
os.environ[" STREAMLIT_SERVER_HEADLESS"] = "true"

import streamlit as st
import pandas as pd
import plotly.express as px

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

# ── PWA 初始化：生成 service worker + 注入 manifest/link ──────────────────────
def _init_pwa():
    """生成 sw.js 并注入 PWA head 元素（manifest + viewport 覆盖 + SW 注册）。"""
    try:
        from pwa_service import generate_sw
        generate_sw()
    except Exception:
        pass  # non-fatal — PWA is optional enhancement

    # Inject manifest <link> and service-worker registration script.
    # st.html renders directly into the Streamlit page body;
    # we use a hidden marker so these elements end up in <head> context.
    st.html(
        f"""
        <link rel="manifest" href="/static/manifest.json" />
        <meta name="theme-color" content="#0ea5e9" />
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
        <script>
        if ('serviceWorker' in navigator) {{
          navigator.serviceWorker.register('/static/sw.js')
            .then(reg => console.log('[PWA] SW registered:', reg.scope))
            .catch(err => console.warn('[PWA] SW registration failed:', err));
        }}
        </script>
        """
    )

_init_pwa()
del _init_pwa

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

@st.cache_data(ttl=300)
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
    except Exception:
        return 0
    finally:
        conn.close()


# ── 侧边栏 ─────────────────────────────────────────────────────────────────

def render_sidebar():
    # URL 参数优先（?page=calendar）
    import streamlit as st_caller
    query_params = st_caller.query_params
    VALID_PAGES = {"📋 持仓仪表板", "📰 新闻摘要", "📋 研报", "📢 公告", "📅 决策日历", "📝 计划审核", "📊 TAMF分析记忆", "⚙️ 设置"}
    PAGES = ["📋 持仓仪表板", "📰 新闻摘要", "📋 研报", "📢 公告", "📅 决策日历", "📝 计划审核", "📊 TAMF分析记忆", "⚙️ 设置"]

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
                    def _safe_float(v, default=0.0):
                        try:
                            return float(str(v).replace(',', '').replace('"', '').strip() or '0')
                        except Exception:
                            return default
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
                            shares = _safe_float(row[3])
                            cost = abs(_safe_float(row[7]))
                            mv = _safe_float(row[9])
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
                                    nav = _safe_float(row[3])
                                    amount = _safe_float(row[5])
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
                                    shares = _safe_float(row[3])
                                    cost = abs(_safe_float(row[7]))
                                    mv = _safe_float(row[9])
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
                                    nav = _safe_float(row[3])
                                    shares = _safe_float(row[6])
                                    mv = _safe_float(row[8])
                                    ct = _safe_float(row[9])
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

def render_portfolio_dashboard():
    positions = load_positions()
    if not positions:
        st.error("无持仓数据，请检查 positions.csv")
        return

    df = pd.DataFrame(positions)
    total_mv = df["市值"].sum()

    # 顶部 KPI 卡片
    st.markdown("## 📋 持仓仪表板")
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)

    # 估算总盈亏（需要成本）
    total_cost = (df["份额"] * df["成本"]).sum()
    total_pnl = total_mv - total_cost
    pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    with kpi1:
        st.metric(
            "💰 总市值",
            f"¥{total_mv:,.0f}",
            delta=f"{pnl_pct:+.1f}%" if pnl_pct != 0 else None,
        )
    with kpi2:
        st.metric("📈 总盈亏", f"{pnl_pnl_str(total_pnl)}")
    with kpi3:
        fund_count = len(df[df["类型"] == "fund"])
        st.metric("📊 基金数", fund_count)
    with kpi4:
        stock_count = len(df[df["类型"] == "stock"])
        st.metric("🏦 股票数", stock_count)

    st.divider()

    # 初始化持仓调整 session_state
    if "holdings_adjustments" not in st.session_state:
        st.session_state["holdings_adjustments"] = {}

    # 主内容区
    col_left, col_right = st.columns([2, 1])

    with col_left:
        # 持仓调整滑块交互区
        st.markdown("### 📐 持仓模拟调整")

        # 调整模式开关
        adjust_mode = st.checkbox("🔧 开启持仓模拟调整", value=False, key="adjust_mode_toggle")

        if adjust_mode:
            st.caption("💡 拖动滑块可模拟50%~150%仓位变化，仅用于交互预览，不实际修改持仓")

            # 按代码建索引便于查找
            df["代码"] = df["代码"].astype(str)
            adj_state = st.session_state["holdings_adjustments"]

            for idx, row in df.iterrows():
                code = row["代码"]
                name = row["名称"]
                current_shares = row["份额"]
                current_mv = row["市值"]
                cost = row["成本"]
                current_pct = row["仓位%"]

                # 默认调整比例 = 100%（不变）
                default_adj = adj_state.get(code, 100)
                adj_pct = st.slider(
                    f"**{name}** (`{code}`)",
                    min_value=50,
                    max_value=150,
                    value=default_adj,
                    step=10,
                    key=f"adj_{code}",
                    help=f"当前市值: ¥{current_mv:,.2f} | 仓位: {current_pct:.2f}%"
                )

                # 计算模拟市值
                simulated_shares = current_shares * adj_pct / 100
                simulated_mv = simulated_shares * cost  # 按成本价估算
                mv_delta = simulated_mv - current_mv
                delta_pct = ((adj_pct / 100 - 1) * 100)

                # 颜色标注模拟结果
                if adj_pct > 100:
                    delta_color = "📈"
                    delta_str = f"+¥{mv_delta:,.2f} (+{delta_pct:.0f}%)"
                elif adj_pct < 100:
                    delta_color = "📉"
                    delta_str = f"-¥{abs(mv_delta):,.2f} ({delta_pct:.0f}%)"
                else:
                    delta_color = "➖"
                    delta_str = "±¥0 (0%)"

                st.markdown(
                    f"　　模拟市值: **{delta_color} ¥{simulated_mv:,.2f}** "
                    f"　变化: {delta_str}"
                )

                # 保存到 session_state
                adj_state[code] = adj_pct

            # 汇总模拟调整结果
            total_simulated = sum(
                df.loc[df["代码"] == code, "份额"].values[0] * adj_state.get(code, 100) / 100
                * df.loc[df["代码"] == code, "成本"].values[0]
                for code in adj_state
                if code in df["代码"].values
            )
            total_original = df["市值"].sum()
            total_change = total_simulated - total_original
            st.divider()
            col_sum1, col_sum2, col_sum3 = st.columns(3)
            with col_sum1:
                st.metric("原始总市值", f"¥{total_original:,.2f}")
            with col_sum2:
                st.metric("模拟总市值", f"¥{total_simulated:,.2f}",
                          delta=f"{total_change:+,.2f}" if total_change != 0 else None)
            with col_sum3:
                change_pct = (total_change / total_original * 100) if total_original > 0 else 0
                st.metric("模拟变化率", f"{change_pct:+.1f}%")

            # 提交模拟记录
            if st.button("📝 提交模拟记录到审核日志", key="submit_simulation"):
                from storage_factory import StorageFactory
                storage = StorageFactory()
                storage.write_audit(
                    event_type="SIMULATION_SUBMITTED",
                    operator="manual",
                    detail={
                        "adjustments": adj_state,
                        "total_original": total_original,
                        "total_simulated": total_simulated,
                    },
                    result="SUCCESS"
                )
                st.success("模拟记录已写入审核日志")

            st.divider()

        # 持仓明细表
        st.markdown("### 持仓明细")

        # 计算盈亏列（市值 - 份额 × 成本）
        df["盈亏"] = (df["市值"] - df["份额"] * df["成本"]).round(2)
        df["盈亏%"] = (((df["市值"] / (df["份额"] * df["成本"])) - 1) * 100).round(2).replace([float("inf"), float("-inf")], 0).fillna(0)

        # 类型映射
        type_icon = {"fund": "📊", "stock": "🏦", "etf": "📈"}
        df["类型图标"] = df["类型"].map(type_icon).fillna("📋")

        display_df = df[["代码", "名称", "类型图标", "成本", "市值", "仓位%", "盈亏"]].copy()
        display_df["成本"] = display_df["成本"].map(lambda x: f"¥{x:.4f}" if x < 100 else f"¥{x:.2f}")
        display_df["市值"] = display_df["市值"].map(lambda x: f"¥{x:,.2f}")
        display_df["仓位%"] = display_df["仓位%"].map(lambda x: f"{x:.2f}%")
        display_df["盈亏"] = display_df["盈亏"].map(lambda x: f"{'+¥' if x >= 0 else '-¥'}{abs(x):,.2f}")

        st.dataframe(
            display_df,
            width="stretch",
            hide_index=True,
        )

    with col_right:
        # 仓位饼图
        st.markdown("### 仓位分布")
        if not df.empty:
            pie = px.pie(
                df,
                values="仓位%",
                names="名称",
                hole=0.4,
                title="仓位占比",
            )
            pie.update_layout(height=300, margin=dict(t=30, b=0, l=0, r=0))
            st.plotly_chart(pie, width="stretch")

        # 行业分布（简化版）
        st.markdown("### 行业分布（估算）")
        sector_map = {
            "00": "主板/中小", "30": "创业板", "15": "ETF",
            "51": "ETF/主板", "58": "ETF/主板", "56": "ETF",
            "59": "ETF/科创", "68": "科创板", "002": "中小板",
            "300": "创业板", "600": "主板", "601": "主板",
        }
        df["行业"] = df["代码"].str[:2].map(sector_map).fillna("其他")
        sector_df = df.groupby("行业")["仓位%"].sum().reset_index()
        sector_df = sector_df.sort_values("仓位%", ascending=False)
        bar = px.bar(sector_df, x="行业", y="仓位%", title="行业暴露", color="仓位%")
        bar.update_layout(height=250, margin=dict(t=30, b=0, l=0, r=0))
        st.plotly_chart(bar, width="stretch")

    # 盈亏排行
    st.divider()
    st.markdown("### 🏆 盈亏排行榜")

    if "盈亏" in df.columns and "盈亏%" in df.columns:
        top_df = df.sort_values("盈亏", ascending=False).head(10)[
            ["名称", "代码", "盈亏", "盈亏%", "仓位%"]
        ]
        top_df["盈亏%"] = top_df["盈亏%"].map(lambda x: f"{x:+.1f}%")
        top_df["仓位%"] = top_df["仓位%"].map(lambda x: f"{x:.1f}%")
        top_df["盈亏"] = top_df["盈亏"].map(
            lambda x: f"{'+¥' if x >= 0 else '-¥'}{abs(x):,.0f}"
        )
        st.dataframe(top_df, width="stretch", hide_index=True)


def pnl_pnl_str(pnl: float) -> str:
    return f"{'+¥' if pnl >= 0 else '-¥'}{abs(pnl):,.0f}"


# ── 视图 2：新闻摘要 ────────────────────────────────────────────────────────

def render_news_summary():
    st.markdown("## 📰 新闻摘要（近7日）")

    # ── 手动同步按钮 ──
    col_info, col_btn = st.columns([2, 1])
    with col_info:
        last_sync = st.session_state.get("news_last_sync")
        if last_sync:
            st.caption(f"最后同步: {last_sync.strftime('%m-%d %H:%M:%S')}")
        else:
            st.caption("尚未手动同步过")
    with col_btn:
        if st.button("🔄 同步新闻", key="sync_news_btn",
                     disabled=st.session_state.get("syncing_news", False)):
            st.session_state["syncing_news"] = True
            with st.spinner("正在采集新闻数据..."):
                try:
                    from fetch_news import collect_and_save_news
                    result = collect_and_save_news()
                    st.session_state["syncing_news"] = False
                    st.session_state["news_last_sync"] = datetime.now()
                    if result["status"] == "ok":
                        st.success(f"同步完成：采集 {result['total']} 条，新增 {result['saved']} 条")
                        st.rerun()
                    elif result["status"] == "empty":
                        st.info("数据已是最新，无新增内容")
                    else:
                        st.error(f"同步失败: {result.get('error', '未知错误')}")
                except Exception as e:
                    st.session_state["syncing_news"] = False
                    st.error(f"同步异常: {e}")
    st.divider()

    conn = get_db_connection()
    if conn is None:
        st.warning("无法连接数据库")
        return

    cur = conn.cursor()
    try:
        # ── Tab 1: 全部新闻 ────────────────────────────────────────────────
        cur.execute("""
            SELECT title, content, source, published_at, severity
            FROM research.news_articles
            WHERE published_at >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY published_at DESC
            LIMIT 80
        """)
        all_rows = cur.fetchall()

        # ── Tab 2: 国际投行研究 ────────────────────────────────────────────
        cur.execute("""
            SELECT title, content, source, published_at,
                   cited_institutions, article_type, is_bank_related
            FROM research.international_bank_research
            WHERE published_at >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY is_bank_related DESC, published_at DESC
            LIMIT 40
        """)
        intl_rows = cur.fetchall()

        if not all_rows and not intl_rows:
            st.info("暂无新闻数据")
            return

        # Tab 布局
        tab1, tab2 = st.tabs(["📋 全部新闻", "🏦 国际投行研究"])

        # ── Tab 1: 全部新闻 ───────────────────────────────────────────────
        with tab1:
            st.markdown(f"**共 {len(all_rows)} 条**")
            for title, content, source, published_at, severity in all_rows:
                date_str = published_at.strftime("%m-%d %H:%M") if published_at else "未知"
                sev_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "⚪"}.get(severity, "⚪")
                with st.expander(f"{sev_emoji} [{date_str}] {source} - {title}"):
                    st.markdown(content[:400] if content else "无正文")

        # ── Tab 2: 国际投行研究 ───────────────────────────────────────────
        with tab2:
            if not intl_rows:
                st.info("暂无国际投行研究数据（定时任务 21:00 采集）")
            else:
                # 统计
                [r for r in intl_rows if r[6]]
                cur.execute("""
                    SELECT COUNT(*) FROM research.international_bank_research
                    WHERE published_at >= CURRENT_DATE - INTERVAL '7 days'
                      AND is_bank_related = TRUE
                """)
                bank_cnt = cur.fetchone()[0] or 0

                col1, col2, col3 = st.columns(3)
                col1.metric("总条数", f"{len(intl_rows)} 条")
                col2.metric("投行相关", f"{bank_cnt} 条")
                col3.metric("来源数", f"{len(set(r[2] for r in intl_rows))} 个")

                st.divider()

                # 筛选：只看投行相关
                show_bank_only = st.checkbox("🔴 仅显示投行相关", value=False, key="intl_bank_only")
                display_rows = [r for r in intl_rows if r[6]] if show_bank_only else intl_rows

                for row in display_rows:
                    title, content, source, published_at, cited_inst, art_type, is_bank = row
                    date_str = published_at.strftime("%m-%d %H:%M") if published_at else "未知"
                    inst_str = ', '.join(cited_inst) if cited_inst else ''
                    bank_tag = "🔴" if is_bank else "⚪"

                    header = (f"{bank_tag} [{date_str}] {source}"
                              + (f" | 涉及: {inst_str}" if inst_str else "")
                              + f" | {title}")
                    with st.expander(header):
                        st.markdown(f"**类型**: {art_type or '一般'}")
                        if inst_str:
                            st.markdown(f"**涉及机构**: {inst_str}")
                        st.markdown(content[:500] if content else "无摘要")

    except Exception as e:
        st.error(f"加载新闻失败: {e}")
    finally:
        conn.close()


# ── 视图 2.5：研报 ─────────────────────────────────────────────────────────

def render_reports():
    """研报展示页面 — 近30天研报，支持按股票/评级/来源筛选"""
    st.markdown("## 📋 研报（近30天）")

    # ── 手动同步按钮 ──
    col_info, col_btn = st.columns([2, 1])
    with col_info:
        last_sync = st.session_state.get("reports_last_sync")
        if last_sync:
            st.caption(f"最后同步: {last_sync.strftime('%m-%d %H:%M:%S')}")
        else:
            st.caption("尚未手动同步过")
    with col_btn:
        if st.button("🔄 同步研报", key="sync_reports_btn",
                     disabled=st.session_state.get("syncing_reports", False)):
            st.session_state["syncing_reports"] = True
            with st.spinner("正在采集研报数据..."):
                try:
                    from fetch_reports import collect_reports
                    reports = collect_reports(days_back=7, save_to_db=True)
                    st.session_state["syncing_reports"] = False
                    st.session_state["reports_last_sync"] = datetime.now()
                    if reports:
                        st.success(f"同步完成：采集 {len(reports)} 条研报")
                        st.rerun()
                    else:
                        st.info("数据已是最新，无新增内容")
                except Exception as e:
                    st.session_state["syncing_reports"] = False
                    st.error(f"同步异常: {e}")
    st.divider()

    conn = get_db_connection()
    if conn is None:
        st.warning("无法连接数据库")
        return

    cur = conn.cursor()
    try:
        # 汇总统计
        cur.execute("""
            SELECT COUNT(*),
                   COUNT(DISTINCT source),
                   COUNT(DISTINCT ts_code),
                   COUNT(DISTINCT report_date)
            FROM research.research_reports
            WHERE report_date >= CURRENT_DATE - INTERVAL '30 days'
        """)
        total, org_count, stock_count, day_count = cur.fetchone()

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("研报总数", f"{total} 份")
        col2.metric("覆盖机构", f"{org_count} 家")
        col3.metric("涉及股票", f"{stock_count} 只")
        col4.metric("覆盖天数", f"{day_count} 天")

        st.divider()

        # 筛选器
        col_a, col_b = st.columns(2)
        with col_a:
            rating_filter = st.selectbox(
                "按评级筛选",
                ["全部", "买入", "增持", "中性", "减持", "卖出", "无评级"],
                index=0,
            )
        with col_b:
            # 加载前10热门来源
            cur.execute("""
                SELECT source, COUNT(*) as cnt
                FROM research.research_reports
                WHERE report_date >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY source
                ORDER BY cnt DESC LIMIT 10
            """)
            source_options = ["全部"] + [r[0] for r in cur.fetchall()]
            source_filter = st.selectbox("按来源筛选", source_options, index=0)

        # 查询研报列表
        query = """
            SELECT report_date, source, ts_code, rating, title, summary, url
            FROM research.research_reports
            WHERE report_date >= CURRENT_DATE - INTERVAL '30 days'
        """
        params = []
        if rating_filter != "全部":
            query += " AND rating = %s"
            params.append(rating_filter)
        if source_filter != "全部":
            query += " AND source = %s"
            params.append(source_filter)
        query += " ORDER BY report_date DESC, id DESC LIMIT 100"

        cur.execute(query, params)
        rows = cur.fetchall()

        st.markdown(f"**{len(rows)} 份研报**")

        # 评级颜色映射
        rating_colors = {
            "买入": "🟢", "增持": "🟢", "强烈推荐": "🟢",
            "中性": "🟡", "谨慎": "🟡",
            "减持": "🔴", "卖出": "🔴",
        }

        for report_date, source, ts_code, rating, title, summary, url in rows:
            date_str = report_date.strftime("%Y-%m-%d") if report_date else "未知"
            r_emoji = rating_colors.get(str(rating).strip(), "⚪")

            # 重建东方财富研报原文 URL（基于 info_code）
            info_code_val = None
            # 从 title+date 查找 info_code（通过额外查询）
            cur.execute("""
                SELECT info_code FROM research.research_reports
                WHERE title = %s AND report_date = %s
            """, (title, report_date))
            row_info = cur.fetchone()
            info_code_val = row_info[0] if row_info else None

            if info_code_val:
                em_url = f"https://data.eastmoney.com/report/zw_industry.jshtml?infocode={info_code_val}"
            else:
                em_url = url if url else None

            label = f"{r_emoji} [{date_str}] {source or '未知来源'} | {str(rating or '无评级'):8s} | {ts_code or ''} {title}"

            with st.expander(label[:120]):
                st.markdown(f"**{title}**")
                col_info1, col_info2 = st.columns(2)
                with col_info1:
                    st.markdown(f"📅 **{date_str}**")
                    st.markdown(f"🏢 **{source or '未知来源'}**")
                    st.markdown(f"📊 **{ts_code or 'N/A'}**")
                with col_info2:
                    st.markdown(f"⭐ **{rating or '无评级'}**")
                    if em_url:
                        st.markdown(f"[🔗 查看原文]({em_url})")
                if summary:
                    with st.expander("📝 摘要", expanded=True):
                        st.markdown(summary[:500] if summary else "无摘要")

    except Exception as e:
        st.error(f"加载研报失败: {e}")
    finally:
        conn.close()


# ── 视图 2.6：公告 ─────────────────────────────────────────────────────────

def render_announcements():
    """公告展示页面 — 近30天持仓股公告，支持按类型筛选"""
    st.markdown("## 📢 持仓股公告（近30天）")

    # ── 手动同步按钮 ──
    col_info, col_btn = st.columns([2, 1])
    with col_info:
        last_sync = st.session_state.get("ann_last_sync")
        if last_sync:
            st.caption(f"最后同步: {last_sync.strftime('%m-%d %H:%M:%S')}")
        else:
            st.caption("尚未手动同步过")
    with col_btn:
        if st.button("🔄 同步公告", key="sync_ann_btn",
                     disabled=st.session_state.get("syncing_ann", False)):
            st.session_state["syncing_ann"] = True
            with st.spinner("正在采集公告数据..."):
                try:
                    from fetch_announcements import fetch_all_positions_announcements
                    from storage_factory import get_storage
                    anns = fetch_all_positions_announcements(days_window=1, max_pages=2)
                    st.session_state["syncing_ann"] = False
                    st.session_state["ann_last_sync"] = datetime.now()
                    if anns:
                        storage = get_storage()
                        saved = storage.write_announcements(anns)
                        storage.close()
                        st.success(f"同步完成：采集 {len(anns)} 条，新增 {saved} 条")
                        st.rerun()
                    else:
                        st.info("数据已是最新，无新增内容")
                except Exception as e:
                    st.session_state["syncing_ann"] = False
                    st.error(f"同步异常: {e}")
    st.divider()

    conn = get_db_connection()
    if conn is None:
        st.warning("数据库未连接")
        return

    try:
        # 统计指标
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*), COUNT(DISTINCT ts_code), COUNT(DISTINCT ann_type)
            FROM research.announcements
            WHERE notice_date >= CURRENT_DATE - INTERVAL '30 days'
        """)
        row = cur.fetchone()
        total, stocks, types = row[0] or 0, row[1] or 0, row[2] or 0

        col1, col2, col3 = st.columns(3)
        col1.metric("公告总数", f"{total} 条")
        col2.metric("涉及股票", f"{stocks} 只")
        col3.metric("类型分布", f"{types} 种")

        st.divider()

        # 筛选
        cur.execute("SELECT DISTINCT ann_type FROM research.announcements ORDER BY ann_type")
        all_types = [r[0] for r in cur.fetchall()]
        selected_types = st.multiselect(
            "筛选公告类型", all_types, default=all_types[:6] if len(all_types) > 6 else all_types,
            format_func=lambda x: f"{x}",
            key="ann_type_filter"
        )

        cur.execute("""
            SELECT notice_date, ts_code, title, ann_type, url, ann_id
            FROM research.announcements
            WHERE notice_date >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY notice_date DESC, ts_code
        """)
        rows = cur.fetchall()
        cur.close()

        # 应用类型筛选
        if selected_types:
            rows = [r for r in rows if r[3] in selected_types]

        st.markdown(f"**共 {len(rows)} 条公告**")

        # 类型颜色映射
        TYPE_COLORS = {
            "年度报告": "🔵", "半年度报告": "🟦", "一季报": "🔷", "三季报": "🔶",
            "季报": "🟣", "业绩预告": "🟡", "董事会决议": "🟠", "股东大会": "🟧",
            "分红公告": "💰", "回购公告": "🔄", "增持公告": "📈", "减持公告": "📉",
            "股权激励": "🎯", "审计报告": "📋", "法律意见书": "⚖️",
            "核查意见": "✅", "监管措施": "🔴", "债券发行": "🏦",
            "重要事项": "⭐", "资质认定": "🏅", "投资者关系": "🤝",
            "退市风险": "⚠️", "一般公告": "📌",
        }
        def TYPE_EMOJI(t):
            return TYPE_COLORS.get(t, "📌")

        # 按日期分组展示
        current_date = None
        for notice_date, ts_code, title, ann_type, url, ann_id in rows:
            date_str = str(notice_date)

            if date_str != current_date:
                st.markdown(f"**📅 {date_str}**")
                current_date = date_str

            emoji = TYPE_EMOJI(ann_type)
            st.markdown(
                f"{emoji} `[{ts_code}]` **{title}**  "
                f"<span style='color:gray;font-size:0.85em'>[{ann_type}]</span> "
                f"<a href='{url}' target='_blank'>🔗 查看原文</a>",
                unsafe_allow_html=True,
            )

    except Exception as e:
        st.error(f"加载公告失败: {e}")
    finally:
        conn.close()


# ── 视图 3：历史决策日历 ────────────────────────────────────────────────────

def _get_calendar_data(days: int = 60) -> list[dict]:
    """从 audit_log 拉取日历所需的数据"""
    conn = get_db_connection()
    if conn is None:
        return []

    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                DATE(event_time)                       AS day,
                event_type,
                operator,
                result,
                detail,
                trace_id
            FROM audit.audit_log
            WHERE event_time >= CURRENT_DATE - INTERVAL '%s days'
              AND event_type NOT IN ('SKILL_EXECUTED', 'SKILL_SPOT_CHECK',
                                     'SKILL_VALIDATED', 'SKILL_APPROVED', 'SKILL_REJECTED')
            ORDER BY event_time DESC
        """, (days,))
        rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "day": r[0],
            "event_type": r[1],
            "operator": r[2],
            "result": r[3],
            "detail": r[4],
            "trace_id": r[5],
        }
        for r in rows
    ]


def _enrich_calendar_entry(entry: dict) -> dict:
    """为单条日历条目补充展示用字段"""
    import json as _json

    detail = entry.get("detail", {})
    if isinstance(detail, str):
        try:
            detail = _json.loads(detail)
        except Exception:
            detail = {}
    entry["_detail"] = detail

    # 运行阶段图标
    phase_icons = {
        "morning": "🌅",
        "closing": "🌆",
        "evening": "🌙",
        "MVP_ANALYSIS_RUN": "🚀",
        "ANALYSIS_COMPLETE": "📋",
        "DAILY_REFLECTION": "🔍",
    }
    entry["_phase_emoji"] = next(
        (v for k, v in phase_icons.items() if k.lower() in entry["event_type"].lower()),
        "📌"
    )

    # 运行阶段中文名
    phase_names = {
        "morning": "盘前",
        "closing": "盘后",
        "evening": "晚间",
        "MVP_ANALYSIS_RUN": "分析运行",
        "ANALYSIS_COMPLETE": "计划生成",
        "DAILY_REFLECTION": "每日复盘",
    }
    entry["_phase_name"] = next(
        (v for k, v in phase_names.items() if k.lower() in entry["event_type"].lower()),
        entry["event_type"]
    )

    # 结果颜色
    entry["_ok"] = entry["result"] == "SUCCESS"

    # 操作计划数（从 detail 中提取）
    plans = detail.get("plans_count") or detail.get("plans") or 0
    if isinstance(plans, list):
        plans = len(plans)
    entry["_plans_count"] = plans

    # 置信度
    entry["_confidence"] = detail.get("confidence", "N/A")

    # 修改比例（来自复盘）
    mod_ratio = detail.get("attribution", {}).get("modification_ratio", None)
    entry["_mod_ratio"] = f"{mod_ratio:.0f}%" if mod_ratio is not None else None

    # 洞察摘要
    insights = detail.get("attribution", {}).get("insights", [])
    entry["_insight"] = (insights[0][:60] + "…") if insights else None

    return entry


def _build_month_grid(year: int, month: int, entries_by_date: dict) -> pd.DataFrame:
    """构建单个月份的日历网格 DataFrame"""
    import calendar

    cal = calendar.Calendar(firstweekday=6)  # 周日开局
    weeks = cal.monthdatescalendar(year, month)

    rows = []
    for week in weeks:
        week_cells = []
        for day in week:
            if day.month != month:
                week_cells.append({"date": "", "day": "", "emoji": "", "events": "", "status": ""})
            else:
                date_str = day.strftime("%Y-%m-%d")
                evts = entries_by_date.get(date_str, [])
                if not evts:
                    status = "empty"
                    emoji = "　"  # 空日期用空格
                    events = ""
                else:
                    # 取最重要的一条记录决定颜色
                    top = evts[0]
                    if top["result"] == "SUCCESS":
                        status = "success"
                    else:
                        status = "failed"

                    # 拼装当日事件摘要
                    summaries = []
                    for e in evts[:3]:
                        phase = e.get("_phase_name", e["event_type"])
                        plans = e.get("_plans_count", 0)
                        conf = e.get("_confidence", "N/A")
                        summaries.append(f"{phase}({plans}计划, {conf})")

                    events = "\n".join(summaries)
                    emoji = top.get("_phase_emoji", "📌")

                week_cells.append({
                    "date": date_str,
                    "day": str(day.day),
                    "emoji": emoji,
                    "events": events,
                    "status": status,
                })
        rows.append(week_cells)

    return rows


def render_history():
    import calendar

    st.markdown("## 📅 历史决策日历")

    # ── 顶部筛选器 ──────────────────────────────────────────────────────────
    col_filter1, col_filter2, col_filter3 = st.columns([1, 1, 2])
    with col_filter1:
        view_month = st.selectbox(
            "查看月份",
            options=list(range(1, 13)),
            index=list(range(1, 13)).index(datetime.now().month) if datetime.now().month <= 12 else 0,
            format_func=lambda m: f"{datetime.now().year}-{m:02d}",
        )
    with col_filter2:
        view_year = st.selectbox("年份", list(range(datetime.now().year - 2, datetime.now().year + 1))[::-1])
    with col_filter3:
        days_range = st.selectbox("时间范围", [30, 60, 90, 180], index=1,
                                  format_func=lambda d: f"近{d}天")

    # ── 拉取数据 ───────────────────────────────────────────────────────────
    raw_entries = _get_calendar_data(days=days_range)
    for e in raw_entries:
        _enrich_calendar_entry(e)

    # 按日期分组
    entries_by_date = {}
    for e in raw_entries:
        day_str = e["day"].strftime("%Y-%m-%d") if e["day"] else ""
        if day_str:
            entries_by_date.setdefault(day_str, []).append(e)

    # ── 月度日历网格 ─────────────────────────────────────────────────────
    st.markdown(f"#### 📆 {view_year} 年 {view_month:02d} 月")
    weeks_data = _build_month_grid(view_year, view_month, entries_by_date)

    # 星期标题
    weekday_labels = ["日", "一", "二", "三", "四", "五", "六"]
    header_cols = st.columns([1, 1, 1, 1, 1, 1, 1])
    for i, label in enumerate(weekday_labels):
        with header_cols[i]:
            color = "#e8f4fd" if i in (0, 6) else "#f8f9fa"
            st.markdown(
                f"<div style='background:{color}; padding:6px 0; text-align:center; "
                f"border-radius:4px; font-weight:bold; font-size:13px'>{label}</div>",
                unsafe_allow_html=True,
            )

    # 日历格子
    for week_cells in weeks_data:
        cols = st.columns([1, 1, 1, 1, 1, 1, 1])
        for i, cell in enumerate(week_cells):
            with cols[i]:
                if not cell["date"]:
                    st.markdown(
                        "<div style='height:90px; background:#fafafa; border-radius:4px;'></div>",
                        unsafe_allow_html=True,
                    )
                    continue

                # 背景色：成功=浅绿，失败=浅红，空=白
                bg_map = {"success": "#e8f5e9", "failed": "#ffebee", "empty": "#ffffff"}
                bg = bg_map.get(cell["status"], "#ffffff")

                # 边框色
                border_map = {"success": "#4caf50", "failed": "#f44336", "empty": "#e0e0e0"}
                border = border_map.get(cell["status"], "#e0e0e0")

                day_num = cell["day"]
                events_md = ""
                if cell["events"]:
                    for line in cell["events"].split("\n"):
                        events_md += f"<div style='font-size:10px; line-height:1.3; color:#555;'>{line}</div>"

                # 关键修复：calendar.Calendar 返回的 day_num 已经是 str，这里取 week_cells[i]["day"]
                st.markdown(
                    f"<div style='background:{bg}; border-left:3px solid {border}; "
                    f"padding:6px 8px; height:90px; overflow:hidden; border-radius:0 4px 4px 0;'>"
                    f"<div style='font-size:16px; font-weight:bold; margin-bottom:2px;'>{day_num}</div>"
                    f"<div style='font-size:14px; margin-bottom:2px;'>{cell['emoji']}</div>"
                    f"{events_md}"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    st.divider()

    # ── 月度统计摘要 ──────────────────────────────────────────────────────
    st.markdown("#### 📊 本月运行摘要")

    month_key = f"{view_year}-{view_month:02d}"
    month_entries = [
        e for e in raw_entries
        if e["day"] and e["day"].strftime("%Y-%m") == month_key
    ]

    if month_entries:
        col_stat1, col_stat2, col_stat3, col_stat4 = st.columns(4)
        total_runs = len(month_entries)
        success = sum(1 for e in month_entries if e["result"] == "SUCCESS")
        plans_total = sum(e.get("_plans_count", 0) for e in month_entries)
        with col_stat1:
            st.metric("运行次数", total_runs)
        with col_stat2:
            st.metric("成功率", f"{success/total_runs*100:.0f}%",
                      delta="✅" if success == total_runs else "⚠️")
        with col_stat3:
            st.metric("生成计划数", plans_total)
        with col_stat4:
            days_with_runs = len({e["day"].strftime("%Y-%m-%d") for e in month_entries if e["day"]})
            st.metric("活跃天数", f"{days_with_runs}/{calendar.monthrange(view_year, view_month)[1]}")
    else:
        st.info("本月暂无运行记录")

    st.divider()

    # ── 每日明细列表 ──────────────────────────────────────────────────────
    st.markdown("#### 📋 每日明细")

    selected_date = st.selectbox(
        "选择日期查看详情",
        options=sorted(entries_by_date.keys(), reverse=True),
        format_func=lambda d: d,
    )

    if selected_date and selected_date in entries_by_date:
        day_entries = entries_by_date[selected_date]
        for entry in day_entries:
            phase = entry.get("_phase_emoji", "📌") + " " + entry.get("_phase_name", entry["event_type"])
            result_icon = "✅ 成功" if entry["_ok"] else "❌ 失败"
            conf = entry.get("_confidence", "N/A")
            plans = entry.get("_plans_count", 0)
            insight = entry.get("_insight")
            mod = entry.get("_mod_ratio")

            with st.expander(f"{phase} — {result_icon} | 置信度:{conf} | {plans}计划"
                             + (f" | 修改:{mod}" if mod else "")):
                st.markdown(f"**操作者**: {entry['operator']}")
                st.markdown(f"**结果**: {entry['result']}")

                detail = entry.get("_detail", {})
                if detail:
                    st.markdown("**详情**:")
                    for k, v in detail.items():
                        if k not in ("plans", "insights"):
                            st.markdown(f"  - {k}: `{v}`")

                if insight:
                    st.markdown(f"**洞察**: {insight}")

    # ── 行为洞察报告入口 ─────────────────────────────────────────────────
    st.divider()
    st.markdown("#### 🧠 行为洞察报告")

    col_ai1, col_ai2 = st.columns(2)
    with col_ai1:
        if st.button("📊 近7天行为分析"):
            from audit_analytics import analyze_trading_behavior
            result = analyze_trading_behavior(7)
            st.success(f"运行次数: {result['total_analysis_runs']} | AI采纳率: {result['analysis_success_rate']}%")
            for p in result.get("behavior_patterns", []):
                st.markdown(f"- {p}")
            for r in result.get("recommendations", []):
                st.markdown(f"💡 {r}")

    with col_ai2:
        if st.button("📅 月度报告"):
            from audit_analytics import monthly_report
            result = monthly_report()
            st.metric("活跃天数", result["active_days"])
            st.metric("定时运行", result["scheduled_runs"])
            st.metric("AI自动采纳率", f"{result['auto_adoption_rate']}%")


# ── 视图 4：设置 ────────────────────────────────────────────────────────────

# ── 视图 7：计划审核 ─────────────────────────────────────────────────────

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


def render_tamf_memory():
    """📊 TAMF分析记忆视图 — 标的级分析记忆文件浏览器"""
    import streamlit as st
    from pathlib import Path

    TAMF_DIR = Path(__file__).parent.parent / "data" / "target_memories"

    st.markdown("## 📊 TAMF 投资标的分析记忆")

    # 加载持仓列表
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from pgcrypto_migration import load_positions_from_db
        positions = load_positions_from_db()
        codes = [p["code"] for p in positions]
        names = {p["code"]: p["name"] for p in positions}
        {p["code"]: p.get("code", p["code"]) for p in positions}
    except Exception as e:
        st.error(f"加载持仓失败: {e}")
        return

    col_sel, col_view = st.columns([1, 3])

    with col_sel:
        st.markdown("### 选择标的")
        code_options = [f"{c} {names.get(c, '')}" for c in codes]
        selected_label = st.selectbox("持仓标的", code_options)
        selected_code = selected_label.split(" ")[0].strip()

        # 元数据卡片
        meta_q = """
            SELECT version_major, version_minor, analysis_status, last_updated, data_snapshot
            FROM memory.target_memory_files WHERE ts_code = %s
        """
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(meta_q, (selected_code,))
            row = cur.fetchone()
            conn.close()
            if row:
                vmaj, vmin, status, lupdated, snapshot = row
                snap = snapshot if isinstance(snapshot, dict) else {}
                st.markdown(f"**{names.get(selected_code, selected_code)}**（{selected_code}）")
                st.caption(f"版本 v{vmaj}.{vmin} | 状态 {status} | 更新 {str(lupdated)[:16]}")
                if snap:
                    st.caption(f"行情: {snap.get('last_quote_date','—')} | 公告: {snap.get('last_ann_date','—')}")
            else:
                st.warning("无TAMF元数据")
        except Exception as e:
            st.error(f"查询元数据失败: {e}")

    with col_view:
        tamf_path = TAMF_DIR / f"{selected_code}.md"
        if not tamf_path.exists():
            st.warning(f"TAMF文件不存在: {tamf_path}")
            return

        content = tamf_path.read_text(encoding="utf-8")

        # 子Tab视图
        tabs = st.tabs(["📋 完整文件", "📊 基本面", "📈 技术面", "📰 消息面", "🧠 监控"])

        with tabs[0]:
            st.markdown(content)

        with tabs[1]:
            import re
            # 提取章节一和三
            m = re.search(r"(## 一、标的基本画像.*?)(?=^## |$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")
            m = re.search(r"(## 三、基本面趋势.*?)(?=^## 四|$$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")

        with tabs[2]:
            m = re.search(r"(## 四、技术面与市场表现.*?)(?=^## 五|$$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")

        with tabs[3]:
            m = re.search(r"(## 五、消息面追踪.*?)(?=^## 六|$$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")

        with tabs[4]:
            m = re.search(r"(## 七、跟踪状态与预警.*?)(?=^## 八|$$)", content, re.DOTALL | re.MULTILINE)
            st.markdown(m.group(1) if m else "无数据")

    # 底部时间线
    st.divider()
    st.markdown("### 📅 时间线事件（近30条）")
    tl_q = """
        SELECT event_time, event_type, severity, title, description
        FROM memory.target_timeline_events
        WHERE ts_code = %s
        ORDER BY event_time DESC
        LIMIT 30
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(tl_q, (selected_code,))
        rows = cur.fetchall()
        conn.close()
        if not rows:
            st.info("暂无时间线事件")
        else:
            for r in rows:
                evt_time, evt_type, sev, title, desc = r
                icon = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}.get(sev, "⚪")
                st.markdown(f"{icon} **{evt_type}** `{str(evt_time)[:16]}` {title or ''}")
                if desc:
                    st.caption(f"  {str(desc)[:100]}")
    except Exception as e:
        st.error(f"加载时间线失败: {e}")


def render_plan_review():
    """计划审核页面：读取历史分析中的 plans，滑块+勾选批准/否决，写入 plan_reviews 并记录到 audit_log"""
    st.markdown("## 📝 计划审核")
    st.caption("查看近期分析生成的交易计划，逐项审批执行额度")

    # 初始化 session_state
    if "plan_reviews" not in st.session_state:
        st.session_state["plan_reviews"] = {}

    conn = get_db_connection()
    if not conn:
        st.error("无法连接数据库")
        return
    ensure_plan_review_table(conn)

    # 读取近7日有 plans 的分析记录（只看 plans 非空且非 [] 的记录）
    cur = conn.cursor()
    cur.execute("""
        SELECT run_id, started_at, detail, plans, confidence
        FROM analysis.analysis_runs
        WHERE started_at >= CURRENT_DATE - INTERVAL '7 days'
          AND (plans IS NOT NULL AND plans::text != 'null' AND plans::text != '[]')
        ORDER BY started_at DESC
        LIMIT 20
    """)
    rows = cur.fetchall()

    if not rows:
        st.info("近7日无含计划的分析记录（或计划正文未存储，请确认 run_analysis.py 已更新）")
        conn.close()
        return

    # 构建 run_id → 分析详情的映射
    runs = []
    import json as _json
    for run_id, started_at, detail_raw, plans_raw, confidence in rows:
        try:
            detail = detail_raw if isinstance(detail_raw, dict) else (_json.loads(detail_raw) if detail_raw else {})
        except Exception:
            detail = {}
        plans = plans_raw if isinstance(plans_raw, list) else (_json.loads(plans_raw) if plans_raw else [])
        if plans:
            runs.append({
                "run_id": run_id,
                "started_at": started_at,
                "plans": plans,
                "confidence": confidence or detail.get("confidence", "N/A"),
            })

    if not runs:
        st.info("近期分析无交易计划")
        conn.close()
        return

    st.divider()

    # 读取已有的审核记录
    cur.execute("SELECT run_id, plan_index, decision, position_pct, reason FROM analysis.plan_reviews")
    reviewed = {}
    for row in cur.fetchall():
        reviewed[(row[0], row[1])] = {
            "decision": row[2], "position_pct": row[3], "reason": row[4]
        }

    plan_reviews = st.session_state["plan_reviews"]

    for run in runs:
        run_id = run["run_id"]
        started_at = str(run["started_at"])[:16]
        confidence = run["confidence"]

        with st.expander(f"📌 {started_at}  |  {len(run['plans'])} 项计划  |  置信度: {confidence}", expanded=False):
            for i, plan in enumerate(run["plans"]):
                plan_id = f"{run_id}_{i}"
                key = (run_id, i)
                existing = reviewed.get(key, {})
                default_decision = existing.get("decision", "")
                default_pct = existing.get("position_pct", 50)
                default_reason = existing.get("reason", "")

                # 当前会话中的审核决策
                current_decision = plan_reviews.get(plan_id, default_decision)

                col_label, col_action = st.columns([0.7, 0.3])
                with col_label:
                    st.markdown(f"**[{i+1}] {plan.get('action', 'N/A')}** "
                                f"`{plan.get('code', '')}` {plan.get('name', '')}")
                    st.caption(f"入场 {plan.get('entry', 'N/A')} | 止损 {plan.get('stop_loss', 'N/A')} | "
                               f"目标 {plan.get('target', 'N/A')} | 仓位 {plan.get('position', 'N/A')}")

                # 批准/否决勾选框
                approve_key = f"approve_{plan_id}"
                reject_key = f"reject_{plan_id}"

                col_cb1, col_cb2, col_cb3 = st.columns([0.15, 0.15, 0.7])
                with col_cb1:
                    # 已审核的显示标签，不重复勾选
                    if existing and existing.get("decision"):
                        st.caption(f"{'✅ 已批准' if existing['decision'] == 'approved' else '❌ 已否决'}")
                    else:
                        is_approved = st.checkbox("✅ 批准", value=(current_decision == "approved"),
                                                  key=approve_key)
                        if is_approved:
                            plan_reviews[plan_id] = "approved"
                        elif plan_reviews.get(plan_id) == "approved":
                            del plan_reviews[plan_id]

                with col_cb2:
                    if not (existing and existing.get("decision")):
                        is_rejected = st.checkbox("❌ 否决", value=(current_decision == "rejected"),
                                                  key=reject_key)
                        if is_rejected:
                            plan_reviews[plan_id] = "rejected"
                        elif plan_reviews.get(plan_id) == "rejected":
                            del plan_reviews[plan_id]

                with col_cb3:
                    if current_decision in ("approved", "rejected"):
                        st.slider(
                            "执行仓位%",
                            min_value=10, max_value=100,
                            value=default_pct,
                            step=10,
                            key=f"pct_{plan_id}",
                        )
                        reason = st.text_input(
                            "备注",
                            value=default_reason,
                            key=f"reason_{plan_id}",
                            placeholder="同意/否决原因...",
                        )
                    elif existing:
                        st.caption(f"已审核: {'✅ 批准' if default_decision == 'approved' else '❌ 否决'} ({default_pct}%) | {default_reason or '无备注'}")

    # ── 审核提交按钮 ─────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 📋 审核汇总")

    pending_approvals = {k: v for k, v in plan_reviews.items() if v in ("approved", "rejected")}
    approved_count = sum(1 for v in pending_approvals.values() if v == "approved")
    rejected_count = sum(1 for v in pending_approvals.values() if v == "rejected")

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        st.metric("待提交-已批准", f"{approved_count} 条")
    with col_s2:
        st.metric("待提交-已否决", f"{rejected_count} 条")

    if st.button("✅ 确认审核（写入 audit_log）", key="submit_plan_reviews", type="primary"):
        if not pending_approvals:
            st.warning("暂无待提交的审核")
        else:
            from storage_factory import StorageFactory
            storage = StorageFactory()

            # 汇总写入 audit_log
            summary = {
                "approved": approved_count,
                "rejected": rejected_count,
                "plans": [
                    {"plan_id": pid, "decision": dec}
                    for pid, dec in pending_approvals.items()
                ]
            }
            ok = storage.write_audit(
                event_type="PLAN_REVIEWED",
                operator="manual",
                detail=summary,
                result="SUCCESS"
            )
            if ok:
                # 同步写入 plan_reviews 表（每条）
                for run in runs:
                    run_id = run["run_id"]
                    for i, plan in enumerate(run["plans"]):
                        plan_id = f"{run_id}_{i}"
                        dec = pending_approvals.get(plan_id)
                        if not dec:
                            continue
                        pct = st.session_state.get(f"pct_{plan_id}", 50)
                        reason = st.session_state.get(f"reason_{plan_id}", "") or ""
                        cur.execute("""
                            INSERT INTO analysis.plan_reviews (run_id, plan_index, decision, position_pct, reason)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (run_id, plan_index)
                            DO UPDATE SET decision = EXCLUDED.decision,
                                          position_pct = EXCLUDED.position_pct,
                                          reason = EXCLUDED.reason,
                                          reviewed_at = CURRENT_TIMESTAMP
                        """, (run_id, i, dec, pct, reason))
                conn.commit()
                st.success(f"已批准 {approved_count} 条 / 已否决 {rejected_count} 条 — 已写入 audit.audit_log")
                # 清空已提交
                for pid in pending_approvals:
                    del plan_reviews[pid]
                st.rerun()
            else:
                st.error("写入 audit_log 失败")

    conn.close()


# ── 视图 8：设置 ───────────────────────────────────────────────────────

def render_settings():
    st.markdown("## ⚙️ 系统设置")
    st.info("配置管理面板（Phase 2 v0.1）")

    st.markdown("### 数据源")
    st.markdown(f"- 持仓文件: `{POSITIONS_CSV}`")
    st.markdown("- 数据库: `postgresql://invest_admin@localhost:5432/investpilot`")
    st.markdown("- 行情: 东方财富基金 API + 新浪财经")
    st.markdown("- 新闻: 同花顺快讯 + 新浪财经 + 金十数据（财联社已停用）")
    st.markdown("- 研报: 东方财富研报 API（16:00 每日采集）")

    st.markdown("### 定时任务")
    st.markdown("- 08:30 盘前工作流")
    st.markdown("- 15:30 盘后工作流")
    st.markdown("- 16:00 研报采集工作流")
    st.markdown("- 21:00 晚间工作流")
    st.markdown("- 每日向量嵌入任务（新闻 + 研报）")

    if st.button("🔄 手动触发盘前分析"):
        with st.spinner("运行中..."):
            from schedule_runner import job_morning
            job_morning()
        st.success("盘前分析完成！")

    if st.button("📊 手动触发向量化"):
        with st.spinner("向量化新闻..."):
            from embedding_service import daily_embedding_job
            daily_embedding_job()
        st.success("向量化完成！")


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
    elif page == "⚙️ 设置":
        render_settings()


if __name__ == "__main__":
    # 委托给模块化版本 dashboard_views/__main__.py
    from dashboard_views.__main__ import main as _dm
    _dm()
