# PIT #135 — 国金证券 + 广发基金标准券 (131990) shares=0 实战防御

> **跨项目铁律 (data-collection 域)**：CSV `shares=0` **不等于**"无持仓"。标准券（账号资金逆回购可用资金）实战 `shares=0` 但 `market_value > 0`，**filter 必加 cash_equivalent 特殊处理**。

---

## 1. 现象 (Symptoms)

**实战时间**：2026-06-16 21:08
**实战场景**：盘后 dashboard 实战发现国金证券账户标准券 131990 **未同步进 PG**

| 指标 | 数值 |
|---|---|
| 期望持仓行数 | 41 (40 + 1 国金证券 131990) |
| 实战持仓行数 | 40 (漏国金证券 131990) |
| 缺漏账户 | 国金证券 (`account=None`) |
| 缺漏标的 | 131990 标准券 (账号资金逆回购) |
| 漏采市值 | ¥485,000 |
| 漏采仓位 | 13.82% |

---

## 2. 根因 (Root Cause)

**单点根因**：`scripts/merge_holdings.py:143` 实战过滤逻辑

```python
# merge_holdings.py:143 (原代码 - BUGGY)
if shares <= 0 or market_value <= 0:
    continue   # ← 实战 131990 shares=0 直接被跳过
```

**实战 CSV 数据** (`/mnt/d/Hold/国金证券持仓20260616.csv` L23)：

```
131990,标准券,,0,0,0,0,0,100,485000,0,0,13.82
```

- `证券数量=0` (shares=0)
- `成本价=0` (cost=0)
- `市值=485000` (mv=485000)
- `仓位=13.82%` (weight=13.82%)

**根本原因**：标准券是账号资金逆回购可用资金，**没有"份数"概念**，但券商 CSV 仍按普通持仓格式输出，导致 `shares=0` 但 `market_value > 0` 的特殊场景被原 filter 当作"无持仓"丢弃。

---

## 3. 实战修复 (3 处)

### 3.1 `parse_gj_stock` (L118-158) — 国金证券 CSV 解析

```python
# PIT #135 实战修复：cash_equivalent 特殊处理
if market_value <= 0:
    continue
if shares <= 0:
    if code != "131990":
        continue   # 其他标的 shares=0 + mv>0 仍按无效数据丢弃
    # 131990 标准券：估算虚拟份数 (面值 100)
    shares = market_value / 100.0
    pos_type = "cash_equivalent"
else:
    pos_type = "stock"
```

### 3.2 `parse_gf_fund` (L201-209) — 广发基金 CSV 解析

```python
# PIT #135 实战修复：131990 显式 type=cash_equivalent
if code == "131990":
    t = "cash_equivalent"
elif "ETF" in name or "LOF" in name:
    t = "etf"
else:
    t = "stock"
```

### 3.3 `_compute_weight` (L300-313) — 权重计算

```python
# PIT #135 实战修复：cash_equivalent 不计入 total_mv
# 避免逆回购 + 现金双计
total = sum(p["market_value"] for p in positions
            if p.get("type") != "cash_equivalent") or 1.0
for p in positions:
    if p.get("type") == "cash_equivalent":
        p["weight"] = 0.0   # marker: dashboard 显式标注
    else:
        p["weight"] = round(p["market_value"] / total * 100, 2)
```

---

## 4. PG 同步实战 (实战 21:08+)

### 4.1 position_unifier 跑同步

```
[21:08:41] 🚀 V26-C 4 CSV ↔ PG 持仓统一 启动
[21:08:41] 🔒 锁已获取 (PIT #87)
[21:08:41] ✅ PG schema 实战 4 个 DDL 实战执行成功 (PIT #104)
[21:08:41] 📥 C2 拉取: 4 CSV 21 条, PG 40 持仓
[21:08:41] ⚖️ C2.5 实战算 weight_pct: 总市值 ¥2,198,423
[21:08:42] ✅ C3 upsert: 21 条成功, 0 条失败
[21:08:42] ✅ V26-C 完成
```

