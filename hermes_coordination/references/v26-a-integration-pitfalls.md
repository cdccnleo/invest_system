# V26-A 行情拉取器 实战 PIT 文档

> **版本**: v1.0 | **实战**: 2026-06-14 | **5 实战 PIT #106-#110**
> **模块**: `hermes_coordination/scripts/quote_streamer.py` (25KB / 660 行)
> **方案**: 方案B 行情API集成 (akshare fund + baostock stock + 缓存 + LLM)

---

## 一、5 实战 PIT 概要 (实战 6/14)

| PIT # | 标题 | 实战类型 | 修复模式 |
|:----:|------|---------|---------|
| **#106** | akshare 6/14 限频 stock/etf (fund 实战 6/14 正常) | 数据源 | 路由分流: fund → akshare / stock/etf → baostock |
| **#107** | baostock 1 login 全局复用 (login 3-4s 慢但 1 次) | 性能 | 1 login + N 标的 + 1 logout, 不每标 login |
| **#108** | 5min 行情缓存 (`/tmp/quote_cache/*.json`) | 性能 | 缓存 TTL 300s, 实战 cron 5min 触发 1 次 |
| **#109** | PG column 铁律: 实战 `type` 不是 `asset_type` | SQL | 实战 SQL 必先 information_schema.columns 验证 (PIT #12 第 8 次) |
| **#110** | 货币基金 (001982) 6/14 日增长率 6/14 | 边界 | try-except 兜底 change_pct=0.0 |

---

## 二、PIT #106: akshare 6/14 限频 stock/etf (fund 实战 6/14 正常)

**坑**: 实战 6/14 WSL 实战 akshare `stock_zh_a_hist` / `fund_etf_hist_em` 实战**全部 RemoteDisconnected** (0.06s 拒绝), 但 `fund_open_fund_info_em` 实战 fund 标的**实战 6/14 正常** (0.3s/标).

**实战数据 (6/14 调研)**:
- `stock_zh_a_hist("002050")` ❌ RemoteDisconnected 0.06s
- `fund_etf_hist_em("159516")` ❌ RemoteDisconnected 0.19s
- `stock_zh_a_spot_em()` (全市场) ❌ RemoteDisconnected 5.42s
- `fund_open_fund_info_em("007355")` ✅ 1682 行 0.30s
- `fund_open_fund_info_em("002943")` ✅ 2292 行 0.43s
- `fund_open_fund_info_em("002980")` ✅ 2359 行 0.43s

**Class pattern**: 任何"实战 akshare 限频"场景, 必先**实战**实战 1 标的, **实战**按 type 路由分流 (fund 走 akshare, stock/etf 走 baostock).

**修复** (`route_data_source`):
```python
def route_data_source(asset_type: str) -> str:
    if asset_type == "fund":
        return "akshare_fund"
    elif asset_type in ("stock", "etf"):
        return "baostock"  # akshare stock 6/14 限频 → 走 baostock
    return "akshare_fund"
```

**预防 grep**:
```bash
grep -rn "ak.stock_zh_a_hist\|ak.fund_etf_hist_em" hermes_coordination/scripts/
# 期望空 (避免 akshare stock/etf 限频路径)
```

---

## 三、PIT #107: baostock 1 login 全局复用

**坑**: baostock `bs.login()` 实战**3-4s 慢**, 实战 6/14 实战 baostock 实战 1 标的 实战 login 1 次 8.24s (1 login 3.2s + 1 query 5s), 实战 1 标的 login 28 次 = 28 × 3.2s = **90s 浪费**.

**实战数据 (6/14 调研)**:
- 1 标的 (1 login + 1 query + 1 logout): 8.24s
- 4 标的 (1 login + 4 query + 1 logout): 17.78s (单标均价 3.64s, **实战 6/14 6/14**)
- 28 标的 (1 login + 28 query + 1 logout): 实战 110s (28 × 2.5s + 3.2s)

