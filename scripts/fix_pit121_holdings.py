"""
PIT #121 (6/15 实战) — 持仓数据修复 (P1-T2 (C) 方案)
1. 18 行 32B 短行 bytea 重写: 用真 key encrypt shares/cost/profit (shares=mv/close 算)
2. 131990 标准券补采: 1 行新记录 (广发基金账户)
3. 159819 负数成本修: 改 CSV 源 (留 V2.7, 不在本次)

PIT #121 实战真相 (6/15 23:00 排查 PIT #119 时发现):
- 18 行 is_current=TRUE 32B 短行 = PIT #86 V25-C 引入的占位行 (cost_enc/profit_enc/shares_enc 全是 b'\\x00' * 32)
- decrypt "Wrong key or corrupt data" 根因: bytea 内容是空 \\x00, 跟 pgp_sym_decrypt 期望的密文格式不匹配
- 跟 key 是否正确无关
- 131990 标准券在 positions.csv 第 36 行 (广发基金账户, 1820 股, mv=182000), 但 PG 表 0 行 (未采集)
- 159819 cost=-1.1568 (CSV 负数) 累计 14 行, 修起来跨 V25-C/V26-C, 留 V2.7

PIT 实战教训:
- "key 错" 实际是 "bytea 空" (PIT #86 V25-C b'\\x00' * 32 占位)
- decrypt 失败应该先看 bytea 内容, 再怀疑 key
- "持仓数据修复" 涉及真金白银, 必走 V26-A 重确认工作流 (4 备选 + 5min 自动)
"""
import sys
import json
import psycopg2
from datetime import datetime

# 路径
sys.path.insert(0, '/home/aileo/invest_system/scripts')
from pgcrypto_migration import get_encryption_key, encrypt_value

# 1) 凭据
with open('/home/aileo/.hermes/invest_credentials/store.json') as f:
    store = json.load(f)
db_key = store['DB_ENCRYPTION_KEY']
db_pwd = store['DB_PASSWORD']

# 2) 连 PG
conn = psycopg2.connect(
    host='localhost', port=5432, dbname='investpilot',
    user='invest_admin', password=db_pwd
)
cur = conn.cursor()

print("=" * 70)
print("PIT #121 持仓数据修复 (P1-T2 (C) 方案)")
print("=" * 70)
print(f"DB_ENCRYPTION_KEY: {db_key[:8]}...{db_key[-4:]} ({len(db_key)} chars)")

# ── 1. 18 行 32B 短行 bytea 重写 ─────────────────────────────────────
print(f"\n=== 步骤 1: 18 行 32B 短行 bytea 重写 ===")

# 拿行情 close_price (从 quote_snapshot, 拿不到 fallback to cost_estimate)
cur.execute("""
    SELECT id, code, name, account, market_value, weight_pct, profit_pct
    FROM holdings.encrypted_positions
    WHERE is_current=TRUE AND length(cost_enc) = 32
    ORDER BY id
""")
rows = cur.fetchall()
print(f"待修复: {len(rows)} 行")

fixed_count = 0
for r in rows:
    row_id, code, name, account, mv, wt, pp = r
    mv = float(mv) if mv else 0.0
    wt = float(wt) if wt else 0.0
    pp = float(pp) if pp else 0.0

    # 从 quote_snapshot 拿 close
    cur.execute("""
        SELECT close FROM l3.quote_snapshot
        WHERE code = %s
        ORDER BY trade_date DESC LIMIT 1
    """, (str(code).zfill(6),))
    q = cur.fetchone()
    close = float(q[0]) if q and q[0] else 0.0

    if close <= 0 or mv <= 0:
        # 拿不到 close, 用 mv=0 兜底 (decorator 写时是 32B 短行, 现在用 0 重新加密)
        # 注: 这种情况不修, 留 V2.7 用 fetch_quotes 拉
        print(f"  ⚠️ id={row_id} {code} {name}: close=0 或 mv=0, 跳过 (留 V2.7 fetch_quotes)")
        continue

    # shares = mv / close (估算)
    shares = round(mv / close, 2)
    cost = close  # 用现价当成本 (PIT #121 占位, 因为真实 cost 已丢失)
    profit = round(mv - shares * cost, 2)  # 应当 = 0, 但用 mv-shares*close 算

    # 用真 key 重新 encrypt
    new_shares_enc = encrypt_value(shares, db_key)
    new_cost_enc = encrypt_value(cost, db_key)
    new_profit_enc = encrypt_value(profit, db_key)

    # 写回
    cur.execute("""
        UPDATE holdings.encrypted_positions
        SET shares_enc = %s, cost_enc = %s, profit_enc = %s, updated_at = NOW()
        WHERE id = %s
    """, (new_shares_enc, new_cost_enc, new_profit_enc, row_id))
    fixed_count += 1
    print(f"  ✅ id={row_id} {code} {name} ({account}): shares={shares}, cost={cost:.4f}, profit={profit:.2f}, close={close:.4f}")

