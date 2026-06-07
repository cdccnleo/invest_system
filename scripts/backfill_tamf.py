"""
TAMF 文件批量回填脚本
为所有持仓标的初始化 TAMF 记忆文件

每个标的生成 8 章节结构：
1. 基础信息 (ts_code, name, industry, listing_date)
2. 持仓现状 (shares, avg_cost, market_value, weight)
3. 财务概览 (roe, pe_ttm, pb, revenue_yoy, profit_yoy)
4. 技术位置 (current_price, support/resistance, trend)
5. 竞争优势 (护城河, 行业地位)
6. 投资研判 (current annotation from db, news summary)
7. 风险因素 (政策, 行业, 公司)
8. 跟踪记录 (建档日期, 更新历史)
"""

import sys
from pathlib import Path
from datetime import date

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from pgcrypto_migration import load_positions_from_db
from storage_factory import get_storage

TAMF_DIR = Path("~/invest_system/data/target_memories").expanduser()
TAMF_DIR.mkdir(parents=True, exist_ok=True)

def get_stock_info(ts_code: str) -> dict:
    """从数据库获取股票基本信息"""
    try:
        storage = get_storage()
        conn = storage._pg_conn
        cur = conn.cursor()
        
        # Get stock basic info
        cur.execute("""
            SELECT ts_code, name, industry, list_date
            FROM market.stock_info
            WHERE ts_code = %s OR ts_code LIKE %s
            LIMIT 1
        """, (ts_code, f"{ts_code}%"))
        row = cur.fetchone()
        
        if row:
            info = {
                "ts_code": row[0],
                "name": row[1] or "",
                "industry": row[2] or "",
                "list_date": str(row[3]) if row[3] else "",
            }
        else:
            info = {"ts_code": ts_code, "name": "", "industry": "", "list_date": ""}
        
        # Get latest financial data
        cur.execute("""
            SELECT close_price FROM market.daily_quotes
            WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 1
        """, (info["ts_code"],))
        price_row = cur.fetchone()
        info["current_price"] = float(price_row[0]) if price_row else 0.0
        
        cur.close()
        storage.close()
        return info
    except Exception as e:
        import logging
        logging.getLogger("tamf_backfill").warning(f"获取 {ts_code} 信息失败: {e}")
        return {"ts_code": ts_code, "name": "", "industry": "", "list_date": "", "current_price": 0.0}

def generate_tamf_content(ts_code: str, name: str, position: dict) -> str:
    """生成完整的 TAMF Markdown 内容"""
    today = date.today().isoformat()
    shares = position.get("shares", 0)
    avg_cost = position.get("avg_cost", position.get("cost", 0))
    market_value = position.get("market_value", 0)
    
    return f"""# {name} ({ts_code}) — TAMF 记忆档案

> 建档日期: {today}  
> 数据来源: InvestPilot 自动初始化

---

## 1. 基础信息

| 字段 | 值 |
|------|-----|
| ts_code | {ts_code} |
| 名称 | {name} |
| 行业 | {position.get('industry', 'N/A')} |
| 上市日期 | {position.get('list_date', 'N/A')} |

---

## 2. 持仓现状

| 字段 | 值 |
|------|-----|
| 持仓份额 | {shares:,.2f} |
| 平均成本 | ¥{avg_cost:.4f} |
| 当前市值 | ¥{market_value:,.2f} |
| 仓位权重 | {position.get('weight', 0):.2f}% |

> 初始化回填 — 实际数据请以 Dashboard 为准

---

## 3. 财务概览

*待从 iFinD 或财报接口填充*

| 指标 | 值 |
|------|-----|
| ROE (TTM) | - |
| PE (TTM) | - |
| PB | - |
| 营收同比 | - |
| 净利润同比 | - |

---

## 4. 技术位置

*待从行情数据填充*

| 字段 | 值 |
|------|-----|
| 当前价 | ¥{position.get('current_price', 0):.4f} |
| 支撑位 | - |
| 阻力位 | - |
| 趋势 | - |

---

## 5. 竞争优势

*待手动填写*

- 护城河：
- 行业地位：
- 核心看点：

---

## 6. 投资研判

*待从最新研报摘要自动更新（见 schedule_runner job_tamf_update 16:05）*

当前立场：

---

## 7. 风险因素

*待手动填写*

- 政策风险：
- 行业风险：
- 公司风险：

---

## 8. 跟踪记录

| 日期 | 事件 |
|------|------|
| {today} | TAMF 档案初始化 |
"""

def backfill_tamf() -> dict:
    """为所有持仓标的生成 TAMF 文件"""
    positions = load_positions_from_db()
    
    results = {"created": 0, "skipped": 0, "errors": 0}
    
    for pos in positions:
        ts_code = pos.get("code") or pos.get("ts_code", "")
        name = pos.get("name", ts_code)
        
        if not ts_code:
            results["skipped"] += 1
            continue
        
        try:
            # Get full stock info
            info = get_stock_info(ts_code)
            
            # Merge position data with info
            pos_data = {**info, **pos}
            
            # Generate TAMF content
            content = generate_tamf_content(ts_code, name, pos_data)
            
            # Write file: {TAMF_DIR}/{ts_code}.md
            safe_name = ts_code.replace(".", "_")
            filepath = TAMF_DIR / f"{safe_name}.md"
            filepath.write_text(content, encoding="utf-8")
            
            results["created"] += 1
        except Exception as e:
            results["errors"] += 1
            import logging
            logging.getLogger("tamf_backfill").error(f"生成 {ts_code} TAMF 失败: {e}")
    
    return results

if __name__ == "__main__":
    result = backfill_tamf()
    print(f"TAMF 回填完成: 新建 {result['created']} 个, 跳过 {result['skipped']} 个, 错误 {result['errors']} 个")
    print(f"文件目录: {TAMF_DIR}")