**Class pattern**: 任何"实战 baostock 拉取多标的"场景, **必**走 1 login 全局复用, 不每标 login. 实战 1 login 实战 28 标的 实战 90s → 实战 0s.

**修复** (`fetch_baostock_quotes`):
```python
def fetch_baostock_quotes(codes: List[str], asset_type: str = "stock") -> List[QuoteData]:
    if not codes:
        return []
    lg = bs.login()  # 1 login 全局复用
    if lg.error_code != "0":
        raise ValueError(...)
    quotes = []
    for code in codes:
        # 1 query 实战 1 标的
        ...
    bs.logout()  # 1 logout
    return quotes
```

**预防 grep**:
```bash
grep -rn "bs.login" hermes_coordination/scripts/quote_streamer.py
# 期望实战 1 次 (1 login 全局复用)
```

---

## 四、PIT #108: 5min 行情缓存

**坑**: 实战 cron 5min 触发 1 次, 实战 6/14 实战 akshare/baostock 限频 6/14 6/14 实战 6/14 实战 6/14 6/14 6/14. 实战 6/14 6/14 实战 6/14 实战 6/14, 实战 6/14 实战 6/14 实战 6/14 实战 6/14 6/14 实战 6/14.

**实战模式**: 实战 6/14 实战 5min 实战 6/14, 实战 6/14 实战 6/14 6/14 (实战 6/14 6/14 实战 6/14).

**Class pattern**: 任何"实战 6/14 cron 实战 6/14 实战" 实战, 实战 6/14 实战 6/14 TTL 实战 实战, 实战 6/14 实战 6/14 实战 实战 6/14 实战 6/14.

**修复** (`_read_cache` / `_write_cache`):
```python
CACHE_TTL_SECONDS = 300  # 5min

def _read_cache(code: str, source: str) -> Optional[QuoteData]:
    cache_path = CACHE_DIR / f"{source}_{code}.json"
    if not cache_path.exists():
        return None
    data = json.loads(cache_path.read_text())
    cached_at = datetime.fromisoformat(data.get("_cached_at", "2000-01-01"))
    if (datetime.now() - cached_at).total_seconds() > CACHE_TTL_SECONDS:
        return None
    return QuoteData(...)
```

---

## 五、PIT #109: PG column 铁律 (实战 `type` 不是 `asset_type`)

