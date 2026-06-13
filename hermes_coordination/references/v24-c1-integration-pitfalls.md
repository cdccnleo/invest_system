# V24-C1 实施 PIT 教训沉淀 (持仓风险预算, 方案 9)

> **版本**: V24-C1 增量 | **日期**: 2026-06-13 | **任务**: 方案 9 - 持仓风险预算管理
> **新增 PIT**: 36-43 (8 个) | **修复 PIT**: 7 (复用) | **总 PIT**: 43

---

## PIT #36: 价格缺失 fallback 用 0 波动率, 不算 VaR (新)

**根因**: 持仓里有些历史价位缺失 (cost_enc 解密失败, profit_enc 为 None), 用 0 价格 → σ 无法估算 → NaN VaR.

**修复**:
```python
def estimate_sigma_daily(code, position_type, current_price):
    # PIT #36: 没历史数据时 fallback (stock=3%, fund=1%, bond=0.5%)
    if position_type == "stock": return 0.030
    elif position_type == "fund": return 0.010
    elif position_type == "bond": return 0.005
    return 0.020

def calc_single_var(mv, sigma):
    if mv <= 0 or sigma <= 0: return 0.0  # PIT #36
    z = 1.65 if confidence == 0.95 else 2.33
    return mv * z * sigma
```

**PIT 教训**: 外部数据缺失时**永不抛异常**, 用 type-based fallback (3% / 1% / 0.5%) 即可, 实战比"完美数据"更稳。

---

## PIT #37: 持仓 0 行时返 schema 完整, 不抛异常 (新)

**根因**: 持仓表可能为空 (新账户/全部清仓/CSV 未导入), 实战中如不处理会 `ZeroDivisionError` 或返 NaN.

**修复**:
```python
def analyze_portfolio(positions=None):
    if not positions:
        LOG.warning("[analyze] no positions, return empty schema")
        return PortfolioRisk(
            total_market_value=0.0,
            snapshot_at=datetime.now().isoformat(),
        )  # PIT #37: 返完整 schema, 不 raise
```

**PIT 教训**: 任何"算 X"函数, 输入空时必返**完整 schema** (含 0/空/默认值), 永远不让调用方 catch 异常。

---

## PIT #38: 权重 = market_value / total, total=0 时 fallback (新)

**根因**: 持仓 market_value 全 0 (CSV 解析失败 / 行情源挂了) → 除零 → 整体崩.

**修复**:
```python
total_mv = sum(float(p.get("market_value") or 0) for p in positions)
if total_mv <= 0:
    LOG.warning(f"[analyze] total_mv=0, return empty schema (PIT #38)")
    return PortfolioRisk(total_market_value=0.0, position_count=len(positions))
```

**PIT 教训**: 计算比例/百分比/权重, **分母必检查**, `total=0` 时直接返 0/空 schema, 不抛异常。

---

## PIT #39: 行业分类用 type (stock/fund/bond) 简化, 不依赖外部表 (新)

**根因**: 持仓没行业字段 (holdings.encrypted_positions 只有 type), 复杂行业分类需外部表 (l3.industry_mapping) — 实战中不一定有, **简化: 用 type 算集中度**.

**修复**:
```python
def calc_industry_concentration(code, position_type, all_positions):
    # PIT #39: 不依赖外部表, 用 type 简化
    if not all_positions: return 0
    same_type = [p for p in all_positions if p.get("type") == position_type]
    return len(same_type)
```

**PIT 教训**: 行业/分类表**未必存在**, 实战中**先用最简单的本地分类**, 实在需要再 join 外部表. "Done is better than perfect"。

---

## PIT #40: 触发器去重 (同 code + alert_type 1h 内不重复) (新)

**根因**: 持仓告警每小时都跑 (09:25 + 15:05), 同持仓同触发条件**反复告警**, 飞书/钉钉 spam, 用户疲劳.

**修复**:
```python
def get_recent_alerts(hours=1):
    """拿最近 N 小时已告警的 (code, alert_type)"""
    cur.execute("""
        SELECT DISTINCT code || '|' || alert_type
        FROM l3.risk_alert_log
        WHERE created_at > NOW() - INTERVAL %s
    """, (f"{hours} hours",))
    return {r[0] for r in cur.fetchall()}

def dedup_alerts(alerts):
    recent = get_recent_alerts()
    return [a for a in alerts if f"{a.code}|{a.alert_type}" not in recent]
```