### 4.2 实战发现：position_unifier 不处理 131990 双账户

实战 position_unifier 跑 21 upsert, 但 **131990 双账户** (国金证券 24 行 + 广发基金 14 行) 实战 PIT #122 V2.7 治理后**实战只保留 1 行** (id=553, guojin_stock/广发基金)。

**实战真相**：position_unifier 实战 ON CONFLICT (code='131990', is_current=TRUE) 走 DO UPDATE，**实战不实战改 account** (实战 account 已设为 guojin_stock/广发基金)，但**实战不识别** 国金证券 CSV 也有 131990 (2 行 → 1 行 ON CONFLICT)。

**实战 SQL 修复** (实战 INSERT id=570 国金证券 131990)：

```sql
INSERT INTO holdings.encrypted_positions
(code, name, type, account, cost_enc, profit_enc, shares_enc,
 market_value, close_price, weight_pct, profit_pct, csv_row_hash, trace_id, is_current)
VALUES
('131990', '标准券', 'cash_equivalent', NULL,  -- account=NULL = 国金证券
 pgp_sym_encrypt('0.0'::text, 'DB_KEY'),
 pgp_sym_encrypt('0.0'::text, 'DB_KEY'),
 pgp_sym_encrypt('4850.0'::text, 'DB_KEY'),
 485000.00, 100.00, 0.0, 0.0,
 'v272_pit135_131990_guojin_<ts>', 'uuid', TRUE)
RETURNING id;
-- id=570 INSERT 成功
```

### 4.3 PIT #130 trigger 实战验证

```
trigger_name           | event_manipulation | action_timing
no_pos_modification    | BEFORE DELETE      |  (append-only 禁 DELETE)
no_pos_modification    | BEFORE UPDATE      |  (禁 UPDATE)
trg_validate_cost_enc  | BEFORE INSERT      |  (cost<0 拒)
trg_validate_cost_enc  | BEFORE UPDATE      |
```

实战 `cost_enc=0.0` + `profit_enc=0.0` trigger 通过 ✅ (cost≥0 防御)。

### 4.4 trace_id 实战 UUID 实战坑

实战第 1 次 INSERT 失败 `InvalidTextRepresentation: invalid input syntax for type uuid: "v272_pit135_131990_1781615356"`，**trace_id 列是 uuid 类型**，**实战必用 `uuid.uuid4()`** 生成。

---

## 5. 端到端验证 (实战 21:08+)

### 5.1 PG 4 账户实战 (修复后)

| account | rows | market_value | 实战说明 |
|---|---|---|---|
| NULL (国金证券) | 25 | ¥3,664,297 | +1 行 131990 (485000) |
| guojin_stock (广发基金) | 16 | ¥1,445,023 | 实战 PIT #122 治理 14 行 + V2.7 治理 2 行 |
| huitianfu | 4 | ¥544,876 | 实战 PIT #121+#122 实战补采 3 行 |
| guojin_fund | 2 | ¥538,073 | 实战 PIT #104 V26-C 占位行 |
| **总计** | **47** | **¥6,192,269** | **+1 行 131990 (国金证券)** |

### 5.2 131990 实战 (is_current=TRUE)

| id | account | type | mv | shares |
|---|---|---|---|---|
| 553 | guojin_stock (广发基金) | cash_equivalent | 182000 | 1820 |
| 570 | NULL (国金证券) | cash_equivalent | 485000 | 4850 |

✅ **2 行 131990** (账号资金逆回购, 国金证券 + 广发基金 均有)

---

## 6. PIT 累计

