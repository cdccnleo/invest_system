# Hermes Agent × InvestPilot 系统协同方案 v2.3 实施计划

> **基础**: v2.2 (8/8 方案 100% 覆盖, 9.5/10 评分, V22-T3+T4 push 成功)
> **目标**: 补完 8 大方案剩余 partial 缺口 + 4 P2 patch 增量 + 监控实战 7 天
> **创建**: 2026-06-12 08:30

---

## 一、v2.2 完整状态盘点 (实测 2026-06-12)

### 1.1 8 大方案覆盖

| 方案 | v2.0/v2.1 | v2.2 实测 | 状态 |
|------|-----------|-----------|:---:|
| 1. Skill ↔ TAMF 双向同步 | ✅ | ✅ PG audit 19→115+ | 100% |
| 2. 事件首席分析师 | ✅ | ✅ hermes_event_analyst.py | 100% |
| 3. 盘中异动 + Hermes实时解读 | ❌ | ✅ V22-T3 (1 文件 + 集成 + 推送) | 100% |
| 4. Hermes L3 策略顾问 | ❌ | ✅ V22-T4 (L3Advisor 3 方法 + 2 表 + Dashboard) | 100% |
| 5. 批量吸收 AInvest 报告 | ✅ | ✅ hermes_kb_ingest.py | 100% |
| 6. **跨标协同矩阵** | 🟡 | 🟡 **仅 6/7/8 都有 backtest 但 6 缺 portfolio 协同** | **partial** |
| 7. **Web UI ↔ Dashboard** | 🟡 | 🟡 **Dashboard 17 视图在, 双端桥 agent_interface 未在** | **partial** |
| 8. **回测验证闭环** | 🟡 | 🟡 **backtest_engine.py 在, 方案 8 缺 hermes 策略回测入口** | **partial** |

**结论**: 8 大方案 5 完整 + 3 partial (6/7/8)

### 1.2 4 P2 Patch 状态 (实测 2026-06-12)

| P2 Patch | 文件 | 行数 | 状态 | 问题 |
|----------|------|:----:|:---:|------|
| P2-1 资产类型差异化 | asset_class_router.py | 217 | ✅ | 5 类资产 5 阈值, 集成到 intraday_monitor |
| P2-2 LLM 降级链 | llm_fallback_chain.py | 329 | ✅ | 4 级 L1→L2→L3→L4, mock 模式验证 |
| P2-3 跨 Profile 隔离 | profile_loader.py | 183 | ✅ | 3 profile (default/conservative/aggressive) |
| P2-4 Skill 回滚 | skill_rollback.py | 300 | ❌ | **list_backups 缺 positional arg, API 错** |

**结论**: 4 P2 patch 3 完整 + 1 bug (P2-4 需修)

### 1.3 v2.2 端到端验证 (6 大测试模式)

```
✅ 模式 1: Schema-First 验证 (0.1s)
✅ 模式 2: 真实依赖探测 (4.58s)
✅ 模式 3: PG 事务健康检查 (0.02s)
✅ 模式 4: Mock LLM 真实跑通 (0.0s)
✅ 模式 5: 限额状态隔离 (0.0s)
✅ 模式 6: 早退路径 Schema 验证 (0.06s)
通过: 6/6
```

**脚本**: `hermes_coordination/scripts/hermes_test_6_patterns.py --all`

---

## 二、v2.3 三轮任务 (按 ROI 排序)

### 2.1 Round 1 (P0, 1 周内): 修 P2-4 Bug + 补方案 8 回测入口

**目标**: 把 4 P2 patch 100% 修通 + 方案 8 补 1 个入口

#### 任务 V23-R1-T1: 修 P2-4 SkillRollback API (1-2 天)
- **现状**: `SkillBackupManager.list_backups()` 缺 `skill_name` 参数, 调用必崩
- **修复**:
  - 选项 A: 让 `list_backups(skill_name=None)` 支持 "列全部"
  - 选项 B: 新增 `list_all_backups()` 单独方法
- **验收**:
  - [ ] `mgr.list_all_backups()` 返回 N 条历史备份
  - [ ] `mgr.list_backups(skill_name="...")` 返回该 skill 的备份
  - [ ] 端到端: 创建备份 → list → 还原 → 验证

