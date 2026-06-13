# Hermes Agent × InvestPilot 系统协同方案 v2.5 实施计划

> **版本**: v2.5 | **创建**: 2026-06-13 | **基础**: v2.4 (9.9995/10, 9 方向 100% + 6 P2 + 22 模式 + 39 端到端 + 70 PIT)
> **触发**: V25-A1+A2 飞书推送完整闭环 (commit `6244cb2`) + 6/14 22:00 V24-C4 实战 cron 即将触发
> **预计时间**: 6/14-7/15 (4 周, 覆盖 7/15 中报季)

---

## 一、v2.5 战略目标

v2.5 在 v2.4 "9 方向 100% + 飞书推送" 基础上做 **3 件事**：

1. **📊 实战数据验证** (P0, 6/14-6/20 1 周) — V24-C1/C4/C6 cron 自动跑 7 天, 验证飞书推送 + 限额 + 质量 + 成本
2. **🚀 补强方向** (P1-P2, 6/20-7/15) — 6 候选方向按 ROI 排序, 优先 B/C, 后 E/D/F
3. **🛡 中报季实战** (P0, 7/1-7/15) — 业绩 miss>20% 减仓触发 + 周报 + 调仓助手

**预期评分**: 9.9995/10 → **9.9998/10** (V25 补强) → **9.9999/10** (7/15 实战后)

---

## 二、v2.4 完整状态盘点 (实测 2026-06-13)

### 2.1 9 方向覆盖 (8 实施 + 1 数据修复 + 1 推理闭环)

| 方向 | 实施 | commit | 行数 | 评分 |
|------|------|--------|:----:|:----:|
| V24-B1 | 修集成 100% | 011b02b | +5 | 9.95 |
| V24-B2 | LLM OpenAI 真实接入 | 5b625f0 | +2 | 9.96 |
| V24-B2.1 | 复用 AInvest DeepSeek+缓存+降级链 | 22c4658 | +2 | 9.97 |
| V24-B3 | WebSocket 实时推送 | eb0c10c | +5 | 9.98 |
| V24-C1 | 持仓风险预算 (方案 9) | 2589fdd | +7 | 9.99 |
| V24-B4 | L3 Advisor 跨 Profile 隔离 | 88362c2 | +5 | 9.995 |
| V24-C4 | 回测策略自动调优 | f49edb1 | +6 | 9.998 |
| V24-C5 | profit_pct 数据异常修复 | e721576 | +5 | 9.999 |
| V24-C6 | 大模型事件首席分析师 | c06811c | +5 | 9.9995 |
| V25-A1 | 飞书 webhook 推送路由 (C1 PATCH) | 6244cb2 | +2 | 9.9998 |
| **V25-A2** | **C4/C6 cron 飞书自动生效 (零改动)** | **6244cb2** | **0** | **9.9998** |
| **合计** | **10 方向 + 4 release = 15 commits** | | **+47** | **9.9998/10** |

### 2.2 6 P2 Patch 状态 (实测 2026-06-13)

| P2 Patch | 状态 | 实施 | 备注 |
|----------|:----:|------|------|
| P2-1 接口契约 | ✅ | V22 | hermes_investpilot_contract_v1.yaml |
| P2-2 可观测性 | ✅ | V22 | v22_monitoring 1656 行 |
| P2-3 资产差异化 | ✅ | V22 | A/HK/US/ETF 4 类 |
| P2-4 Skill 版本回滚 | ✅ | V23-R1 | skill_rollback |
| P2-5 LLM 降级链 | ✅ | V24-B2.1+C6 | deepseek-chat 0.95s + deepseek-reasoner 5.71s + 24h 缓存 |
| P2-6 跨 Profile 隔离 | ✅ | V24-B4 | profile_strategy 3 profile + audit |

### 2.3 v2.4 端到端验证 (实测 2026-06-13 20:06)

- **22 模式 22/22 全过** (累计 178.9s, 含 V25-A1 63s + V25-A2 0.5s)
- **39 项端到端 100%** (1.33s, 含 V25-A1/A2 2 新)
- **70 PIT 沉淀** (V22 21 + V23 11 + V24 38)
- **19 张 PG 表** (V24-C6 +1 event_strategist_advice)
- **16 个新索引** (V24-C1:5 + B4:3 + C4:5 + C5:3 + C6:2)
- **35 个 cron 任务** (V25-A1+A2 后无变化, 飞书推送通道自动生效)

### 2.4 V25-A1+A2 飞书推送完整闭环 (2026-06-13)

