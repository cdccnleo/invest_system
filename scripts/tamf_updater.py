"""
TAMF Updater — 投资标的分析记忆文件（TAMF）自动更新引擎
与 RouterAgent 和 schedule_runner.py (APScheduler) 集成

核心职责:
  1. 增量更新TAMF文件（仅更新受影响的章节）
  2. 检测各数据源变化，触发对应章节更新
  3. Agent驱动的智能段落生成（基本面/技术面/消息面/反思笔记）
  4. 事件驱动的即时更新（交易/公告/评级变动）
"""

import os
import re
import json
import hashlib
import logging
import subprocess
from math import floor
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
import psycopg2
from psycopg2 import pool

# ─── 项目路径 ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TAMF_DIR = PROJECT_ROOT / "data" / "target_memories"
TAMF_TEMPLATE = TAMF_DIR / "TEMPLATE.md"
CREDENTIAL_STORE = Path.home() / ".hermes" / "invest_credentials" / "store.json"

# ─── 代码归一化 ──────────────────────────────────────────────
# holdings.code (6位纯数字) → daily_quotes.ts_code (6位.交易所后缀)
FUND_CODES = {'001982','007355','016452','018967','159516','159819','512880',
              '515050','515700','516650','518880','561160','562500','563230',
              '588190','588290','002943'}

def normalize_ts_code(code: str, name: str = "") -> str:
    """
    将 holdings.encrypted_positions.code 归一化为 market.daily_quotes.ts_code 格式。
    
    规则:
    - 基金(ETF/LOF/普通基金): code.OF
    - 科创板(688xxx): code.XSHG (上交所)
    - 上交所(600xxx/601xxx/603xxx): code.XSHG
    - 深交所(000xxx/002xxx/300xxx): code.XSHE
    """
    c = code.strip()
    
    # 基金判断
    if c in FUND_CODES or 'ETF' in name or '基金' in name or '指数' in name:
        return f"{c}.OF"
    
    # 按代码范围判断交易所
    if c.startswith('688'):
        return f"{c}.XSHG"   # 科创板
    if c.startswith(('600','601','603','605')):
        return f"{c}.XSHG"   # 上交所
    if c.startswith(('000','001','002','003','300')):
        return f"{c}.XSHE"   # 深交所
    
    # 退市债等
    if c.startswith('4') or '退' in name:
        return f"{c}.XSHE"
    
    # 默认深交所（A股最常见）
    return f"{c}.XSHE"


# ─── 线程安全连接池 ───────────────────────────────────────────
_db_pool = None


import threading

_thread_local = threading.local()

def _get_thread_conn():
    """获取当前线程专属连接（线程内复用，避免ThreadedConnectionPool的unkeyed错误）"""
    if not hasattr(_thread_local, 'conn') or _thread_local.conn is None:
        from pgcrypto_migration import get_credential
        _thread_local.conn = psycopg2.connect(
            host="localhost", port=5432, database="investpilot",
            user="invest_admin", password=get_credential("DB_PASSWORD")
        )
        _thread_local.conn.autocommit = True
    return _thread_local.conn

def get_db_conn():
    """兼容旧接口：返回当前线程专属连接"""
    return _get_thread_conn()

def release_db_conn(conn):
    """兼容旧接口：连接由线程自己持有，不放回池（no-op）"""
    pass

def close_db_pool():
    """关闭所有线程的连接"""
    if hasattr(_thread_local, 'conn') and _thread_local.conn:
        _thread_local.conn.close()
        _thread_local.conn = None


# ─── 第1层：数据获取 ─────────────────────────────────────────

def load_positions() -> list[dict]:
    """从 holdings.encrypted_positions 加载当前持仓（复用pgcrypto_migration解密）"""
    from pgcrypto_migration import load_positions_from_db
    raw = load_positions_from_db()
    return [
        {"code": r["code"], "name": r["name"], "shares": r["shares"],
         "avg_cost": r["cost"], "market_value": r["market_value"],
         "pnl_amount": r["profit"], "pnl_pct": r["profit_pct"]}
        for r in raw
    ]


def load_recent_quotes(ts_code: str, days: int = 20) -> list[dict]:
    """获取近N日行情（用于技术面分析）"""
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, open_price, high_price, low_price, close_price, volume, amount, change_pct
            FROM market.daily_quotes
            WHERE ts_code = %s
            ORDER BY trade_date DESC
            LIMIT %s
        """, (ts_code, days))
        rows = cur.fetchall()
        if not rows:
            return []
        return [
            {"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5], "amount": r[6], "change_pct": r[7]}
            for r in rows
        ]
    finally:
        release_db_conn(conn)


def load_financial_trend(ts_code: str, quarters: int = 8) -> list[dict]:
    """获取近N季度财务指标"""
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT report_date, report_type, total_revenue, net_profit, gross_margin,
                   net_margin, debt_ratio, roe, yoy_growth, profit_growth
            FROM market.financial_indicators
            WHERE ts_code = %s
            ORDER BY report_date DESC
            LIMIT %s
        """, (ts_code, quarters))
        rows = cur.fetchall()
        return [
            {"date": r[0], "type": r[1], "revenue": r[2], "profit": r[3],
             "gross_margin": r[4], "net_margin": r[5], "debt_ratio": r[6],
             "roe": r[7], "yoy_growth": r[8], "profit_growth": r[9]}
            for r in rows
        ]
    finally:
        release_db_conn(conn)


def load_recent_announcements(ts_code: str, limit: int = 10) -> list[dict]:
    """获取近N条公告"""
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ann_id, ts_code, title, ann_type, notice_date
            FROM research.announcements
            WHERE ts_code = %s
            ORDER BY notice_date DESC NULLS LAST, created_at DESC
            LIMIT %s
        """, (ts_code, limit))
        rows = cur.fetchall()
        return [
            {"id": r[0], "ts_code": r[1], "title": r[2], "type": r[3],
             "date": r[4]}
            for r in rows
        ]
    finally:
        release_db_conn(conn)


def load_recent_reports(ts_code: str, limit: int = 5) -> list[dict]:
    """获取近N篇研报"""
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, ts_code, title, summary, rating, report_date, source
            FROM research.research_reports
            WHERE ts_code = %s
            ORDER BY report_date DESC NULLS LAST, created_at DESC
            LIMIT %s
        """, (ts_code, limit))
        rows = cur.fetchall()
        return [
            {"id": r[0], "ts_code": r[1], "title": r[2], "summary": r[3],
             "rating": r[4], "date": r[5], "source": r[6]}
            for r in rows
        ]
    finally:
        release_db_conn(conn)


# ─── 第2层：TAMF文件读写 ────────────────────────────────────

def get_tamf_path(code: str) -> Path:
    return TAMF_DIR / f"{code}.md"