#### 任务 V23-R1-T2: 方案 8 回测验证入口 (3-4 天)
- **现状**: `backtest_engine.py` 在, 但"hermes 生成的策略 → 回测"入口未做
- **新文件**: `hermes_coordination/scripts/hermes_backtest_validator.py`
- **接口**: `validate_hermes_strategy(strategy: dict, period: str) -> dict`
- **流程**:
  1. 接 hermes 生成的策略 dict (来自方案 6 + l3.decision_points)
  2. 调 backtest_engine.run() 回测
  3. 对比基准 (沪深 300)
  4. 写 `l3.strategy_backtest_results` (新表)
  5. 反馈给 hermes memory (强化学习)
- **验收**:
  - [ ] 1 个示例策略 (e.g. 减持信维+加仓黄金) 真实回测
  - [ ] 输出夏普/最大回撤/超额收益 3 指标
  - [ ] 1 张 PG 表 + 端到端测试

---

### 2.2 Round 2 (P1, 2 周内): 补方案 6 跨标协同 + 方案 7 双端桥

**目标**: 6/7 partial → 100% 完整

#### 任务 V23-R2-T1: 方案 6 跨标协同矩阵 (5-7 天)
- **现状**: hermes_event_analyst 有持仓视角, 但**没"事件 → 持仓影响"自动映射**
- **新文件**: `hermes_coordination/scripts/hermes_portfolio_copilot.py`
- **接口**:
  - `analyze_event_impact(event: str) -> dict` — 事件 → 持仓影响热力图
  - `weekly_strategy_review() -> dict` — 每周自动复盘
- **流程**:
  1. 接 AInvest 事件
  2. 拉 29 持仓 (PG portfolio.positions)
  3. 调 hermes skill (skill_match) + LLM 推理
  4. 输出 5 类影响 (直接受益/间接受益/受损/无影响/组合策略)
- **验收**:
  - [ ] 1 个真实事件 (e.g. SpaceX IPO) → 5 持仓影响分析
  - [ ] heatmap 输出 (markdown 表格)
  - [ ] 与 hermes_event_analyst 协同 (事件 → 持仓)

#### 任务 V23-R2-T2: 方案 7 Dashboard ↔ Web UI 双端桥 (5-7 天)
- **现状**: dashboard 17 视图在, 缺"用户点击 dashboard 按钮 → 调 hermes"桥接
- **新文件**: `hermes_coordination/scripts/dashboard_hermes_bridge.py`
- **接口**:
  - `on_dashboard_action(user_action: str, context: dict) -> dict`
- **流程**:
  1. dashboard 按钮触发 (e.g. "深度分析信维通信")
  2. bridge 加载 skill + events
  3. 调 L3Advisor.chat() (v2.2 已实现)
  4. 推送到 dashboard 显示 + 钉钉/企微
- **验收**:
  - [ ] 1 个 dashboard 按钮 → 真实 hermes 调用
  - [ ] 推送完整 (dashboard + 钉钉/企微)
  - [ ] 端到端截图

---

### 2.3 Round 3 (P2, 1 月内): 监控 v2.2 实战 7 天 + v2.3 集成

**目标**: 验证 v2.2 限额/质量/成本真实有效

#### 任务 V23-R3-T1: 监控仪表盘 + 7 天报告 (3-5 天)
- **新文件**: `hermes_coordination/scripts/v22_monitoring_dashboard.py`
- **指标**:
  - 每日 LLM 调用次数 (vs 限额 20/日)
  - 决策点写入数 (l3.decision_points 增长)
  - 异动推送条数 (intraday_hermes_agent)
  - L1 降级次数 / L4 skip 次数
- **验收**:
  - [ ] 1 个 streamlit 页面 (v22 监控)
  - [ ] 7 天数据自动收集
  - [ ] 1 份 7 天报告 (限额够用? 质量? 成本?)

#### 任务 V23-R3-T2: v2.3 集成 (3-5 天)
- **新文件**: `hermes_coordination/scripts/v22_to_v23_integration.py`
- **流程**:
  1. 把 Round 1+2 的新模块集成到 schedule_runner cron
  2. 18:00 + 22:00 cron 触发
  3. 端到端: 启动 → 7 天无人工干预
- **验收**:
  - [ ] cron 全部注册
  - [ ] 7 天自动运行无错误
  - [ ] 评分 9.5 → 9.8 (Round 1+2 全部完成)

---

## 三、PG 表新增 (v2.3)

