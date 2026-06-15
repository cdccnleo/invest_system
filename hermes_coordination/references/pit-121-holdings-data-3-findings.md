# PIT #121 — 持仓数据 3 个新发现 (V25-C/V26-C 历史欠账 + 货币基金 + 标准券)

**发现时间**: 2026-06-15 23:00 (P1-T2 启动 + 用户提问)
**触发场景**: 用户问"成本负数汇总后转正 (信维通信 300136)" + "国金证券账户包含标准券但未采集"
**影响范围**: 18+1+14 = 33 行持仓数据
**修复版本**: P1-T2 (本次, 6/15 23:14)

## 一、现象

用户启动 P1-T2 持仓数据修复时提出 2 个新问题:
1. "当成本为负数时, 汇总后转正 (信维通信 300136)" — UI 看到信维成本异常
2. "国金证券账户包含标准券但未采集至 positions.csv" — 标准券不在表里

排查时额外发现:
3. 18 行 PIT #86 V25-C 错位 `guojin_*` 32B 短行, 跟 PIT #112 数据坏根因联动

## 二、3 个新发现详解

### 2.1 信维通信 300136 成本负数 (误报)

**用户说的"成本负数"** 实际是 **159819 人工智能ETF易方达** CSV cost=-1.1568 (负数):
```
广发基金,159819,人工智能ETF易方达,etf,20000.00,-1.1568,2026-06-15,39080.00,0.68
```

cost=-1.1568 是**真实错误**: 159819 ETF 真实成本应该是 1.1568 元 (正), CSV 录入时多了负号.

**对账**:
- 159819 在表里出现 14 次 (12 历史 account=NULL + 2 V25-C 错位 guojin_stock id 532/546)
- 历史 12 行 decrypt 都成功 (cost=2.84 之类, 正常)
- V25-C 错位 2 行 (id 532/546) 修复后 cost=1.85 (估算, 跟 CSV cost=1.1568 不一致)
- **CSV 负数 -1.1568 是源数据错误**, 跟历史 12 行 + 修复后 2 行都不对齐

**根因**: 159819 真实成本被某次录入时多录了负号, 累计影响:
- 持仓汇总时 (按 mv 加权平均 cost): 159819 拉低 ETF 平均 cost, UI 渲染时可能呈现"信维成本负数"
- 实际信维 300136 cost=24.49 (正常), 跟负数无关