def read_tamf(code: str) -> Optional[str]:
    p = get_tamf_path(code)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def write_tamf(code: str, content: str) -> None:
    """
    写入 TAMF 文件。
    若影子模式激活，则写入影子目录（不触达生产文件）。
    """
    # ── 影子模式检测 ────────────────────────────────
    try:
        from tamf_shadow import is_shadow_mode_active, tamf_shadow_write
        if is_shadow_mode_active():
            tamf_shadow_write(code, content)
            return
    except ImportError:
        pass

    p = get_tamf_path(code)
    p.parent.mkdir(parents=True, exist_ok=True)

    # ── 写入前备份旧版本 ────────────────────────────────
    import shutil, datetime as _dt
    if p.exists():
        backup_dir = TAMF_DIR.parent / "target_memories_shadow" / "auto_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(p, backup_dir / f"{code}_{ts}.md")

    p.write_text(content, encoding="utf-8")


def get_tamf_metadata(code: str) -> Optional[dict]:
    """从memory.target_memory_files读取某标的的元数据"""
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ts_code, stock_name, version_major, version_minor,
                   analysis_status, last_updated, data_snapshot
            FROM memory.target_memory_files WHERE ts_code = %s
        """, (code,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "ts_code": row[0], "stock_name": row[1],
            "version_major": row[2], "version_minor": row[3],
            "analysis_status": row[4], "last_updated": row[5],
            "data_snapshot": row[6] if isinstance(row[6], dict) else json.loads(row[6]) if row[6] else {}
        }
    finally:
        release_db_conn(conn)


def upsert_tamf_metadata(code: str, stock_name: str,
                          version_major: int, version_minor: int,
                          analysis_status: str,
                          data_snapshot: dict,
                          file_path: str) -> None:
    """upsert到memory.target_memory_files"""
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO memory.target_memory_files
                (ts_code, stock_name, file_path, version_major, version_minor,
                 analysis_status, data_snapshot, last_updated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ts_code) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                version_major = EXCLUDED.version_major,
                version_minor = EXCLUDED.version_minor,
                analysis_status = EXCLUDED.analysis_status,
                data_snapshot = EXCLUDED.data_snapshot,
                last_updated = NOW()
        """, (code, stock_name, file_path, version_major, version_minor,
              analysis_status, json.dumps(data_snapshot)))
        conn.commit()
    finally:
        release_db_conn(conn)


# ─── 第3层：章节更新 ─────────────────────────────────────────

def detect_manual_edit(content: str) -> tuple[str, list[str]]:
    """
    检测被 <!-- MANUAL EDIT --> 保护的段落。
    返回: (清理后内容, [受保护段落的锚点文本列表])
    """
    protected = []
    pattern = re.compile(r'<!--\s*MANUAL\s*EDIT:.*?-->', re.IGNORECASE | re.DOTALL)
    for m in pattern.finditer(content):
        protected.append(m.group())
    cleaned = pattern.sub('', content)
    return cleaned, protected


def is_section_manually_edited(content: str, section_heading: str) -> bool:
    """
    检查指定章节（通过 ### Agent 标题定位）是否被手动编辑过。
    检查该章节块内是否有 <!-- MANUAL EDIT --> 标记。
    """
    idx = content.find(section_heading)
    if idx < 0:
        return False
    # 取该章节下一个 ## 标题之前的内容
    after = content[idx + len(section_heading):]
    next_section = after.find('\n## ')
    block = after[:next_section] if next_section > 0 else after
    return bool(re.search(r'<!--\s*MANUAL\s*EDIT:', block, re.IGNORECASE))


def detect_affected_sections(new_data: dict) -> list[str]:
    """根据新数据判断需要更新哪些章节"""
    affected = []
    if new_data.get("quotes"):
        affected.extend(["section_4_technical", "section_7_monitoring"])
    if new_data.get("announcements"):
        # new_data["announcements"] is a bool here (from detect_new_data_for_target)
        # 公告类型检测需从数据库实时查询，这里简化为宽泛触发
        affected.extend([
            "section_5_news",
            "section_3_fundamentals",
            "section_1_basic_profile",
            "section_2_holdings",
        ])
    if new_data.get("transactions"):
        affected.append("section_2_holdings")
    if new_data.get("reports"):
        affected.append("section_5_news")
    if new_data.get("financials"):
        affected.append("section_3_fundamentals")
    return list(set(affected))


def build_section_2_holdings(pos: dict) -> str:
    """生成章节二：持仓历程"""
    mv = pos.get("market_value") or 0
    pnl = pos.get("pnl_amount") or 0
    pnl_pct = pos.get("pnl_pct") or 0
    shares = pos.get("shares") or 0
    avg_cost = pos.get("avg_cost") or 0
    # 从市值反推现价，避免 profit_pct 脏数据干扰
    current_price = (mv / shares) if shares and mv else avg_cost
    position_pct = "—" if not pos.get("weight") else f"{pos.get('weight', 0):.1f}%"
    
    return f"""### 当前持仓状态
| 持有数量 | 平均成本 | 现价 | 持仓市值 | 盈亏金额 | 盈亏% | 仓位占比 |
|---------|---------|------|---------|---------|------|---------|
| {shares:,.0f} | {avg_cost:.3f} | {current_price:.3f} | ¥{mv:,.0f} | {'+' if pnl >= 0 else ''}{pnl:,.0f} | {'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}% | {position_pct} |

### 操作历史时间线
<!-- 暂无操作记录，数据来自 holdings.encrypted_positions -->

### 公司行为记录
<!-- 暂无公司行为记录 -->
"""


def build_section_4_technical(quotes: list[dict]) -> str:
    """生成章节四：技术面"""
    if not quotes or len(quotes) < 2:
        return """### 近期关键价位
| 指标 | 数值 | 日期 |
|------|------|------|
| ⚠️ 数据不足 | — | — |

### Agent 技术面简评
```
⚠️ 数据不足 (market.daily_quotes 中无该标的记录，coverage: 0%)
```
"""
    
    latest = quotes[0]
    close = latest.get("close") or 0
    high_20d = max(q.get("high") or 0 for q in quotes[:20])
    low_20d = min(q.get("low") or 0 for q in quotes[:20])
    
    # 计算MA20
    ma20_prices = [q.get("close") for q in quotes[:20] if q.get("close")]
    ma20 = sum(ma20_prices) / len(ma20_prices) if ma20_prices else 0
    deviation = ((close - ma20) / ma20 * 100) if ma20 else 0
    drawdown = ((close - high_20d) / high_20d * 100) if high_20d else 0
    
    # 简单技术评语
    trend = "上升趋势" if deviation > 2 else ("下降趋势" if deviation < -2 else "横盘整理")
    signal = "🔴 偏离MA20上方" if deviation > 5 else ("🟢 贴近MA20下方" if deviation < -5 else "⚪ MA20附近")
    
    return f"""### 近期关键价位
| 指标 | 数值 | 日期 |
|------|------|------|
| 近20日最高价 | ¥{high_20d:.3f} | {latest.get('date', '—')} |
| 近20日最低价 | ¥{low_20d:.3f} | — |
| 当前距20日高点 | {drawdown:.2f}% | — |
| 近20日均价 | ¥{ma20:.3f} | — |
| MA20偏离度 | {deviation:+.2f}% | — |

### Agent 技术面简评
```
当前价¥{close:.3f}，{trend}，{signal}({deviation:+.1f}%)。
近期20日高点¥{high_20d:.3f}，低点¥{low_20d:.3f}。
数据覆盖{len(quotes)}个交易日。
```
"""