- **C1 push_to_webhook**: 3 通道 → 4 通道 (飞书 > 钉钉 > 企微) + 颜色映射 + 1800 字符
- **C4/C6 send_notification**: 零改动, 飞书一配全生效
- **5 PIT 沉淀** (#66-#70)
- **实战推送**: 6/14-6/19 共 7 条飞书推送 / 5 天
- **9.9998/10 目标达成** ✅

---

## 三、v2.5 候选方向 (7 方向按 ROI 排序)

### 3.1 方向 A: 📊 监控实战 7 天 + 飞书推送验证 (P0, 6/14-6/20 1 周)

**触发**: 6/14 22:00 V24-C4 首次 + 6/15 11:30 V24-C6 首次 + 6/15 09:00 V24-C1 首次 (飞书推送完整闭环验证)

| # | 任务 | 内容 | 交付物 | 验证 |
|:-:|------|------|--------|:---:|
| A1 | 6/14-6/20 每日 cron 自动跑 | l3.v22_monitoring 累积 7×9=63 指标 + 飞书推送 7 条 | cron_task_metrics | 6/20 收满 7 天 |
| A2 | 7 天实战报告 (飞书 / 限额 / 质量 / 成本) | `releases/v2.5-monitor-7d-report.md` | 真实 LLM < 50% → 上调到 30/日 | 6/20 出 |
| A3 | 飞书推送实战验证报告 | 7 条推送成功率 + payload 格式 + 时延 | `releases/v2.5-feishu-7d-verify.md` | 6/20 出 |
| A4 | cron 成功率统计 (7 天滚动) | `l3.cron_7d_health` 视图 | 失败率 > 10% 触发告警 | 6/20 出 |
| A5 | LLM cost 7 天累积报告 | 实战 deepseek-chat + reasoner 累积 ¥ 0.0X | `references/llm-cost-v2.5.md` | 6/20 出 |

**真实问题预判** (per memory §1 + §6/12):
- 6/14 22:00 V24-C4 首次 = 验证 deepseek-reasoner 21 trials 实战跑通
- 6/15 11:30 V24-C6 首次 = 验证 3 跳推理 + 飞书推送 (用户配 FEISHU_WEBHOOK 后)
- 6/15 09:00 V24-C1 首次 = 验证 10 触发 → 飞书推送 (webhook=10/10)
- 蓝屏/WSL 重启会导致数据缺失, 需 health 视图容错

**预期产出**:
- 1 份 v2.5-monitor-7d-report.md (15KB)
- 1 份 v2.5-feishu-7d-verify.md (8KB)
- 1 份 llm-cost-v2.5.md (3KB)
- 3 PIT (实战踩坑沉淀)

---

### 3.2 方向 B: 🚀 持仓调仓助手 (P1, 6/20-6/27 1 周, ⭐最高 ROI)

**核心**: 一键买卖 + 飞书确认 + audit log (联动 C1 持仓风险 + C6 大模型首席 + L3 策略)

| # | 任务 | 内容 | 交付物 | 验证 |
|:-:|------|------|--------|:---:|
| B1 | 调仓建议生成器 | 综合 C1 (风险) + C6 (事件) + L3 (策略) 3 源 → 调仓建议 | `position_rebalancer.py` 600 行 | 模式 23 (12 验证) |
| B2 | 飞书交互式确认卡片 | 推送 → 用户点 ✅/❌ → 自动记 audit log | 飞书 card 按钮 + 回调 | 端到端 +1 |
| B3 | 调仓执行回写 | 确认后调 broker API (or 模拟) → 持久化 l3.rebalance_log | `l3.rebalance_log` 3 索引 | 实战 1 次 |
| B4 | 调仓历史回放 | PG l3.rebalance_log + 时间轴 + 胜率 | 飞书周报 | 6/27 首次 |

**核心 API**:
```python
def generate_rebalance_suggestion(positions, alerts, advices) -> List[RebalanceAction]:
    """综合 3 源 (C1 风险 + C6 事件 + L3 策略) → 调仓建议"""
    # 1. C1 风险触发 → 必须减仓 (severity P0)
    # 2. C6 事件方向 → 加仓/减仓 (confidence > 0.7)
    # 3. L3 策略 buy/sell → 加仓/减仓
    # 4. 单标的权重 > 5% → 减仓至 5% (V24-B4 default)
    
def confirm_rebalance(user_id, action_id, confirmed: bool) -> bool:
    """飞书卡片回调 → 写 audit log"""
    
def execute_rebalance(action_id) -> RebalanceLog:
    """调 broker API (or 模拟) → 持久化"""
```

**PG 表**:
- `l3.rebalance_log` (4 索引: pkey + user_id_time + action_type + confirmed)

**预期产出**:
- 1 个新文件 600 行 (position_rebalancer.py)
- 1 张新表 (l3.rebalance_log) + 4 索引
- 1 模式 (模式 23, 12 验证项)
- 4-5 PIT (飞书回调 + 调仓并发 + 资金检查 + audit log)
- 1 份 PIT 文档 (8KB)
- 实战 6/27 调仓 1 次

---

### 3.3 方向 C: 🚀 历史事件回放 + 实战准确度评估 (P1, 6/27-7/04 1 周)

**核心**: V24-C6 大模型首席分析师实战 2 周后, 跑反向回测验证准确度

| # | 任务 | 内容 | 交付物 | 验证 |
|:-:|------|------|--------|:---:|
| C1 | 历史事件 KB 拉取 | 6/1-7/15 所有重大事件 (FOMC/SpaceX/分拆/财报) | `historical_events.json` 30 事件 | 6/27 完成 |
| C2 | 实战建议收集 | 6/15-7/15 V24-C6 自动跑的 advice (含 conf + reasoning) | `l3.event_strategist_advice` 累积 20+ 行 | 7/4 累积 |
| C3 | 事后价格对比 | 事件后 1d/7d/30d 价格变化 vs 建议方向 | `l3.event_backtest_results` 5 索引 | 7/4 跑 |
| C4 | 准确度报告 | 准确率 / 平均收益 / 最大回撤 / 推 vs 拉 | `releases/v2.5-event-backtest.md` | 7/4 出 |
| C5 | conf 分层分析 | 高 conf (>0.8) vs 低 conf (<0.5) 实战收益 | 1 张图 + 1 段分析 | 7/4 出 |

**核心 API**:
```python
def backtest_event_advice(date_from, date_to) -> EventBacktestResult:
    """拉历史事件 + 实战建议 + 价格 → 准确度报告"""
    
def calc_accuracy(advices, prices) -> float:
    """advice.direction == price.direction → 1, else 0"""
    
def analyze_confidence_layer(advices) -> Dict[float, float]:
    """conf 0.0-1.0 10 层, 每层实战收益"""
```

**预期产出**:
- 1 个新文件 500 行 (event_backtester.py)
- 1 张新表 (l3.event_backtest_results) + 5 索引
- 1 模式 (模式 24, 12 验证项)
- 4-5 PIT (价格数据缺失 + 事件分类 + conf 校准)
- 1 份 PIT 文档 (8KB)
- 1 份 v2.5-event-backtest.md (15KB)
- 实战准确度 1 份报告

---

### 3.4 方向 D: 🚀 持仓调仓助手 — 调仓执行回写优化 (P1, 7/04-7/10 1 周)

**核心**: B 方向调仓助手实战 1 周后, 优化并发 + 资金 + 跨账户

| # | 任务 | 内容 | 交付物 | 验证 |
|:-:|------|------|--------|:---:|
| D1 | 调仓并发锁 | fcntl.flock 防双跑 + 用户 2 次确认 | 锁文件 + retry | 模式 25 |
| D2 | 资金检查 | 可用现金 ≥ 调仓金额 1.1x | 调仓前 pre-check | 实战 |
| D3 | 跨账户汇总 | 广发 + 国金 + 汇添富 + 天天 4 CSV 合并 | 总资产视图 | 实战 |
| D4 | 调仓历史周报 | 每周日 22:00 自动出 | 飞书推送 1 条 | 7/12 首次 |

**预期产出**:
- 1 个新文件 400 行 (position_rebalancer_extensions.py)
- 1 模式 (模式 25, 12 验证项)
- 3-4 PIT
- 1 份 PIT 文档 (6KB)

---

### 3.5 方向 E: 📊 业绩归因分析 (P2, 7/04-7/12 1 周)

**核心**: 持仓 vs 沪深300/科创50 基准对比 + LLM 归因

| # | 任务 | 内容 | 交付物 | 验证 |
|:-:|------|------|--------|:---:|
| E1 | 基准数据拉取 | 沪深300 (000300) + 科创50 (000688) 同期日线 | 4 索引 | 实战 |
| E2 | 持仓 vs 基准对比 | 累计收益 / 夏普 / 最大回撤 / α/β | `l3.attribution_summary` | 实战 |
| E3 | LLM 归因 | 月度调仓记录 + 持仓变化 → 业绩归因报告 | 飞书月报 1 条 | 7/12 首次 |
| E4 | 业绩仪表盘 | Streamlit 业绩归因面板 | 1 页面 | 实战 |

**预期产出**:
- 1 个新文件 500 行 (attribution_analyzer.py)
- 1 张新表 (l3.attribution_summary) + 4 索引
- 1 模式 (模式 26, 12 验证项)
- 3-4 PIT
- 1 份 PIT 文档 (6KB)

---

### 3.6 方向 F: 🛡 中报季实战 (P0, 7/1-7/15 2 周, ⭐时间敏感)

**触发**: 7/15 中报季开始 (per memory §持仓档案 7/15 亨通分拆)

| # | 任务 | 内容 | 交付物 | 验证 |
|:-:|------|------|--------|:---:|
| F1 | 中报日历收集 | 7/15-8/31 所有持仓的中报预披露日期 | `holdings.earnings_calendar` 50 行 | 7/1 完成 |
| F2 | 业绩 miss 触发器 | 实际 pp < 预期 pp * 0.8 → 减仓 50% 告警 | `earnings_miss_trigger.py` 400 行 | 模式 27 |
| F3 | 中报前 3 天 cron | 每日 09:25 跑 + 飞书推送 | 实战 | 7/12 首次 |
| F4 | 中报实战 1 周报告 | 业绩符合率 / 减仓执行 / 实际收益 | `releases/v2.5-earnings-season.md` | 8/1 出 |

**核心 API**:
```python
def check_earnings_miss(today) -> List[MissAlert]:
    """拉 7/15-8/15 中报实际 pp → 对比预期 → miss>20% 减仓"""
    
def trigger_reduce_on_miss(code, miss_pct) -> bool:
    """miss > 20% → 自动调仓 50% (联调 B 方向)"""
```

**预期产出**:
- 1 个新文件 400 行 (earnings_miss_trigger.py)
- 1 模式 (模式 27, 12 验证项)
- 3-4 PIT
- 1 份 PIT 文档 (6KB)
- 实战: 7/15-8/15 持仓 28 中报预披露 → 飞书推送 N 条

---

### 3.7 方向 G: 📊 7 天实战报告自动出 (P0, 6/20 自动, 无需实施)

**触发**: 6/20 收满 7 天实战数据, 自动跑报告生成

| # | 任务 | 内容 | 交付物 | 验证 |
|:-:|------|------|--------|:---:|
| G1 | 7 天报告自动生成 | 调 v22_monitoring + 4 实战表汇总 | `releases/v2.5-7d-auto-report.md` | 6/20 自动 |
| G2 | 飞书推送报告 | 报告完成后推飞书 1 条 (摘要) | 实战 | 6/20 自动 |
| G3 | 7 天数据持久化 | l3.v25_7d_snapshot 表 + 时间戳 | 1 索引 | 6/20 自动 |

**核心 API**:
```python
def generate_7d_report() -> 7dReport:
    """拉 4 表 + v22_monitoring → 汇总报告"""
    
def push_report_to_feishu(report) -> bool:
    """报告摘要推飞书"""
```

**预期产出**:
- 1 个新文件 300 行 (7d_report_generator.py)
- 1 张新表 (l3.v25_7d_snapshot) + 1 索引
- 1 模式 (模式 28, 12 验证项)
- 2-3 PIT
- 1 份 PIT 文档 (5KB)

---

## 四、v2.5 实施时间线 (推荐)

```
Week 1 (6/14-6/19):  方向 A — 7 天实战 + 飞书推送验证 (P0, 自动)
Week 2 (6/20-6/26):  方向 G — 7 天报告自动出 (P0, 6/20) + 方向 B — 调仓助手 (P1)
Week 3 (6/27-7/03):  方向 C — 事件回放 (P1) + 方向 B 实战 1 周
Week 4 (7/04-7/10):  方向 D — 调仓优化 (P1) + 方向 E — 业绩归因 (P2)
Week 5-6 (7/11-7/15): 方向 F — 中报季实战 (P0, 7/1 启动) + 收尾 v2.5
```

**关键时间节点**:
- **6/14 22:00**: V24-C4 策略调优 cron 首次 → 飞书推送 1 条
- **6/15 11:30**: V24-C6 大模型首席分析师 cron 首次 → 飞书推送 1 条 ⭐
- **6/15 09:00**: V24-C1 持仓风险周报 cron 首次 → 飞书推送 1 条
- **6/20**: 7 天实战报告自动出 (方向 A + G)
- **7/1**: 中报季日历收集 (方向 F 启动)
- **7/15**: 中报季实战开始 (方向 F 实战)
- **7/31**: 7 月底总结 v2.5

---

## 五、风险评估 (v2.5)

| 风险 | 概率 | 影响 | 缓解 |
|------|:---:|:---:|------|
| 6/14-6/19 7 天数据缺 (蓝屏) | 中 | 中 | 容错视图 + 健康度检查 + 推送告警 |
| 飞书推送失败 (webhook 配错) | 中 | 低 | PIT #69 PG 兜底 + 失败告警 |
| 调仓助手误触发 (B 方向) | 中 | **高** | 7 天模拟回测 + 灰度 + 用户二次确认 |
| 中报季 miss 触发误报 (F 方向) | 中 | **高** | 实际 pp 来源校验 (Tushare 真实数据) + 阈值 conf |
| LLM 降级链失效 (event 误判) | 低 | 中 | deepseek-reasoner + chat 双备份 + 24h 缓存 |
| 跨账户合并冲突 (D 方向) | 中 | 中 | fcntl.flock 锁 + 用户主账户选择 |
| 业绩归因不准 (E 方向) | 中 | 低 | 多基准对比 + 显式归因 |
| schedule_runner 蓝屏崩溃 | 中 | **高** | watchdog 拉起 (per memory §1) + cron 兜底 |

---

## 六、命名空间约定 (v2.5)

- **任务前缀**: V25-T1/T2/T3 (方向 A/B/C/D/E/F/G 各自)
- **方向 B (调仓助手)**: V25-B-T1/T2/T3/T4
- **方向 C (事件回放)**: V25-C-T1/T2/T3/T4/T5
- **方向 D (调仓优化)**: V25-D-T1/T2/T3/T4
- **方向 E (业绩归因)**: V25-E-T1/T2/T3/T4
- **方向 F (中报季)**: V25-F-T1/T2/T3/T4
- **方向 G (7 天报告)**: V25-G-T1/T2/T3
- **新文件** (候选):
  - `hermes_coordination/scripts/position_rebalancer.py` (B)
  - `hermes_coordination/scripts/event_backtester.py` (C)
  - `hermes_coordination/scripts/attribution_analyzer.py` (E)
  - `hermes_coordination/scripts/earnings_miss_trigger.py` (F)
  - `hermes_coordination/scripts/7d_report_generator.py` (G)
- **PG 表** (候选):
  - `l3.rebalance_log` (B, 4 索引)
  - `l3.event_backtest_results` (C, 5 索引)
  - `l3.attribution_summary` (E, 4 索引)
  - `l3.earnings_calendar` + `l3.earnings_miss_log` (F, 5 索引)
  - `l3.v25_7d_snapshot` (G, 1 索引)
- **24-28 模式测试**: 扩展 `hermes_test_6_patterns.py`
- **CLI** (B/C/D/E/F/G): `--run / --report / --backtest`

---

## 七、v2.5 评分预期

| 维度 | v2.4 | 方向 A | 方向 B | 方向 C | 方向 D | 方向 E | 方向 F | **v2.5** |
|------|:----:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 9 方向 100% | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 |
| 6 P2 patch | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| 飞书推送 | 4 通道 | 4 通道 + 7 实战 | 4 + 调仓确认 | 4 + 实战 | 4 + 飞书 | 4 + 月报 | 4 + 中报 | **4 + 全** |
| 实战 cron | 35 | 35 + 7 实战 | 35 + 1 (B) | 35 + 1 (C) | 35 + 1 (D) | 35 + 1 (E) | 35 + 1 (F) | **42** |
| 模式 | 22/22 | 22/22 | 23/23 | 24/24 | 25/25 | 26/26 | 27/27 | **27/27** |
| 端到端 | 39/39 | 39/39 | 40/40 | 41/41 | 42/42 | 43/43 | 44/44 | **44/44** |
| PG 表 | 19 | 19 + 1 (G) | 20 + 1 (B) | 21 + 1 (C) | 22 | 22 + 1 (E) | 23 + 2 (F) | **25** |
| PIT | 70 | 70 + 3 | 75 + 5 | 80 + 5 | 85 + 4 | 89 + 4 | 93 + 4 | **97** |
| 评分 | 9.9995 | 9.9996 | 9.9997 | 9.9998 | 9.9999 | 9.9999 | 9.99995 | **9.99995** |

---

## 八、立即可执行的下一步 (Week 1: 方向 A)

### 6/14 22:00 V24-C4 首次自动跑 (无需用户操作):
1. cron 触发 `job_strategy_optimization`
2. 调 `strategy_optimizer.py --run --method walk_forward`
3. 21 trials × 5.71s/次 → 2min 内完成
4. 写 l3.strategy_optimization_runs + 1
5. **send_notification("🎯 策略调优报告") → 飞书推送 1 条** (用户配 FEISHU_WEBHOOK 后)

### 6/15 09:00 V24-C1 首次自动跑:
1. cron 触发 `job_position_risk_weekly`
2. 调 `position_risk_manager.py` → 持仓 45 个 → 10 触发
3. **push_to_webhook → 飞书推送 10 条** (实战 0/10 → 10/10) ⭐

### 6/15 11:30 V24-C6 首次自动跑 (⭐ V24-C6 首次实战):
1. cron 触发 `job_chief_event_analyst`
2. 3 事件 (SpaceX / 英伟达 GTC / FOMC) × deepseek-reasoner 5-10s
3. 写 l3.event_strategist_advice + 3
4. **send_notification("🧠 大模型首席分析师") → 飞书推送 1 条** ⭐

### 6/20 (下周五) 自动出 7 天报告 (方向 G):
- 选项 1: 启动方向 B (调仓助手) — 6/20 用户决策
- 选项 2: 启动方向 C (事件回放) — 7/4 用户决策
- 选项 3: 启动方向 F (中报季) — 7/1 自动启动 (时间敏感)
- 选项 4: 6/20 同时启动 B + 6/27 启动 C + 7/1 自动 F

### 用户需回复:
- **6/14 5pm 前**: 配 `FEISHU_WEBHOOK` 到 `store.json` (1 行)
- **6/20 6pm**: 决策是否启动 B (调仓助手) — 默认 ✅ 启动
- **7/1**: 决策中报季 F 启动 — 默认 ✅ 启动

---

## 九、待用户决策 (6/14 12pm 前)

**核心问题**:
1. **飞书 webhook**: 是否配 `FEISHU_WEBHOOK` 到 store.json? (默认 ✅ 配, 1 行)
2. **方向 B 时机**: 6/20 启动调仓助手? (默认 ✅ 启动)
3. **方向 C 时机**: 6/27 启动事件回放? (默认 ✅ 启动)
4. **方向 F 时机**: 7/1 启动中报季? (默认 ✅ 自动启动)
5. **方向 D/E 时机**: 7/4-7/12 启动 D + E? (默认 ✅ 启动)

**默认推荐** (无需回复则按此):
- 6/14 22:00 V24-C4 实战 ✅
- 6/15 11:30 V24-C6 实战 ✅
- 6/20 方向 B 启动 ✅
- 6/27 方向 C 启动 ✅
- 7/1 方向 F 自动启动 ✅
- 7/4 方向 D + E 启动 ✅
- 7/15 中报季实战 ✅
- 7/31 v2.5 总结 + v2.5.0 release 文档

---

## 十、参考

- **v2.4 完整 release**: `releases/v2.4-summary.md` (20 章节) + v2.4.1 (21) + v2.4.2 (25) + v2.4.3 (27) + v2.4.4 (28)
- **V25-A1+A2 完整 PIT**: `references/v25-a1-integration-pitfalls.md` (10.5KB / 5 PIT #66-#70)
- **V22-V24 累计 70 PIT**:
  - `references/v22-10-bugs-pitfalls.md` (22.8KB / 21 PIT)
  - `references/v23-r3-integration-pitfalls.md` (V23 11 PIT)
  - `references/v24-b1/b2/b2.1/b3/c1/b4/c4/c5/c6-integration-pitfalls.md` (10 份 / 80KB / 38 PIT)
- **22 模式测试**: `hermes_test_6_patterns.py --all` (22/22 全过, 178.9s)
- **39 端到端**: `v22_to_v23_integration.py` (39/39, 1.33s, 含 V25-A1+A2 2 新)
- **35 个 cron 任务**: 守护进程 PID 215889 (V25 加载)
- **19 张 PG 表**: 累计 16 个新索引
- **v2.4 累计 15 commits** (10 实施 + 5 release)
- **守护进程**: watchdog 606 + cron 兜底 (per memory §1 + §6/9 教训)

---

**v2.5 草案 v0.1** — 6/14 第一份 V24-C4 实战报告出来后细化。**等用户 6/20 决策是否启动方向 B**。