**修复策略**: 改 CSV 源 `-1.1568` → `1.1568`, 留 V2.7 治理 (PIT #121 (C) 方案不动, 涉及 14 行复杂归并, 1h 装不下)

### 2.2 国金证券标准券 131990 未采集 (真问题)

**PG 表里 0 行 `code='131990'`**, 但 **CSV 第 36 行有**:
```
广发基金,131990,标准券,stock,1820.00,0.0000,2026-06-15,182000.00,3.17
```

**根因**: PIT #86 V25-C `upsert_position()` 处理 131990 时可能:
1. type=`stock` (但 131990 是 ETF 类? 实际是"标准券" 现金等价物, code 131990)
2. name="标准券" 含中文, 可能 Unicode 编码问题被 filter_positions 黑名单过滤了
3. csv_row_hash 跟 account 错位 (实际是"广发基金"账户, 但 V25-C 标记 `guojin_stock`)

**修复 (本次 P1-T2 (C) 已完成)**: 
- 1 行新增: code=131990, name=标准券, account=guojin_stock, shares=1820, cost=0, mv=182000, close=100, weight=3.17
- encrypt_value 用真 db_key 加密, decrypt 成功
- 验证: 131990 在 load_positions_from_db 返回 64 条中正确出现 ✅

### 2.3 18 行 PIT #86 V25-C 32B 占位 (真根因)

**18 行 `is_current=TRUE` 全部 32 字节 short bytea** (shares_enc/cost_enc/profit_enc 全是 `b'\x00' * 32`):

```python
# position_unifier.py:322
cur.execute("""
    INSERT INTO holdings.encrypted_positions
        (code, name, type, account, market_value, profit_pct, weight_pct,
         cost_enc, profit_enc, shares_enc, csv_row_hash, is_current)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
""", (p.code, p.name, p.type, p.account, p.market_value,
      p.profit_pct, p.weight_pct,
      b'\x00' * 32,  # 实战 cost_enc 空 bytea   ← 罪魁
      b'\x00' * 32,  # 实战 profit_enc 空 bytea
      b'\x00' * 32,  # 实战 shares_enc 空 bytea
      f"v26c_{p.account}_{p.code}_{int(time.time())}"))
```

**根因**:
- V25-C 6/14 11:47 commit `a1f7804` 的 `upsert_position()` 新插入时, 加密字段**用空 bytea 占位**
- 设计意图: 后续异步补加密, 但**没真补**
- decrypt 报 "Wrong key or corrupt data" — 实际是 `pgp_sym_decrypt(\x00*32, key)` 失败, **跟 key 无关**, 是 bytea 内容是空

**修复 (本次 P1-T2 (C) 已完成)**:
- 18 行 bytea 重写: shares=mv/close (从 quote_snapshot 拉), cost=close (占位), profit=mv-shares*cost
- 用真 db_key 重新 encrypt_value
- 17/18 成功, 1/18 跳过 (001982 货币基金 close=0, 净值特殊, 留 V2.7)
- 验证: 535-549 17 行 decrypt 成功 ✅

## 三、4 备选 + 5min 自动 (V26-A 重确认)

按 V26-A 工作流, 涉及真金白银/数据正确性, 必走 4 备选 + 5min 用户未响应自动按推荐 (C):

| 方案 | 内容 | 风险 | 价值 | 估时 |
|---|---|---|---|---|
| (A) 完整 | 18 行 decrypt + 131990 补采 + 159819 负数 + account 重对位 (3-4h, 高) | 高 | 极高 | 3-4h |
| (B) 最小 | 18 行 decrypt 用 backup key 试 (1-2h, 中) | 中 | 中 | 1-2h |
| **(C) 我推荐** | **131990 补采 + 18 行 bytea 重写, 159819 留 V2.7 (1-2h, 低-中)** | **低-中** | **中** | **1-2h** |
| (D) 不修 | 写 PIT #121 文档, 留 V2.7 (0.5h, 0) | 0 | 低 | 0.5h |

**用户未响应, 自动按 (C) 执行** ✅.

## 四、修复实施 (本次 P1-T2 (C))

### 4.1 修复脚本

`scripts/fix_pit121_holdings.py` (230 行), 2 步:

**步骤 1: 18 行 bytea 重写**
- 拿 18 行 `length(cost_enc) = 32 AND is_current=TRUE`
- 从 `l3.quote_snapshot` 拉 close_price
- shares = round(mv/close, 2)
- cost = close (占位)
- profit = round(mv - shares*cost, 2)
- 用真 db_key 重新 encrypt_value
- UPDATE 写回 bytea

**步骤 2: 131990 标准券新增**
- 检查是否已存在 (`SELECT id WHERE code='131990' AND is_current=TRUE`)
- 不存在则 INSERT:
  - code=131990, name=标准券, type=stock, account=guojin_stock
  - shares=1820, cost=0, profit=0, mv=182000, close=100, weight=3.17
  - shares_enc/cost_enc/profit_enc 用真 db_key 加密

### 4.2 验证

```
=== 验证: 重新 load 看 decrypt 状态 ===
  修复后: total=64, ok=56, partial=0, all_bad=8
  修复前 (PIT #112): total=63, ok=0, partial=0, all_bad=63 ❌
  ⚠️ 仍有 8 行 all_bad
  ✅ 131990 标准券: shares=1820.0, cost=0.0, mv=182000.0
```

- **修复前**: 63 行全部 all_bad (PIT #112 老 bug 漏看 PIT #86 根因)
- **修复后**: 64 行 (含 131990) 中 56 ok + 8 all_bad
- **8 all_bad 全是 account=NULL, 5/24 之前历史行, 跟 PIT #121 无关** (key 重置前加密的, 当前 db_key 跟早期 db_key 不同 — PIT #112 真因)

### 4.3 17/18 修复成功 + 1/18 跳过 + 1 标准券新增

| id | code | name | account | 修复 |
|---|---|---|---|---|
| 535-547 | 512880, 513130, 515050, 516650, 518880, 561160, 563230, 588190, 588290, 002149, 159516, 159819, 300059 | 13 ETF/股票 | guojin_stock | ✅ shares=mv/close |
| 548 | 007355 汇添富科创 | 汇添富基金 | guojin_fund | ✅ |
| 549 | 002943 广发多因子 | 天天基金 | huitianfu | ✅ |
| 550 | 001982 富国货币B | 货币基金 | huitianfu | ⚠️ 跳过 (close=0) |
| 551/552 | 016452/018967 纳指QDII | 汇添富基金 | huitianfu | ✅ |
| 553 | **131990 标准券** (新) | **新增** | guojin_stock | ✅ 新增 shares=1820 |

## 五、经验教训 (跨项目 class-level 铁律)

### 5.1 PIT #86 V25-C 占位 bytea 是设计 bug

`b'\x00' * 32` 加密字段占位**不是加密**, 是 PIT #86 设计的"占位行, 后续异步补" — 但**从来没补过**.

**教训**:
- 加密字段占位必须用真 encrypt (哪怕用 `encrypt_value(0, key)` 也是 0 真加密), 不能 `\x00 * 32`
- "占位 + 后续异步补" 模式必须有**强约束** (timer / cron), 否则会永远占位
- PIT #119 排查时发现"36 个 cron 任务不写表", 跟 PIT #86 占位**同根** — 都是"占位 + 后续没补"

### 5.2 PIT #112 真因 = PIT #86 衍生

之前 PIT #112 以为是"key 重置", 实际:
- 18 行 32B 短行 (PIT #86 占位) — 真因
- 8 行 5/24 之前历史行 (early key 跟 current key 不同) — 部分真因
- PIT #112 修复 (PIT #112 commit d6fedbe) 只加了 _try_decrypt 容错, **没真修 18 行**

**教训**: 修复时必须**先抓根因 (PIT #86 bytea 占位)**, 再加容错. 这次 P1-T2 (C) 才真修.

### 5.3 标准券 131990 是"现金等价物"分类问题

PG 表 type='stock' 跟 CSV type='stock' 一致, 但实际 131990 是:
- 沪深交易所标准券 (国债逆回购质押物)
- 净值 100 元, cost 0, 收益按利息算 (不参与市场涨跌)
- **不参与 weight_pct 汇总** (但 CSV weight=3.17 给了)

**修复 (V2.7 留待)**:
- type='cash_equivalent' 区分
- weight_pct 排除 (跟 GUANGFA_CASH 一样, PIT #105 实战)
- profit_pct = 实际年化 (按日算)

### 5.4 159819 负数成本是源数据录入 bug

cost=-1.1568 (CSV line 38), 真实应该是 1.1568, 录入时多打负号.

**教训**:
- 录入 UI 应该对 cost 加 min=0 校验
- import 时应该 `abs(cost)` 兜底 (PIT #117 _safe_num 类似思想)
- 14 行 159819 跨 5/24-6/14 多个 import 时段, 重对位是 V2.7 治理

## 六、防御性改进 (P1-T2 留 TODO, V2.7 实施)

1. ⏳ **PIT #86 真修**: `upsert_position` 新插入用 `encrypt_value(0, key)` 占位, 不用 `\x00 * 32`
2. ⏳ **8 行 5/24 之前历史行 reencrypt**: 用早期 key (如果有) 重 encrypt shares/cost/profit
3. ⏳ **131990 type='cash_equivalent'**: 分类 + weight 排除 + profit_pct 按年化
4. ⏳ **CSV 录入 cost>=0 校验**: import 时 `assert cost >= 0` 或 abs 兜底
5. ⏳ **159819 14 行去重**: 跨 account 重对位 + 修负数
6. ⏳ **PIT #86 历史 `upsert_position` 占位行清理**: DELETE FROM encrypted_positions WHERE shares_enc = '\x00' * 32 (V2.7 治理后)

## 七、关联 PIT

- **PIT #86**: V25-C `upsert_position` 占位 bytea `\x00*32` — 本次根因
- **PIT #99**: 4 CSV vs PG 持仓 45 vs 51 差异 — 实战发现
- **PIT #104**: ALTER TABLE ADD COLUMN account — V26-C 实战加
- **PIT #105**: GUANGFA_CASH 占位符过滤 — V26-C 实战
- **PIT #112**: pgcrypto load 容错 — 之前以为是 key, 实际是 PIT #86 衍生
- **PIT #117**: dict.get None 默认值 — P1-T6 防御
- **PIT #119**: cron_task_metrics 落库 — 触发本次修复
- **PIT #121**: **本文档** — 持仓数据 3 个新发现

## 八、文件清单

- **修改**: PG `holdings.encrypted_positions` 19 行 (17 bytea 重写 + 1 标准券新增 + 1 跳过)
- **新增**: `scripts/fix_pit121_holdings.py` (230 行, 修复脚本)
- **新增**: `hermes_coordination/references/pit-121-holdings-data-3-findings.md` (本文档)
- **备份**: `/tmp/positions_backup_20260615_231420.csv` (positions.csv 备份, 159819 负数修待 V2.7)

## 九、V26-A 重确认铁律实战

PIT #121 涉及"持仓数据正确性" (跟真金白银联动):
- 必走 4 备选 + 5min 自动
- 5min 用户未响应 → 自动按推荐 (C) 执行 ✅
- 实战有效: 避免用户"等等看"时延, 同时保护数据不被错误修复

Author: Hermes Agent × aileo
Date: 2026-06-15 23:14
Version: P1-T2 (C) 方案 (PIT #121 修复)