def build_section_3_fundamentals(financials: list[dict], reports: list[dict]) -> str:
    """生成章节三：基本面趋势"""
    if not financials and not reports:
        return """### 财务指标趋势（最近 8 个季度）
| 指标 | Q-7 | Q-6 | ... | Q-1(最新) | 趋势 | 预警 |
|------|-----|-----|-----|----------|------|:---:|
| ⚠️ 数据不足 | — | — | — | — | — | — |

### Agent 基本面综合评估
```
⚠️ 数据不足 (market.financial_indicators 中无该标的记录)
```
"""
    
    fin_rows = ""
    if financials:
        headers = ["指标", "Q-7", "Q-6", "Q-5", "Q-4", "Q-3", "Q-2", "Q-1(最新)", "趋势", "预警"]
        fin_rows = "| 指标 | Q-7 | Q-6 | Q-5 | Q-4 | Q-3 | Q-2 | Q-1(最新) | 趋势 | 预警 |\n|------|-----|-----|-----|-----|-----|-----|----------|------|:---:|\n"
        
        fields = [
            ("营业收入(亿)", "revenue", lambda v: f"{v/1e8:.2f}" if v else "—"),
            ("归母净利润(亿)", "profit", lambda v: f"{v/1e8:.2f}" if v else "—"),
            ("毛利率%", "gross_margin", lambda v: f"{v:.1f}" if v else "—"),
            ("净利率%", "net_margin", lambda v: f"{v:.1f}" if v else "—"),
            ("ROE%", "roe", lambda v: f"{v:.1f}" if v else "—"),
            ("资产负债率%", "debt_ratio", lambda v: f"{v:.1f}" if v else "—"),
        ]
        
        rev_data = [f.get("revenue") for f in reversed(financials)]
        for fname, fkey, fmt in fields:
            vals = [fmt(f.get(fkey)) for f in reversed(financials)]
            vals_str = " | ".join(vals[:8])
            if len(vals) < 8:
                vals_str += " | " + " | ".join(["—"] * (8 - len(vals)))
            trend = "→"
            if len(vals) >= 2 and vals[-1] != "—" and vals[-2] != "—":
                try:
                    trend = "↑" if float(vals[-1].replace('—','0')) > float(vals[-2].replace('—','0')) else "↓"
                except ValueError:
                    trend = "→"
            fin_rows += f"| {fname} | {vals_str} | {trend} | — |\n"
    
    # 研报汇总
    report_summary = ""
    if reports:
        report_lines = "\n".join([
            f"| {r.get('date','—')} | {r.get('source','?')} | {r.get('rating','?')} | {r.get('title','')[:50]} |"
            for r in reports[:3]
        ])
        report_summary = f"""
### 近期研报观点（最近 3 篇）
| 日期 | 机构 | 评级 | 标题 |
|------|------|:---:|------|
{report_lines}
"""
    
    return f"""### 财务指标趋势（最近 8 个季度）
⚠️ 数据来源: market.financial_indicators (仅{floor(len(financials)/2) if financials else 0}个季度)
{fin_rows if fin_rows else '| ⚠️ 无财务数据 | — |'}

### Agent 基本面综合评估
```
{'⚠️ 数据不足，无法生成基本面评估' if not financials else '基于现有数据生成评估（需Agent补充）'}
```
{report_summary}
"""


def build_section_5_news(anns: list[dict], reports: list[dict]) -> str:
    """生成章节五：消息面"""
    ann_lines = ""
    if anns:
        for a in anns[:3]:
            ann_lines += f"| {a.get('date','—')} | {a.get('type','?')} | {a.get('title','')[:50]} | — |\n"
    else:
        ann_lines = "| ⚠️ 无公告数据 | — | — | — |\n"
    
    report_lines = ""
    if reports:
        for r in reports[:3]:
            report_lines += f"| {r.get('date','—')} | {r.get('source','?')} | {r.get('rating','?')} | {r.get('title','')[:40]} |\n"
    else:
        report_lines = "| ⚠️ 无研报数据 | — | — | — |\n"
    
    return f"""### 近期重要公告（最近 3 条）
| 日期 | 类型 | 摘要 | Agent 判断影响 |
|------|------|------|------|
{ann_lines}

### 近期研报观点（最近 3 篇）
| 日期 | 机构 | 评级 | 标题 |
|------|------|:---:|------|
{report_lines}

### Agent 消息面综合判断
```
{'⚠️ 数据不足 (research.announcements 覆盖率低)' if not anns else '基于公告数据生成（需Agent补充）'}
```
"""


def build_tamf_file(code: str, name: str, pos: dict,
                    financials: list, anns: list, reports: list,
                    quotes: list) -> str:
    """为某标的生成完整TAMF文件内容"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    content = f"""# {name}（{code}）投资分析记忆

> **最后更新**: {ts}
> **记忆版本**: v1.0  （major: 重大基本面变化, minor: 日常更新）
> **分析状态**: 🟢 持有中
> **关联技能**: 

---

## 一、标的基本画像（静态层 — 季度更新）

| 属性 | 值 | 最后更新 | 数据来源 |
|------|-----|---------|---------|
| 所属行业 | — | — | 东方财富F10 |
| 细分行业 | — | — | 东方财富F10 |
| 上市日期 | — | — | 交易所 |
| 总市值(亿) | — | — | 日行情 |
| 流通市值(亿) | — | — | 日行情 |
| PE(TTM) | — | — | 财务数据 |
| PB | — | — | 财务数据 |
| ROE | — | — | 最新季报 |

### 核心业务与护城河
<!-- ⚠️ 待Agent生成（需研报数据支持）-->

### 关键风险因子
<!-- ⚠️ 待Agent生成 -->

---

## 二、持仓历程（动态层 — 每次操作更新）

{build_section_2_holdings(pos)}

---

## 三、基本面趋势（分析层 — 每次季报/重大公告更新）

{build_section_3_fundamentals(financials, reports)}

---

## 四、技术面与市场表现（分析层 — 每日更新）

{build_section_4_technical(quotes)}

---

## 五、消息面追踪（分析层 — 每日更新）

{build_section_5_news(anns, reports)}

---

## 六、历史决策记忆（元记忆层 — 每次复盘更新）

### 关键决策案例
<!-- 暂无决策记录 -->

### Agent 反思笔记
```
⚠️ 暂无足够历史数据支持反思笔记生成
```
---

## 七、跟踪状态与预警（监控层 — 实时更新）

| 监控维度 | 状态 | 阈值 | 触发时动作 |
|---------|:---:|------|------|
| 日内涨跌幅 | ⚪ | ±5% | 推送→熔断 |
| 距成本价偏离 | ⚪ | -8%止损 | 推送提醒 |
| 公告待处理 | 0条 | >0即推送 | 推送→自动更新成本 |
| 研报评级变动 | 无 | 下调即推送 | 推送→标记观察 |
| 连续下跌天数 | 0天 | ≥5天推送 | 推送→建议复盘 |
| 上次分析距今 | 0天 | >7天提醒 | 推送→触发深度分析 |
| 情绪负面累积 | 0条 | 3条/周即告警 | 推送→标记风险 |

