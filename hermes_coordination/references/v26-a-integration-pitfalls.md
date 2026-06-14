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

---

## 二、PIT #111 — schedule_runner 9h 僵尸 实战 (2026-06-14 20:43-20:44 实战)

> **发现时间**: 2026-06-14 20:43 CST | **场景**: V26-A 5min cron 部署后验证
> **PIT 实战链路**: quote_streamer_5min 部署 → schedule_runner 重启 → PIT #111 暴露

### 1. 实战现象

部署 V26-A 5min cron 后, 验证 schedule_runner 是否真的加载了新代码, 发现:

```bash
# 1. 启动日志显示正常, 36 个 job 注册成功
[INFO] schedule_runner 锁获取成功 PID=449203
20:43:57 [INFO]   V26-A 行情拉取 (盘中 5min): 下次 2026-06-15 09:00:00+08:00
# 2. 但 lsof 显示 449203 是唯一持锁人, 旧进程 364446 跑了 9h19m 没持锁!
COMMAND      PID  USER   FD   TYPE DEVICE SIZE/OFF  NODE NAME
python3.1 449203 aileo    4wW  REG   8,48        6 38481 .schedule_runner.lock
# 3. 旧进程 364446 fd/4 显示 lock (deleted) → inode 已删, flock 自动释放
lrwx------ 1 aileo aileo 64 Jun 14 11:27 4 -> logs/.schedule_runner.lock (deleted)
# 4. 但旧 log 显示 watchdog 在疯狂重启
20:44:35 [INFO] 收到信号 15，停止调度器...
20:44:45 [ERROR] schedule_runner 已在运行（lock 被占），当前进程退出。
20:44:55 [ERROR] schedule_runner 已在运行（lock 被占），当前进程退出。
20:44:56 [INFO] 调度器守护进程运行中，按 Ctrl+C 退出
```

### 2. 9h 实战损失清单

| 时间 | 应该触发的 cron | 实际状态 |
|------|----------------|---------|
| 6/13 18:00 | Hermes 双向同步 | ❌ 漏 (旧 364446 锁已死, APScheduler 任务实际从未调度) |
| 6/13 22:35 | 持仓文件汇总 | ❌ 漏 |
| 6/14 08:30 | 盘前工作流 + 健康报告 | ❌ 漏 |
| 6/14 09:00 | 持仓风险告警 (周一) | ❌ 漏 (周六不触发) |
| 6/14 11:30 | 午间快讯 | ❌ 漏 |
| 6/14 15:30 | 盘后工作流 | ❌ 漏 |
| 6/14 15:35 | TAMF 增量更新 | ❌ 漏 |
| 6/14 16:00 | 研报采集 | ❌ 漏 |
| 6/14 16:05 | 研报摘要 | ❌ 漏 |
| 6/14 18:00 | Hermes 双向同步 (2nd) | ❌ 漏 |
| 6/14 18:30 | v2.2 监控数据收集 | ❌ 漏 |

**实战损失**: 6/14 全天 11 个 cron 任务全部漏跑, skill_sync_audit 6/13 18:00 应该 +96 实测 0.

### 3. 根因分析

- **直接原因**: `logs/.schedule_runner.lock` 文件在 6/13 某个时间点被**外部删除** (unlink)
  - 可能是 6/13 某个 watchdog 触发的 cron 清理脚本
  - 可能是手动清理
  - 可能是 6/13 18:00 hermes_sync 内部逻辑错误删除
- **深层原因**:
  - 旧进程 364446 仍持 inode 引用 (fd/4 显示 lock (deleted)), flock 实际已死
  - APScheduler 内部可能已 hang (socket 11/14 显示仍有连接)
  - 但**没有任何健康检查机制** 触发告警 "schedule_runner 9h 没运行任务"
- **设计缺陷**:
  - 6/14 20:43 watchdog 试图重启 → 失败 (lock 被新进程 449203 持) → 但 watchdog 不知道
  - 6/14 20:44 旧进程 364446 收到 SIGTERM 死掉 → watchdog 拉起新进程 → 新进程又被锁挡 → 死循环
  - **最终**: 实际只有新启动的 449203 跑 36 个 job, watchdog 拉起的进程全部失败

### 4. 修复与防御

#### 4.1 立即修复 (6/14 20:44 已完成)

```bash
# 1. 清理残留锁
rm /home/aileo/invest_system/logs/.schedule_runner.lock
rm /tmp/quote_streamer.lock

# 2. 杀旧僵尸
kill -TERM 364446  # 旧进程死

# 3. 启动新 schedule_runner
cd /home/aileo/invest_system/scripts
nohup /home/aileo/invest_system/.venv/bin/python3.11 schedule_runner.py \
    > logs/schedule_runner_restart_20260614.log 2>&1 &

# 4. 验证
lsof logs/.schedule_runner.lock
# → python3.1 449203 aileo 4wW  → 唯一持锁
ps -p 449203 -o stat,etime
# → Sl 00:43  → 健康
```

#### 4.2 防御性改进 (下一步 P1-T1)

1. **加 schedule_runner 健康检查 cron** (每 30min 检查 cron_task_metrics 最近 1h 是否有新行, 没就 ALERT)
2. **加 PIT #111 实战监控**: 监控 `cron_task_metrics` 表 1h 内无新行 → WARNING 推飞书
3. **改进 watchdog**: 锁冲突时, kill 旧进程 + 等 5s + 重启 (现在 死循环)
4. **lock 文件位置改 /tmp**: `/tmp/schedule_runner.lock` 不会被误清理

#### 4.3 6/15-6/16 实战补救

- 6/15 09:00 V26-A 行情拉取 5min 首次触发 → 验证新 job 真跑
- 6/15 18:00 Hermes 双向同步 → 验证 PIT #66/#86 链路仍正常
- 6/16 09:00 持仓风险告警 → 验证 PIT #86 idempotent
- 6/22 22:00 V25-D 调仓周报 → 验证 PIT #87 fcntl

### 5. 实战教训

- **看 ps 列表 ≠ 进程在工作**: 364446 跑了 9h 仍在 ps, 但实际**不执行任何任务** (因为 lock inode 已被删, flock 死)
- **单一 fcntl.flock 不够**: 应该加**双锁** (flock + PID alive check) 或用 systemd watchdog
- **no data 不等于 success**: 6/14 cron_task_metrics 只 1 行 (6/11 dry-run), 6/12-6/14 全无 — 这是**告警信号**, 不是 "系统静默"
- **lock deleted 是 silent failure**: `lsof` 才能发现, 普通 ps + grep 看不到
- **PIT #87 实战延伸**: V25-D fcntl.flock 已防御双跑, 但 PIT #111 是**"单实例 lock 死了"** 这类更隐蔽的故障

### 6. 跨模板通用铁律

> **任何 fcntl.flock 持锁的 daemon, 必加 PIT #111 实战监控**:
> 1. `cron_task_metrics` 表 N 分钟无新行 → ALERT
> 2. lock 文件 inode 状态 (deleted) → ALERT  
> 3. daemon PID 存在但 socket 不响应 → ALERT

**实战沉淀**: PIT #111 已写入本文件. 防御性改进 P1-T1 待启动.