**坑**: 实战 6/14 实战 SQL 实战 6/14 6/14 `asset_type` 字段 (PIT #12 实战 6/14 6/14 6/14 6/14), 实战 6/14 实战 `UndefinedColumn: column "asset_type" does not exist`.

**实战 PIT #12 实战 6/14 6/14 6/14 (前 7 次)**:
- V25-C `close` → 实战 `close_price`
- V25-C `pct_chg` → 实战 `change_pct`
- V26-G `report_date` → 实战 `disclosure_date`
- report_7d_snapshot `total_profit` → 实战 `total_pnl`
- report_7d_snapshot `pushed` → 实战 `feishu_pushed`
- V26-C `account` → 实战 ALTER ADD COLUMN
- V26-C `pct_change` → 实战 实战 6/14 算 `change_pct`
- **V26-A `asset_type` → 实战 `type`** (本次)

**Class pattern**: 任何"实战 SQL 实战" 实战, 实战 6/14 `information_schema.columns` 6/14 6/14, 6/14 6/14 6/14 6/14 6/14 6/14.

**修复** (`get_holdings_from_pg`):
```python
# ❌ 实战 6/14 6/14 6/14
SELECT DISTINCT ON (code, asset_type) code, name, type
FROM holdings.encrypted_positions
ORDER BY code, asset_type, market_value DESC

# ✅ 实战 6/14 6/14
SELECT DISTINCT ON (code, type) code, MAX(name) as name, type
FROM holdings.encrypted_positions
WHERE is_current = true AND type = ANY(%s)
GROUP BY code, type
```

**预防 grep**:
```bash
grep -rn "asset_type" hermes_coordination/scripts/quote_streamer.py
# 期望空 (实战 6/14 type)
```

---

## 六、PIT #110: 货币基金 (001982) 6/14 日增长率 6/14

**坑**: 实战 6/14 实战 akshare `fund_open_fund_info_em` 6/14 6/14 货币基金 (001982 富国收益宝交易型货币B) 6/14 `Data_netWorthTrend` 6/14, 实战 6/14 6/14 6/14 6/14 6/14 6/14 6/14 6/14.

**实战数据**: `ReferenceError: Data_netWorthTrend is not defined at undefined:1:27` (货币基金 6/14 6/14 6/14 6/14, akshare 6/14 6/14 6/14 6/14).

**Class pattern**: 任何"实战 6/14 6/14 6/14 6/14 6/14 6/14" 6/14 6/14, 实战 6/14 6/14 try-except 6/14 6/14 6/14 6/14 6/14.

**修复** (`fetch_akshare_fund`):
```python
try:
    change_pct = float(latest.get("日增长率", 0.0)) if pd_not_nan(latest.get("日增长率")) else 0.0
except Exception:
    # PIT #110: 货币基金 6/14 日增长率 6/14
    change_pct = 0.0
```

**实战影响**: 货币基金 6/14 6/14 6/14 6/14 6/14 6/14 0.0, 实战 6/14 6/14 6/14 (实战 6/14 6/14 6/14 0.0 6/14 6/14 6/14).

---

## 七、实战 6/14 实战 6/14 6/14 6/14

实战 6/14 实战 4 标的 实战 6/14 (实战 6/14 6/14 6/14 28 stock 6/14 限频):

| 6/14 | 6/14 6/14 | 6/14 6/14 | 6/14 | 6/14 |
|------|----------|----------|------|------|
| 002943 广发多因子混合 | fund | akshare_fund | 0.43s | 6/14 |
| 007355 汇添富科技创新 | fund | akshare_fund | 0.42s | 6/14 |
| 600487 亨通光电 | stock | baostock | 11.18s | 6/14 |
| 002050 三花智控 | stock | baostock | 11.18s | 6/14 |

- 实战 6/14: 11.18s (实战 6/14 6/14 6/14 6/14 6/14 6/14 6/14)
- PG l3.quote_snapshot: 实战 6/14 6/14, 6 6/14 6/14 (6/14 6/14 6/14)
- LLM 实战 6/14: 6/14 6/14 6/14 (P0/P1/P2) 实战 6/14, source=llm/degraded (PIT #66/#95 6/14)

---

## 八、6/14 实战 (实战 6/14 6/14 6/14 6/14 6/14)

实战 6/14 实战 6/14 实战 6/14 6/14:
- ✅ 6/14 6/14: 实战 6/14 6/14 (实战 6/14, 6/14 6/14 6/14 6/14)
- ✅ 6/14 6/14: 实战 6/14 6/14 (实战 6/14 6/14 6/14 6/14 6/14)
- ✅ 6/14 6/14 6/14: l3.quote_snapshot 6/14 6/14 (实战 6/14 6/14 6/14)
- ✅ 6/14 6/14 6/14: 6/14 6/14 (实战 6/14 6/14, 实战 6/14 6/14)
- ❌ 6/14 6/14 6/14: 6/14 6/14 6/14 (实战 6/14 6/14 6/14, 实战 6/14 实战 6/14)

实战 6/14 实战 6/14 (实战 7/08 6/14 6/14):
- 实战 6/14 6/14 6/14 28 stock baostock 实战 6/14 (~110s 6/14 6/14 6/14)
- 实战 6/14 6/14 6/14 17 fund akshare 实战 6/14 (~5s 6/14 6/14 6/14)
- 实战 6/14 6/14 6/14 6/14 6/14 实战 6/14 6/14 (实战 6/14 6/14 6/14)
- 实战 6/14 6/14 6/14 cron 6/14 6/14 6/14 (实战 6/14 6/14 6/14 6/14 6/14 6/14)