---

## 八、AI 操作建议历史（决策追溯层 — 每次计划生成后追加）

### 最近 10 次操作建议
<!-- 暂无操作建议记录 -->

---

*本文件由 Hermes Agent 自动维护，每日盘后更新*
*手动修改请在修改后标注 <!-- MANUAL EDIT: [原因] -->*
*完整审计日志见: audit.audit_log WHERE entity_id = '{code}'*
"""
    return content


# ─── 第4层：批量初始化 ────────────────────────────────────────

def init_all_tamf_files() -> dict:
    """
    批量为所有持仓生成初始TAMF文件。
    在非交易时段执行（T1.4任务入口）。
    """
    TAMF_DIR.mkdir(parents=True, exist_ok=True)
    
    positions = load_positions()
    results = {"success": 0, "failed": 0, "errors": []}
    
    for pos in positions:
        code = pos["code"]
        name = pos["name"]
        try:
            ts_code = normalize_ts_code(code, name)
            
            # 并行加载各数据源（线程池）
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                f_fin = pool.submit(load_financial_trend, ts_code, 8)
                f_ann = pool.submit(load_recent_announcements, ts_code, 10)
                f_rep = pool.submit(load_recent_reports, ts_code, 5)
                f_quo = pool.submit(load_recent_quotes, ts_code, 20)
                
                financials = f_fin.result()
                anns = f_ann.result()
                reports = f_rep.result()
                quotes = f_quo.result()
            
            content = build_tamf_file(code, name, pos, financials, anns, reports, quotes)
            write_tamf(code, content)
            
            # 写入DB元数据
            upsert_tamf_metadata(
                code=code,
                stock_name=name,
                version_major=1,
                version_minor=0,
                analysis_status="ACTIVE",
                data_snapshot={
                    "last_quote_date": str(quotes[0].get("date")) if quotes and quotes[0].get("date") else None,
                    "last_ann_date": str(anns[0].get("date")) if anns and anns[0].get("date") else None,
                    "last_report_date": str(reports[0].get("date")) if reports and reports[0].get("date") else None,
                    "initialized_at": datetime.now().isoformat(),
                },
                file_path=str(get_tamf_path(code))
            )
            
            # 记录时间线事件
            conn = get_db_conn()
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO memory.target_timeline_events
                        (ts_code, event_time, event_type, event_source, severity, title, description)
                    VALUES (%s, NOW(), 'TAMF_INIT', 'SYSTEM', 'INFO',
                            'TAMF文件首次初始化', '批量初始化脚本生成初始TAMF文件')
                """, (code,))
                conn.commit()
            finally:
                release_db_conn(conn)

            results["success"] += 1
            
        except Exception as e:
            results["failed"] += 1
            results["errors"].append(f"{code} {name}: {str(e)}")
    
    return results


# ─── 第5层：增量更新（每日盘后） ─────────────────────────────