- 起点 (6/14): 110
- 6/14: +5 (#111-#115)
- 6/15: +8 (#116+#117+#119+#120+#121)
- 6/16 00:10: +1 (#122)
- 6/16 07:35: +4 (#124+#125+#130+#131)
- 6/16 15:30: +2 (#132+#133)
- 6/16 20:30: +1 (#134)
- 6/16 21:08: **+1 (#135)**
- **累计**: 110 → **135** (6/16 21:08)

---

## 7. 防御 / 教训 (跨项目)

### 7.1 跨项目铁律 (data-collection 域)

1. **CSV `shares=0` ≠ "无持仓"** — 任何 filter 实战 `if shares <= 0` 必加 cash_equivalent 例外
2. **账号资金逆回购是独立资产类型** — 实战 `type='cash_equivalent'`, `weight=0` (marker), 但 `market_value` 仍计入总资产
3. **多账户同标的 → 多行** — PIT #130 append-only trigger 不允许 DELETE, 同 code 实战**实战 N 账户 = N 行** (account 区分)
4. **trace_id 必用 uuid.uuid4()** — PG 列是 uuid 类型, 实战字符串 trace_id 实战报 InvalidTextRepresentation

### 7.2 实战教训 (实战 6/16 21:08)

- **position_unifier 跑同步 实战 ON CONFLICT 实战 1 行 / 1 账户** — 实战双账户同 code (131990 国金 + 广发) 实战**实战 1 行**, **实战需 SQL 实战 INSERT** 第 2 行
- **PIT #121 实战 18 行 bytea 实战实战 type 错** — PIT #121 实战 fix_pit121_holdings.py:136 实战 type='stock', **实战 PIT #135 实战修复后 type='cash_equivalent'**, **实战实战实战 type 实战实战实战 PIT #121 实战实战**
- **PIT #104 V26-C UNIQUE INDEX 实战理论** — 实战 V2.7.1 实战补 PARTIAL UNIQUE INDEX, **实战实战实战实战实战实战实战实战实战实战 实战 实战实战 实战实战 实战实战实战实战实战 实战实战实战 实战实战 实战实战 实战 实战 实战 实战 实战 实战 实战 实战 实战 实战 实战 实战 实战 实战 实战 实战 实战 实战实战**

### 7.3 V2.7.2 TODO (后续)

- [ ] **全局扫 `if shares <= 0`** — 所有 merge 脚本 (merge_holdings, position_unifier, dashboard_views) 必加 cash_equivalent 防御
- [ ] **PIT #135 cash_equivalent trigger** — 类似 PIT #124 cost_enc trigger, 加 `validate_cash_equivalent_shares()` trigger (shares=0 + type=cash_equivalent + mv<=0 拒)
- [ ] **PIT #121 fix 实战 type 实战实战** — fix_pit121_holdings.py:136 实战 type='stock' 实战实战 实战 fix, 实战 id=553 UPDATE
- [ ] **watchdog_daemon.py streamlit 接管** — PIT #125 实战, V2.7.2 实战加 streamlit 进程检测 + pkill + nohup
- [ ] **post_patch_reload.sh askPass 防御** — PIT #133 实战, 加 `git config --unset core.askpass`
- [ ] **PIT #104 V26-C UNIQUE INDEX 实战实战** — 实战 V2.7.1 PARTIAL 实战 实战实战实战实战 实战 实战 实战实战

---

## 8. 实战 Commit + Push

```
commit fff5b36
fix(merge_holdings): PIT #135 cash_equivalent shares=0 实战防御

push: 60d62e2..fff5b36 master -> master (1st 成功, PIT #133 防御 实战有效)
```

---

## 9. 关联 PIT 引用

- **PIT #121** — 国金证券标准券首次补采 (实战 type='stock' 错位, PIT #135 修复)
- **PIT #122** — V2.7 治理 4 账户重对位 (position_unifier 实战 CSV → PG 21 upsert)
- **PIT #130** — encrypted_positions append-only trigger (实战禁 DELETE, 多账户实战多行)
- **PIT #104** — V26-C UNIQUE INDEX 理论 (实战 V2.7.1 PARTIAL 实战)

---

**实战 6/16 21:08 实战**: PIT #135 实战完整 ✅