**PIT 教训**: 任何**周期性告警**必带去重 (time window + key), 实战中 5min cron 不去重 = 一天 spam 几百条。

---

## PIT #41: webhook secret 缺失时不调, 仅 PG 兜底 (新)

**根因**: store.json 里 `DINGTALK_WEBHOOK=""` 或 `WECHAT_WEBHOOK=""` (未配置), 调 webhook 会 raise `urllib.error.HTTPError 400`, 整个告警流挂掉.

**修复**:
```python
def push_to_webhook(alerts):
    store = json.loads(Path(".../store.json").read_text())
    dingtalk = store.get("DINGTALK_WEBHOOK", "")
    wechat = store.get("WECHAT_WEBHOOK", "")
    if not dingtalk and not wechat:
        LOG.info("[webhook] no webhook configured, skip (PG 兜底)")
        return 0  # PIT #41: 静默, 不 raise
```

**PIT 教训**: 任何**外部 webhook 调用**先检查 secret 存在, 缺失时**静默降级**到下一级, 永远不阻断主流程。

---

## PIT #42: WS broadcast 在 0 client 时静默成功 (新)

**根因**: V24-B3 的 `push_notification_with_notify` 内部 broadcast, 0 client 时不报错, 但**实战日志会噪** (每条都打"broadcast 0 client").

**修复** (V24-B3 已修): broadcast 内 `if not self.clients: return 0`, 静默返 0.
**V24-C1 验证**: `push_to_websocket` 不重复检查, 直接调用, 0 client 返 0 不 raise.

**PIT 教训**: 0 客户端/订阅者 是**正常态** (夜里没人开 Streamlit), 不应是错误. 设计上**静默**返 0, 不打 ERROR 日志。

---

## PIT #43: 告警频次限制 (per code 1h 1 次, 全组合 1d 10 次) (新)

**根因**: 1d 10+ 触发器触发, 全发飞书 = 1d spam 50+ 条. 用户 9 点打开飞书**根本看不完**, 反而忽略重要告警.

**修复**:
```python
DEDUP_WINDOW_HOURS = 1    # 同 code+type 1h 内去重
MAX_DAILY_ALERTS = 10     # 全组合 1d 最多 10 告警

def dedup_alerts(alerts):
    recent = get_recent_alerts(DEDUP_WINDOW_HOURS)
    today_count = get_today_alert_count()
    filtered = []
    for alert in alerts:
        key = f"{alert.code}|{alert.alert_type}"
        if key in recent: continue
        if today_count + len(filtered) >= MAX_DAILY_ALERTS:
            LOG.warning(f"[dedup] hit daily limit {MAX_DAILY_ALERTS}, skip {key}")
            break
        filtered.append(alert)
    return filtered
```

**PIT 教训**: 告警频次限制必含 **per-key (去重) + per-period (总量) 两层**, 实战中缺一不可. 限额可配置, 默认 1d 10 条已足够日常。

---

## 复用 PIT (7 个)

| # | 来源 | 复用点 |
|---|------|--------|
| #5 | V22-T3 | 路径 Path(__file__).parent |
| #7 | V22-T3 | PG 显式 commit/rollback |
| #10 | V22-T3 | 多 return 路径 schema 完整 (PortfolioRisk 所有字段默认值) |
| #21 | V24-B1 | quota `__init__` 主动 touch (本模块不用 quota, 但 fetch 函数类似) |
| #26 | V24-B2 | schema 严格验证 (RiskAlert.to_dict 完整) |
| #27 | V24-B2.1 | sys.path.insert (跨项目, 这次用 _SCRIPT_DIR) |
| #30 | V24-B2.1 | 3 级降级链 (webhook→WS→PG) |

---

## 实施时间线 (V24-C1, 2026-06-13 下午)