def detect_new_data_for_target(code: str) -> dict:
    """
    检测自上次TAMF更新以来，该标的有哪些新数据到达。
    """
    meta = get_tamf_metadata(code)
    if not meta:
        # 无元数据 = 首次全量更新
        return {
            "has_new_data": True,
            "new_items": {
                "quotes": True, "announcements": True,
                "reports": True, "financials": True
            },
            "affected_sections": [
                "section_1_basic_profile", "section_2_holdings",
                "section_3_fundamentals", "section_4_technical",
                "section_5_news"
            ]
        }
    
    last_update = meta.get("last_updated", datetime.min)
    snapshot = meta.get("data_snapshot", {})
    
    # 对每个数据源检查是否有新记录
    ts_code = normalize_ts_code(code)
    new_data = {}
    
    # 检查行情
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM market.daily_quotes
            WHERE ts_code = %s AND trade_date > %s
        """, (ts_code, last_update))
        new_quote_count = cur.fetchone()[0]
        new_data["quotes"] = new_quote_count > 0

        # 检查公告
        cur.execute("""
            SELECT COUNT(*) FROM research.announcements
            WHERE ts_code = %s AND (notice_date > %s OR created_at > %s)
        """, (code, last_update, last_update))
        new_ann_count = cur.fetchone()[0]
        new_data["announcements"] = new_ann_count > 0

        # 检查研报
        cur.execute("""
            SELECT COUNT(*) FROM research.research_reports
            WHERE ts_code = %s AND created_at > %s
        """, (code, last_update))
        new_report_count = cur.fetchone()[0]
        new_data["reports"] = new_report_count > 0

        # 检查财务
        cur.execute("""
            SELECT COUNT(*) FROM market.financial_indicators
            WHERE ts_code = %s AND created_at > %s
        """, (ts_code, last_update))
        new_fin_count = cur.fetchone()[0]
        new_data["financials"] = new_fin_count > 0
    finally:
        release_db_conn(conn)

    affected = detect_affected_sections(new_data)
    return {
        "has_new_data": any(new_data.values()),
        "new_items": new_data,
        "affected_sections": affected
    }


def incremental_update(code: str) -> dict:
    """
    增量更新单个标的TAMF文件：只更新受影响的章节，保护手动编辑。
    """
    # 读取现有内容
    current = read_tamf(code)
    if not current:
        return {"status": "no_file", "code": code}
    
    # 检测手动编辑保护段落
    current_cleaned, protected = detect_manual_edit(current)
    
    # 获取持仓数据
    positions = load_positions()
    pos = next((p for p in positions if p["code"] == code), None)
    if not pos:
        return {"status": "no_position", "code": code}
    
    name = pos["name"]
    ts_code = normalize_ts_code(code, name)
    
    # 获取变化检测
    detection = detect_new_data_for_target(code)
    if not detection["has_new_data"]:
        return {"status": "no_change", "code": code}
    
    affected = detection["affected_sections"]
    
    # 加载新数据
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        f_fin = pool.submit(load_financial_trend, ts_code, 8)
        f_ann = pool.submit(load_recent_announcements, ts_code, 10)
        f_rep = pool.submit(load_recent_reports, ts_code, 5)
        f_quo = pool.submit(load_recent_quotes, ts_code, 20)
        financials = f_fin.result()
        anns = f_ann.result()
        reports = f_rep.result()
        quotes = f_quo.result()
    
    # 重建受影响的章节（简化策略：整体重写，保留手动编辑段落）
    # 实际上这里用"分章节重建"策略，只更新特定章节
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 替换对应章节（正则匹配到第一个 --- 分隔符）
    new_content = current_cleaned
    
    # 提取版本信息
    version_match = re.search(r'\*\*记忆版本\*\*:', current)
    version_line = version_match.group() if version_match else ""
    
    # 重新构建全文
    new_content = build_tamf_file(code, name, pos, financials, anns, reports, quotes)
    
    # 恢复手动编辑保护内容（追加到末尾）
    if protected:
        new_content += "\n\n---\n\n## 手动编辑记录\n"
        for p in protected:
            new_content += p + "\n"
    
    write_tamf(code, new_content)
    
    # 触发Agent段落生成（技术面/基本面/消息面 — 异步并行）
    # 仅当有行情数据时调用，避免无数据下LLM幻觉
    if quotes:
        try:
            agent_updated = update_agent_sections_in_tamf(
                code, name, quotes, financials, anns, reports
            )
            if agent_updated:
                write_tamf(code, agent_updated)
        except Exception as e:
            import logging
            logging.getLogger("tamf_updater").warning(f"Agent段落更新失败 {code}: {e}")
    
    # 更新元数据
    meta = get_tamf_metadata(code)
    vmaj = (meta["version_major"] + 1) if meta else 1
    vmin = 0
    
    upsert_tamf_metadata(
        code=code,
        stock_name=name,
        version_major=vmaj,
        version_minor=vmin,
        analysis_status="ACTIVE",
        data_snapshot={
            "last_quote_date": str(quotes[0].get("date")) if quotes and quotes[0].get("date") else None,
            "last_ann_date": str(anns[0].get("date")) if anns and anns[0].get("date") else None,
            "last_report_date": str(reports[0].get("date")) if reports and reports[0].get("date") else None,
            "last_update": datetime.now().isoformat(),
        },
        file_path=str(get_tamf_path(code))
    )
    
    return {
        "status": "updated",
        "code": code,
        "affected_sections": affected,
        "version": f"v{vmaj}.{vmin}"
    }


def parallel_update_all_holdings(max_workers: int = 2) -> dict:
    """
    并行更新所有持仓标的的 TAMF 文件。
    使用 ThreadPoolExecutor 并发执行 incremental_update，
    显著缩短总耗时（从串行约 5 分钟降至约 1.5 分钟）。

    max_workers=2 配合 ThreadedConnectionPool(maxconn=16) 使用，
    避免数据库连接池耗尽（原 max_workers=4 在嵌套线程池场景下
    曾导致 46 个持仓全部失败）。
    """
    import concurrent.futures
    from concurrent.futures import ThreadPoolExecutor, as_completed

    positions = load_positions()
    results = {"total": len(positions), "updated": 0, "skipped": 0, "failed": 0,
               "details": {}}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(incremental_update, pos["code"]): pos["code"]
            for pos in positions
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                r = future.result(timeout=120)
                results["details"][code] = r
                if r.get("status") == "updated":
                    results["updated"] += 1
                else:
                    results["skipped"] += 1
            except Exception as e:
                results["failed"] += 1
                results["details"][code] = {"status": "error", "error": str(e)}
                logger = logging.getLogger("tamf_updater")
                logger.error(f"TAMF 并行更新失败: {code}, {e}")

    return results


def scheduled_update_all_holdings() -> dict:
    """
    定时任务入口：遍历所有持仓，检测新数据并增量更新。
    注册到 APScheduler: 每日 15:35

    若影子模式激活:
      - 所有更新写入影子目录（data/target_memories_shadow/）
      - 每 N 个影子周期完成后发送差异摘要通知
      - 生产文件不受影响
    """
    # ── 影子模式周期启动 ────────────────────────────
    shadow_active = False
    try:
        from tamf_shadow import is_shadow_mode_active, TamfShadowMode
        shadow_active = is_shadow_mode_active()
        if shadow_active:
            shadow = TamfShadowMode()
            shadow.state["cycle_count"] = shadow.state.get("cycle_count", 0) + 1
            from tamf_shadow import _save_state
            _save_state(shadow.state)
    except ImportError:
        pass

    results = parallel_update_all_holdings(max_workers=4)

    # ── 影子模式周期后处理 ──────────────────────────
    if shadow_active and results["updated"] > 0:
        try:
            shadow = TamfShadowMode()
            report = shadow.status_report()
            # 发送影子运行通知
            try:
                from notification import send_notification
                shadow_msg = (
                    f"🕶️ TAMF 影子模式运行报告 (周期#{report['cycle_count']})\n\n"
                    f"持仓总数: {results['total']}\n"
                    f"本次更新: {results['updated']} 只\n"
                    f"影子化累计: {report['total_shadow_files']} 只\n"
                    f"有差异: {report['with_differences']} 只 ({report['total_diff_lines']}行)\n"
                    f"待晋升: {len(report['holdings_shadowed'])} 只\n\n"
                    f"⚠️ 生产文件未受影响。请用 `python tamf_shadow.py diff` 审查差异。"
                )
                send_notification("🕶️ TAMF 影子运行", shadow_msg, level="INFO")
            except Exception:
                pass
            logger = logging.getLogger("tamf_updater")
            logger.info(f"影子模式周期#{report['cycle_count']}完成: {results['updated']}只更新, "
                        f"{report['with_differences']}只有差异")
        except Exception:
            pass

    return results


def scheduled_deep_analysis_weekly() -> dict:
    """
    周频深度分析 — 每周日22:00运行，对所有持仓标的进行全面的Agent段落重生成。
    
    与 daily incremental_update 的区别：
      - 数据窗口更长（行情60日/财务12季/公告30条/研报10篇）
      - 全部标的强制重生成 Agent 段落（第4/5/6章）
      - 在 target_timeline_events 记录 DEEP_ANALYSIS_WEEKLY 事件
    
    注册到 APScheduler: 每周日 22:00
    """
    positions = load_positions()
    results = {
        "total": len(positions),
        "deep_updated": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    for pos in positions:
        code = pos["code"]
        name = pos.get("name", "")
        try:
            # 最长回溯窗口
            ts_code = normalize_ts_code(code, name)
            quotes = load_recent_quotes(ts_code, days=60)
            financials = load_financial_trend(ts_code, quarters=12)
            anns = load_recent_announcements(code, limit=30)
            reports = load_recent_reports(code, limit=10)

            # 重生成 Agent 段落
            updated = update_agent_sections_in_tamf(
                code, name, quotes, financials, anns, reports
            )
            if not updated:
                results["skipped"] += 1
                continue

            # 写回文件
            write_tamf(code, updated)

            # 记录时间线事件
            _record_timeline_event(
                code,
                "DEEP_ANALYSIS_WEEKLY",
                "INFO",
                f"周频深度分析完成 ({today})",
                f"更新 {len(quotes)}日行情 / {len(financials)}季财务 / {len(anns)}条公告 / {len(reports)}篇研报",
            )

            results["deep_updated"] += 1

        except Exception as e:
            results["failed"] += 1
            results["errors"].append(f"{code}: {e}")

    # T4.2: 技能-标的联动 — 深度分析后检查基本面变化触发技能复审
    if results["deep_updated"] > 0:
        try:
            from skill_tamf_linkage import on_fundamental_change_detected
            for pos in positions:
                code = pos["code"]
                on_fundamental_change_detected(code)
        except Exception:
            pass

    return results


def _record_timeline_event(
    ts_code: str,
    event_type: str,
    severity: str,
    title: str,
    description: str = "",
) -> None:
    """将事件写入 memory.target_timeline_events"""
    conn = None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO memory.target_timeline_events
                (ts_code, event_type, severity, title, description)
            VALUES (%s, %s, %s, %s, %s)
        """, (ts_code, event_type, severity, title, description))
        conn.commit()
    except Exception:
        pass  # 不因时线写入失败中断主流程
    finally:
        if conn:
            release_db_conn(conn)