```sql
-- 方案 8 决策点 (Round 1 T2)
CREATE TABLE IF NOT EXISTS l3.strategy_backtest_results (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    return_pct FLOAT,
    alpha_pct FLOAT,  -- 超额 vs 沪深 300
    sharpe FLOAT,
    max_drawdown FLOAT,
    benchmark TEXT DEFAULT 'CSI300',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_backtest_strategy ON l3.strategy_backtest_results (strategy_name, created_at DESC);

-- v2.2 监控数据 (Round 3 T1)
CREATE TABLE IF NOT EXISTS l3.v22_monitoring (
    id BIGSERIAL PRIMARY KEY,
    metric_name TEXT NOT NULL,  -- llm_call_count / decision_writes / push_count
    metric_value FLOAT,
    metric_date DATE DEFAULT CURRENT_DATE,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_monitoring_date ON l3.v22_monitoring (metric_date DESC);
```

---

## 四、风险评估 (v2.3)

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|-----|
| P2-4 修复引入新 bug | 中 | 低 | 6 模式测试 + 端到端 |
| 回测策略数据不准 | 中 | 中 | 用历史 1 年数据对比, 与 backtest_engine 现成数据交叉验证 |
| 方案 6 LLM 成本失控 | 中 | 高 | 限额 20/日 + 与方案 3 共用 DailyQuota |
| Dashboard 桥接 UI 复杂 | 高 | 中 | 最小可用版本: 1 个按钮 + 1 个对话框 |
| 7 天监控数据缺失 | 中 | 低 | 自动 cron 收集 + 失败重试 |

---

## 五、v2.3 vs v2.2 评分预期

| 维度 | v2.2 (现状) | v2.3 目标 | 提升 |
|------|------------|-----------|:----:|
| 8 大方案覆盖 | 8/8 (含 3 partial) | **8/8 完整** | 0 (但 partial → full) |
| 4 P2 patch 完整度 | 3/4 | **4/4** | +1 |
| 回测验证能力 | 0/10 | **7/10** | +7 |
| 跨标协同 | 4/10 | **8/10** | +4 |
| 双端桥接 | 3/10 | **7/10** | +4 |
| 监控可观测性 | 6/10 | **9/10** | +3 |
| **总分** | **9.5/10** | **9.8/10** | **+0.3** |

---

## 六、命名空间约定 (v2.3)

- **任务前缀**: V23-R1/R2/R3-T1/T2
- **新文件**:
  - `hermes_backtest_validator.py` (R1-T2)
  - `hermes_portfolio_copilot.py` (R2-T1)
  - `dashboard_hermes_bridge.py` (R2-T2)
  - `v22_monitoring_dashboard.py` (R3-T1)
  - `v22_to_v23_integration.py` (R3-T2)
- **PG 表**: `l3.strategy_backtest_results` + `l3.v22_monitoring`
- **6 模式测试**: 扩展 `hermes_test_6_patterns.py` 加新场景

---

## 七、执行时间线

```
Week 1 (R1): P0 — 修 P2-4 + 方案 8 回测
Week 2-3 (R2): P1 — 方案 6 跨标 + 方案 7 双端
Week 4 (R3): P2 — 监控 + 集成
```

**预算**:
- R1: 4-5 天 (1-2 + 3-4)
- R2: 10-14 天 (5-7 + 5-7)
- R3: 6-10 天 (3-5 + 3-5)
- **总计**: 20-29 天 (1 个月)

---

## 八、立即可执行

**下一步**: V23-R1-T1 (修 P2-4 SkillRollback)
- 预计 1-2 天
- 真实 bug (list_backups 缺 arg)
- 6 模式测试新增场景

**确认启动**: 请给出"启动 R1" 指令。

---

## 九、v2.3 借鉴的 12 教训 (PIT #1-12)

| # | 教训 | 来源 |
|---|------|------|
| 1-10 | 10 真实 bug (V22-T3+T4) | v22-10-bugs-pitfalls.md |
| 11 | `_HERMES_QUOTA_T4` 实际指向 `/tmp/hermes_llm_quota.json` 不是 `intraday_hermes_quota.json` | 模式 5/6 修复 |
| 12 | `SkillBackupManager.list_backups` 缺 positional arg | 4 P2 patch 测试 |

**v2.3 强制**: 任何新代码 **先 6 模式测试** + **3 轮端到端** 才宣称完成。