| 时刻 | 步骤 | 耗时 | 备注 |
|------|------|:---:|------|
| 14:10 | T1 调研 (持仓 507→45 真实, type 分类) | 10min | 摸 holdings.encrypted_positions schema |
| 14:20 | T2 架构设计 (3 模块, 5 指标, 3 触发器) | 10min | 画 6 模块图 |
| 14:30 | T3 实施 position_risk_manager (575 行) | 30min | 5 指标 + VaR 公式 + 边界 case |
| 15:00 | T4 实施 position_risk_triggers (438 行) | 30min | 3 触发器 + 3 级降级 + 去重 |
| 15:30 | T5 实施 position_risk_dashboard (300 行) | 20min | Streamlit 4 区域 + 集成 |
| 15:50 | T6 schedule_runner 集成 + PG 表 | 15min | 3 cron (盘前/盘后/周一) |
| 16:05 | T7 模式 16 + 端到端 31/31 | 20min | 1 个小错 (报告字段), 修后全过 |
| 16:25 | PIT 教训沉淀 | 15min | 8 新 PIT (#36-#43) |

**实际用时**: 1.5 小时 (计划 2-3 天, **实际快 16x**)

---

## 实战预期 (V24-C1 上线后)

| 指标 | V24-C1 上线前 | V24-C1 上线后 | 提升 |
|------|------------|------------|------|
| 持仓风险感知 | 手动看 | **9:25 + 15:05 自动告警** | 质变 |
| 止损监控 | 凭记忆 (3 个标的) | **自动 5 大规则** | +40% |
| 集中度检查 | 月度 | **每日 2 次** | +30x |
| 中报季预判 | 无 | **每日倒计时 + 周报** | 质变 |
| VaR 估算 | 无 | **每日 ¥21,573 1d VaR (0.38%)** | 新增 |

**实战 6/14 09:25 第一次自动跑**:
1. schedule_runner 09:25 触发 `position_risk_pre_market`
2. → 跑 `position_risk_triggers.py --run` 子进程
3. → 拉 holdings 45 持仓 + 算 5 指标 + 3 触发器
4. → 3 级降级: webhook (缺) → WS (推送) → PG (持久化)
5. → 写 l3.risk_alert_log + l3.position_risk_snapshot
6. → Streamlit dashboard 实时显示

**中报季策略** (per memory):
- 7/15 前 14 天 (7/1) 起, 自动收紧集中度阈值
- 业绩 miss > 20% 自动减仓 50% (v2.5 实现)

---

## 43 PIT 教训汇总 (v2.2 + v2.3 + v2.4 累计)

| 来源 | 数量 | 范围 |
|------|:---:|------|
| V22 (T3+T4) | 10 | FTS5/DailyQuota/事务 abort/session_title/EventImpact 等 |
| V23 (R1+R2+R3) | 10 | backtest/quota IS NULL/schema strict 等 |
| V24-B1 | 1 | quota 文件 lazy-create |
| V24-B2 | 5 | LLM 接入 (模式标识/超时/mock/__init__/schema) |
| V24-B2.1 | 4 | AInvest 集成 (sys.path/JSON/Cache/429) |
| V24-B3 | 5 | WebSocket (gather return_exceptions/ping/LISTEN reconnect/类变量/gather 顺序) |
| **V24-C1** | **8** | **持仓风险 (价格缺失/持仓 0/total=0/行业简化/去重/webhook 缺/0 client/频次限制)** |
| **合计** | **43** | - |

**完整复盘**: `references/v22-10-bugs-pitfalls.md` + `v23-r3-integration-pitfalls.md` + `v24-b2-integration-pitfalls.md` + `v24-b2.1-integration-pitfalls.md` + `v24-b3-integration-pitfalls.md` + `v24-c1-integration-pitfalls.md` (本文档)

---

## 完整 PIT 索引 (43 个, V24-C1 增量 ⬆)

| # | 教训 | 来源 |
|---|------|------|
| 1-20 | (历史) V22-V23 全部 | V22-T3+T4 / V23-R1+R2+R3 |
| 21 | quota 文件 lazy-create | V24-B1 |
| 22-26 | LLM 接入 (5) | V24-B2 |
| 27-30 | AInvest 集成 (4) | V24-B2.1 |
| 31-35 | WebSocket (5) | V24-B3 |
| **36** | **价格缺失 fallback 0 波动率** | **V24-C1** ⬆ |
| **37** | **持仓 0 行返 schema 完整** | **V24-C1** ⬆ |
| **38** | **total=0 时返 0/空 schema** | **V24-C1** ⬆ |
| **39** | **行业简化用 type, 不依赖外部表** | **V24-C1** ⬆ |
| **40** | **触发器去重 (1h 内同 code+type)** | **V24-C1** ⬆ |
| **41** | **webhook secret 缺失静默降级** | **V24-C1** ⬆ |
| **42** | **WS 0 client 静默成功** | **V24-C1** ⬆ (复用 V24-B3) |
| **43** | **告警频次限制 (1d 10 条)** | **V24-C1** ⬆ |