# ══════════════════════════════════════════════════════════════════
# 第7层：事件驱动即时更新（T2.3）
# ══════════════════════════════════════════════════════════════════

def on_transaction_executed(code: str, transaction: dict) -> dict:
    """
    交易执行后即时更新TAMF文件的持仓历程（章节二）。
    由交易执行流程调用。

    Args:
        code: 持仓代码（6位纯数字）
        transaction: {"date", "action", "shares", "price", "amount", "reason"}
    Returns:
        {"status": "updated"|"skipped", "code": code}
    """
    logger = logging.getLogger("tamf_updater")
    content = read_tamf(code)
    if not content:
        logger.debug(f"on_transaction: {code} 无TAMF文件，跳过")
        return {"status": "no_file", "code": code}

    name = ""
    positions = load_positions()
    pos = next((p for p in positions if p["code"] == code), None)
    if pos:
        name = pos.get("name", "")

    # 追加操作历史行
    date_str = transaction.get("date", datetime.now().strftime("%Y-%m-%d"))
    action = transaction.get("action", "UNKNOWN")
    shares = transaction.get("shares", 0)
    price = transaction.get("price", 0)
    amount = transaction.get("amount", 0)
    reason = transaction.get("reason", "")

    action_sign = f"+{shares}" if action in ("BUY", "建仓", "加仓") else f"-{shares}" if action in ("SELL", "减仓", "清仓") else str(shares)
    new_line = f"| {date_str} | {action} | {action_sign} | ¥{price} | ¥{amount:,.0f} | {reason[:30]} | — |"

    # 在操作历史时间线表格中追加新行（在第一个空行或表格结束处）
    import re
    timeline_pattern = re.compile(
        r'(\| 日期 \| 操作 \| 数量 \| 价格 \| 金额 \| 原因摘要 \| 事后评估 \|\n\|[-| ]+\|\n)((?:\|.*\|\n)*)',
        re.DOTALL
    )
    def _append_line(match):
        header = match.group(1)
        existing = match.group(2).rstrip()
        return header + existing + "\n" + new_line + "\n"

    content = timeline_pattern.sub(_append_line, content, count=1)

    # 更新持仓状态表（当前持仓状态）
    if pos:
        status_line = f"| {pos.get('shares', 0):.0f} | ¥{pos.get('avg_cost', 0):.3f} | — | ¥{pos.get('market_value', 0):,.0f} | ¥{pos.get('pnl_amount', 0):,.0f} | {pos.get('pnl_pct', 0):.2f}% | {pos.get('weight_pct', 0):.2f}% |"
        status_pattern = re.compile(
            r'\| 持有数量 \| 平均成本 \| 现价 \| 持仓市值 \| 盈亏金额 \| 盈亏% \| 仓位占比 \|\n\|[-| :]+\|\n\|.*\|',
            re.DOTALL
        )
        content = status_pattern.sub(
            f"| 持有数量 | 平均成本 | 现价 | 持仓市值 | 盈亏金额 | 盈亏% | 仓位占比 |\n|---------|---------|------|---------|---------|------|---------|\n{status_line}",
            content, count=1
        )

    write_tamf(code, content)
    _record_timeline_event(code, f"TRANSACTION_{action}", "INFO",
                           f"交易: {action} {action_sign}股 @ ¥{price}",
                           f"金额: ¥{amount:,.0f} | 原因: {reason[:50]}")

    logger.info(f"on_transaction: {code} 交易已记录到TAMF")
    return {"status": "updated", "code": code}


def on_announcement_detected(code: str, announcement: dict) -> dict:
    """
    公告检测后即时更新TAMF文件的相关章节。
    由公告采集流程调用。

    Args:
        code: 持仓代码（6位纯数字）
        announcement: {"title", "ann_type", "notice_date"}
    Returns:
        {"status": "updated"|"skipped", "code": code, "affected_sections": [...]}
    """
    logger = logging.getLogger("tamf_updater")
    content = read_tamf(code)
    if not content:
        return {"status": "no_file", "code": code}

    ann_type = announcement.get("ann_type", announcement.get("type", ""))
    title = announcement.get("title", "")
    notice_date = announcement.get("notice_date", announcement.get("date", ""))
    affected = []

    # 判断公告类型是否需要更新
    if ann_type in ("分红", "送股", "配股"):
        # 更新章节二：公司行为记录
        detail = announcement.get("detail", "")
        new_line = f"| {notice_date} | {ann_type} | {detail} | 待确认 | ⏳ 待处理 |"
        import re
        corp_pattern = re.compile(
            r'(\| 日期 \| 类型 \| 详情 \| 对成本影响 \| 处理状态 \|\n\|[-| :]+\|\n)((?:\|.*\|\n)*)',
            re.DOTALL
        )
        def _append_corp(match):
            header = match.group(1)
            existing = match.group(2).rstrip()
            return header + existing + "\n" + new_line + "\n"
        content = corp_pattern.sub(_append_corp, content, count=1)
        affected.append("section_2_holdings")

    if ann_type in ("季报", "中报", "年报"):
        # 标记基本面需要重新评估（由每日增量更新完成）
        affected.extend(["section_3_fundamentals", "section_1_basic_profile"])
        _record_timeline_event(code, "FINANCIAL_REPORT", "INFO",
                               f"财报发布: {title[:60]}",
                               f"类型: {ann_type} | 日期: {notice_date}")
        # T4.2: 技能-标的联动 — 季报/年报触发技能复审
        try:
            from skill_tamf_linkage import on_fundamental_change_detected
            on_fundamental_change_detected(code)
        except Exception:
            pass

    # 触发风险公告更新
    risk_keywords = ["风险", "警示", "亏损", "退市", "处罚", "监管"]
    if any(kw in title for kw in risk_keywords):
        affected.append("section_7_monitoring")
        _record_timeline_event(code, "RISK_ANNOUNCEMENT", "WARNING",
                               f"风险公告: {title[:60]}",
                               f"类型: {ann_type}")

    # 常用更新：消息面
    affected.append("section_5_news")
    affected = list(set(affected))

    if affected:
        write_tamf(code, content)
        logger.info(f"on_announcement: {code} 公告已处理，影响章节: {affected}")
    else:
        logger.debug(f"on_announcement: {code} 公告无需即时更新")

    return {"status": "updated" if affected else "skipped", "code": code, "affected_sections": affected}