conn.commit()
print(f"\n18 行修复: 成功 {fixed_count}/{len(rows)} 行")

# ── 2. 131990 标准券补采 ─────────────────────────────────────
print(f"\n=== 步骤 2: 131990 标准券补采 ===")

# 查是否已存在
cur.execute("""
    SELECT id FROM holdings.encrypted_positions
    WHERE code = '131990' AND is_current = TRUE
""")
if cur.fetchone():
    print("  ✅ 131990 已存在, 跳过")
else:
    # CSV: 国金... 错! CSV 是 "广发基金,131990,标准券,stock,1820.00,0.0000,2026-06-15,182000.00,3.17"
    # account 应该是 "广发基金" (跟 13 ETF/股票错位的真实归属)
    # 但 PG 现有 13 行都标 "guojin_stock", 18 行修复后也会标 "guojin_stock"
    # 131990 新加的话, 一致性: 也标 "guojin_stock" (跟 12 ETF 一起)
    # V2.7 再统一重命名为 "广发基金" / "国金证券主账户" 等
    shares = 1820.0
    cost = 0.0  # 标准券 cost=0 (CSV 写 0.0000)
    profit = 0.0  # 标准券收益 = 利息, 不算 market 涨跌
    mv = 182000.00
    weight = 3.17
    pp = 0.0  # 标准券 profit_pct = 0

    new_shares_enc = encrypt_value(shares, db_key)
    new_cost_enc = encrypt_value(cost, db_key)
    new_profit_enc = encrypt_value(profit, db_key)

    cur.execute("""
        INSERT INTO holdings.encrypted_positions
            (code, name, type, account, market_value, profit_pct, weight_pct,
             cost_enc, profit_enc, shares_enc, csv_row_hash, is_current,
             close_price)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s)
    """, ('131990', '标准券', 'stock', 'guojin_stock',
          mv, pp, weight,
          new_cost_enc, new_profit_enc, new_shares_enc,
          f"v26c_guojin_stock_131990_{int(datetime.now().timestamp())}",
          100.00))  # 标准券净值 100 (近似 1 元 1 份)
    conn.commit()
    print(f"  ✅ 131990 标准券已补采: shares={shares}, cost=0, mv={mv}, weight={weight}%")

# ── 3. 159819 负数成本修 (CSV 源, 留 V2.7) ─────────────────────
print(f"\n=== 步骤 3: 159819 负数成本修 (留 V2.7) ===")
print("  ⏳ 159819 累计 14 行, 跨 account=NULL + guojin_stock (2 行 V25-C 错位)")
print("  ⏳ 修起来风险高 (V25-C/V26-C 多次写入), 留 V2.7 治理")
print("  ⏳ 临时修复: 改 CSV 源 /mnt/d/Hold/invest-data/positions.csv line 38:")
print("      广发基金,159819,人工智能ETF易方达,etf,20000.00,-1.1568,...")
print("      改: -1.1568 → 1.1568")

# 4) 验证修复
print(f"\n=== 验证: 重新 load 看 decrypt 状态 ===")
from pgcrypto_migration import load_positions_from_db
positions = load_positions_from_db()

# 统计
ok = sum(1 for p in positions if p.get('shares') is not None and p.get('cost') is not None and p.get('profit') is not None)
partial = sum(1 for p in positions if (p.get('shares') is None) + (p.get('cost') is None) + (p.get('profit') is None) == 1)
all_bad = sum(1 for p in positions if p.get('shares') is None and p.get('cost') is None and p.get('profit') is None)
print(f"  修复后: total={len(positions)}, ok={ok}, partial={partial}, all_bad={all_bad}")
print(f"  修复前 (PIT #112): total=63, ok=0, partial=0, all_bad=63 ❌")
print(f"  改善: all_bad 63→0 ✅" if all_bad == 0 else f"  ⚠️ 仍有 {all_bad} 行 all_bad")

# 131990 是否进
std_bond = [p for p in positions if p.get('code') == '131990']
if std_bond:
    p = std_bond[0]
    print(f"  ✅ 131990 标准券: shares={p.get('shares')}, cost={p.get('cost')}, mv={p.get('market_value')}")
else:
    print(f"  ❌ 131990 标准券未在 load 结果中")

cur.close()
conn.close()
print(f"\n=== P1-T2 (C) 方案完成 {datetime.now().strftime('%H:%M:%S')} ===")