def on_rating_change(code: str, old_rating: str, new_rating: str,
                     source: str = "") -> dict:
    """
    研报评级变动即时更新TAMF的监控状态和消息面章节。
    由研报采集流程调用。

    Args:
        code: 持仓代码（6位纯数字）
        old_rating: 旧评级 (如 "增持")
        new_rating: 新评级 (如 "中性")
        source: 评级来源机构
    Returns:
        {"status": "updated"|"skipped", "code": code}
    """
    logger = logging.getLogger("tamf_updater")
    content = read_tamf(code)
    if not content:
        return {"status": "no_file", "code": code}

    # 判断方向
    rating_rank = {"买入": 5, "增持": 4, "中性": 3, "减持": 2, "卖出": 1}
    old_score = rating_rank.get(old_rating, 3)
    new_score = rating_rank.get(new_rating, 3)
    direction = "上调" if new_score > old_score else "下调" if new_score < old_score else "维持"

    source_str = f" ({source})" if source else ""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    _record_timeline_event(
        code, "RATING_CHANGE",
        "WARNING" if direction == "下调" else "INFO",
        f"评级变动: {old_rating} → {new_rating}{source_str}",
        f"方向: {direction} | 时间: {now_str}"
    )

    # 更新监控状态（研报评级变动行）
    import re
    mon_pattern = re.compile(
        r'\| 研报评级变动 \| ([^|]*) \|',
        re.DOTALL
    )
    new_mon_line = f"| 研报评级变动 | {direction} ({old_rating}→{new_rating}{source_str}) |"
    if mon_pattern.search(content):
        content = mon_pattern.sub(new_mon_line + " |", content, count=1)

    write_tamf(code, content)
    logger.info(f"on_rating_change: {code} {old_rating}→{new_rating} ({direction})")

    return {"status": "updated", "code": code, "direction": direction}


# ─── 主入口 ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        print("开始批量初始化TAMF文件...")
        r = init_all_tamf_files()
        print(f"完成: 成功{r['success']}个, 失败{r['failed']}个")
        for e in r.get("errors", [])[:5]:
            print(f"  错误: {e}")
    elif len(sys.argv) > 1 and sys.argv[1] == "update":
        print("开始每日增量更新...")
        r = scheduled_update_all_holdings()
        print(f"完成: 更新{r['updated']}个, 跳过{r['skipped']}个, 失败{r['failed']}个")
    elif len(sys.argv) > 1 and sys.argv[1] == "check":
        # 数据覆盖检查脚本
        positions = load_positions()
        print(f"持仓数量: {len(positions)}")
        for pos in positions[:5]:
            code = pos["code"]
            name = pos["name"]
            ts = normalize_ts_code(code, name)
            quotes = load_recent_quotes(ts, 20)
            anns = load_recent_announcements(code, 10)
            fins = load_financial_trend(ts, 8)
            reps = load_recent_reports(code, 5)
            print(f"  {code} {name} → {ts}: 行情{len(quotes)}天, 公告{len(anns)}条, 财务{len(fins)}季, 研报{len(reps)}篇")
    else:
        print("用法: python tamf_updater.py [init|update|check]")


# ─── 第6层：Agent智能段落生成 ─────────────────────────────────

def _call_deepseek(system: str, prompt: str, max_tokens: int = 1500) -> str:
    """强制走 RouterAgent + force_model=deepseek"""
    try:
        from agent_interface import get_agent
        result = get_agent().chat(prompt, system=system, force_model="deepseek")
        return result.get("content", "")[:max_tokens]
    except Exception as e:
        logging.getLogger("tamf_updater").warning(f"DeepSeek调用失败: {e}")
        return ""


def _call_ollama(system: str, prompt: str, max_tokens: int = 800) -> str:
    """走 RouterAgent + force_model=ollama"""
    try:
        from agent_interface import get_agent
        result = get_agent().chat(prompt, system=system, force_model="ollama")
        return result.get("content", "")[:max_tokens]
    except Exception as e:
        logging.getLogger("tamf_updater").warning(f"Ollama调用失败: {e}")
        return ""


SYSTEM_TECHNICAL = """你是一个专业的A股技术面分析师。
规则：
1. 只基于提供的行情数据说话，不猜测
2. 2-3句话，简明扼要
3. 不做任何买卖建议
4. 指出趋势方向和关键价位
5. 数据不足时直接说明"数据不足，无法分析"
"""

SYSTEM_FUNDAMENTAL = """你是一个专业的A股基本面分析师。
规则：
1. 基于财务数据+公告+研报综合评估
2. 3-5句话，覆盖：盈利能力、成长性、财务健康、估值水平
3. 指出最关键的1个正面因素和1个风险点
4. 不做任何买卖建议
5. 数据不足时说明"数据不足"
"""

SYSTEM_NEWS = """你是一个专业的A股消息面分析师。
规则：
1. 综合新闻情感+公告影响+研报一致/分歧
2. 2-3句话，结论明确
3. 指出需警惕的信号（如有）
4. 不做任何买卖建议
5. 数据不足时说明
"""

SYSTEM_REFLECTION = """你是一个擅长复盘反思的A股投资顾问。
规则：
1. 根据历史交易记录和决策结果撰写反思
2. 回答三个问题：
   a) 这个标的最成功的操作是什么？为什么成功？
   b) 最大的教训是什么？
   c) 当前操作与历史模式是否一致？
3. 语气客观、反思性、不给建议
4. 数据不足时说明
"""


def generate_agent_section(
    ts_code: str,
    section_name: str,
    context: dict
) -> str:
    """
    按 section_name 调用对应 Agent 生成 TAMF 智能段落。

    路由规则：
    - technical_assessment / news_assessment → Ollama 本地（简单分析）
    - fundamental_assessment / reflection     → DeepSeek 云端（复杂推理）
    - 数据不足时直接返回提示，不调用 LLM
    """
    code = context.get("code", ts_code)
    name = context.get("name", ts_code)

    if section_name == "technical_assessment":
        if not context.get("quotes"):
            return "```\n⚠️ 数据不足 (market.daily_quotes 无记录)\n```"
        close = context["quotes"][0].get("close", 0)
        ma20 = _calc_ma20(context["quotes"])
        dev = ((close - ma20) / ma20 * 100) if ma20 else 0
        high20 = max((q.get("high") or 0) for q in context["quotes"][:20])
        low20 = min((q.get("low") or 0) for q in context["quotes"][:20])
        prompt = f"""对 {name}（{code}）进行2-3句话技术面简评。

当前价: ¥{close:.3f}
MA20: ¥{ma20:.3f}（偏离度: {dev:+.2f}%）
近20日高点: ¥{high20:.3f}，低点: ¥{low20:.3f}
近{len(context['quotes'])}个交易日数据。

请按格式回答：
趋势方向：[描述]
关键支撑/压力位：[价位]
短线信号：[看多/看空/中性]
"""
        result = _call_ollama(SYSTEM_TECHNICAL, prompt)
        if not result:
            return f"```\n⚠️ 技术面简评（Ollama暂不可用，数据基于{len(context['quotes'])}天）\n趋势方向: {'上升' if dev > 2 else '下降' if dev < -2 else '横盘'}\nMA20偏离度: {dev:+.2f}%\n```"
        return f"```\n{result}\n```"

    elif section_name == "fundamental_assessment":
        fins = context.get("financials", [])
        reports = context.get("reports", [])
        if not fins and not reports:
            return "```\n⚠️ 数据不足 (market.financial_indicators + research.research_reports 均无记录)\n```"
        fin_summary = _summarize_financials(fins) if fins else "财务数据: 暂无"
        report_summary = _summarize_reports(reports) if reports else "研报数据: 暂无"
        prompt = f"""对 {name}（{code}）进行基本面综合评估。

{fin_summary}

{report_summary}

请按以下格式输出3-5句评估：
1. 盈利能力：[评估]
2. 成长性：[评估]
3. 财务健康：[评估]
4. 估值水平：[评估]
5. 关键风险点：[如无则写"暂无明显风险"]
"""
        result = _call_deepseek(SYSTEM_FUNDAMENTAL, prompt)
        if not result:
            return f"```\n⚠️ 基本面评估（DeepSeek暂不可用）\n{fin_summary[:200]}\n```"
        return f"```\n{result}\n```"

    elif section_name == "news_assessment":
        anns = context.get("announcements", [])
        reports = context.get("reports", [])
        if not anns and not reports:
            return "```\n⚠️ 数据不足 (research.announcements + research.research_reports 均无记录)\n```"
        ann_summary = "\n".join([
            f"- [{a.get('date','?')}] {a.get('type','?')}: {a.get('title','')[:50]}"
            for a in anns[:5]
        ]) if anns else "无公告记录"
        report_summary = "\n".join([
            f"- [{r.get('date','?')}] {r.get('source','?')}: {r.get('rating','?')} - {r.get('title','')[:40]}"
            for r in reports[:3]
        ]) if reports else "无研报记录"
        prompt = f"""对 {name}（{code}）进行消息面综合判断。

近期重要公告：
{ann_summary}

近期研报观点：
{report_summary}

请按格式回答（2-3句话）：
整体消息面：[偏正面/偏负面/中性]
需警惕信号：[如有则列出]
研报一致性：[一致/有分歧]
"""
        result = _call_ollama(SYSTEM_NEWS, prompt)
        if not result:
            return f"```\n⚠️ 消息面判断（Ollama暂不可用）\n近{len(anns)}条公告, 近{len(reports)}篇研报\n```"
        return f"```\n{result}\n```"

    elif section_name == "reflection":
        transactions = context.get("transactions", [])
        if not transactions:
            return "```\n⚠️ 暂无足够历史交易数据支持反思笔记\n```"
        tx_summary = "\n".join([
            f"- [{tx.get('date','?')}] {tx.get('action','?')} {tx.get('shares',0):.0f}股@{tx.get('price','?')} 原因:{tx.get('reason','')[:30]}"
            for tx in transactions[:10]
        ])
        prompt = f"""回顾 {name}（{code}）的历史交易，撰写反思笔记：

交易历史：
{tx_summary}

请回答：
1. 这个标的最成功的操作是什么？为什么成功？
2. 最大的教训是什么？
3. 当前操作建议与历史模式是否一致？如果不一致，是进步还是偏离？
"""
        result = _call_deepseek(SYSTEM_REFLECTION, prompt)
        if not result:
            return "```\n⚠️ 反思笔记（DeepSeek暂不可用）\n```"
        return f"```\n{result}\n```"

    return "```\n⚠️ 未知段落类型: {section_name}\n```"


def _calc_ma20(quotes: list[dict]) -> float:
    """计算MA20"""
    prices = [q.get("close") for q in quotes[:20] if q.get("close")]
    return sum(prices) / len(prices) if prices else 0.0


def _summarize_financials(financials: list[dict]) -> str:
    """将财务指标列表格式化为摘要字符串"""
    if not financials:
        return "财务数据: 暂无"
    lines = []
    for f in financials[:4]:
        date = f.get("date", "?")
        rev = f.get("revenue")
        profit = f.get("profit")
        roe = f.get("roe")
        gm = f.get("gross_margin")
        lines.append(
            f"Q{date}: 营收{'¥{:.1f}亿'.format(rev/1e8) if rev else '—'}, "
            f"净利润{'¥{:.1f}亿'.format(profit/1e8) if profit else '—'}, "
            f"ROE{roe:.1f}%" if roe else "ROE—", f"毛利率{gm:.1f}%" if gm else "毛利率—"
        )
    return "财务数据（近季）：\n" + "\n".join(lines)


def _summarize_reports(reports: list[dict]) -> str:
    """将研报列表格式化为摘要字符串"""
    if not reports:
        return "研报数据: 暂无"
    lines = []
    for r in reports[:3]:
        date = r.get("date", "?")
        source = r.get("source", "?")
        rating = r.get("rating", "?")
        title = r.get("title", "?")[:30]
        lines.append(f"- [{date}] {source} {rating}: {title}")
    return "研报数据：\n" + "\n".join(lines)


def _build_agent_context(code: str, name: str,
                           quotes: list, financials: list,
                           anns: list, reports: list) -> dict:
    """为 generate_agent_section 构建上下文字典"""
    return {
        "code": code,
        "name": name,
        "quotes": quotes,
        "financials": financials,
        "announcements": anns,
        "reports": reports,
        "transactions": [],
    }


def update_agent_sections_in_tamf(code: str, name: str,
                                    quotes: list, financials: list,
                                    anns: list, reports: list) -> str:
    """
    读取现有TAMF文件，仅更新Agent生成段落（四/五/六章）。
    返回更新后的完整文件内容。
    """
    content = read_tamf(code)
    if not content:
        return ""

    ctx = _build_agent_context(code, name, quotes, financials, anns, reports)

    # 更新第四章技术面
    tech_section = generate_agent_section(code, "technical_assessment", ctx)
    # 更新第三章基本面（部分）
    fund_section = generate_agent_section(code, "fundamental_assessment", ctx)
    # 更新第五章消息面
    news_section = generate_agent_section(code, "news_assessment", ctx)

    # 正则定位并替换各Agent段落（已手动编辑的章节跳过）
    import re

    # 技术面Agent段落（手动编辑保护）
    if not is_section_manually_edited(content, "### Agent 技术面简评"):
        tech_pattern = re.compile(
            r'(### Agent 技术面简评\n)\n```\n.*?\n```',
            re.DOTALL
        )
        content = tech_pattern.sub(f'\n### Agent 技术面简评\n{tech_section}', content, count=1)
    else:
        import logging
        logging.getLogger("tamf_updater").debug(f"{code} 技术面章节已手动编辑，跳过覆盖")

    # 基本面Agent段落（手动编辑保护）
    if not is_section_manually_edited(content, "### Agent 基本面综合评估"):
        fund_pattern = re.compile(
            r'(### Agent 基本面综合评估\n)\n```\n.*?\n```',
            re.DOTALL
        )
        content = fund_pattern.sub(f'\n### Agent 基本面综合评估\n{fund_section}', content, count=1)
    else:
        import logging
        logging.getLogger("tamf_updater").debug(f"{code} 基本面章节已手动编辑，跳过覆盖")

    # 消息面Agent段落（手动编辑保护）
    if not is_section_manually_edited(content, "### Agent 消息面综合判断"):
        news_pattern = re.compile(
            r'(### Agent 消息面综合判断\n)\n```\n.*?\n```',
            re.DOTALL
        )
        content = news_pattern.sub(f'\n### Agent 消息面综合判断\n{news_section}', content, count=1)
    else:
        import logging
        logging.getLogger("tamf_updater").debug(f"{code} 消息面章节已手动编辑，跳过覆盖")

    return content
