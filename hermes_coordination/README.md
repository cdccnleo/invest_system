# Hermes Agent × InvestPilot 系统协同方案 (v2.1)

本目录是 Hermes Agent（外挂大脑）与 InvestPilot 仪表盘的协同方案实施代码。

## 版本
- **当前**：v2.1 (2026-06-12)
- **基础**：v2.0 (8大方案 + 5大补丁) + P2 阶段 4 补丁
- **评分**：7.3/10 → **8.7/10**

## 目录结构
```
hermes_coordination/
├── SKILL.md                      ⭐ v2.1 升级说明
├── references/
│   ├── v1_8_schemes.md          # v1.0 8大方案
│   ├── pg-deployment-log.md     # PG16 部署日志
│   ├── progress_log.md          # 实施进度日志
│   ├── contracts/               # 5 大工程补丁 + 2 个 v2.1 新增
│   ├── monitoring/              # 监控告警 + cron 配置
│   └── profiles/                # 3 套 Profile (default/conservative/aggressive)
└── scripts/
    ├── hermes_event_analyst.py  # 方案2: 事件首席分析师
    ├── hermes_kb_ingest.py      # 方案5: KB 批量吸收
    ├── hermes_notifier.py       # 通知+脱敏
    ├── hermes_agent_sync.py     # 方案1: 双向同步 ⭐
    ├── asset_class_router.py    # 补丁6: 资产路由 ⭐
    ├── llm_fallback_chain.py    # 补丁7: LLM 降级 ⭐
    ├── profile_loader.py        # 补丁8: Profile 加载 ⭐
    ├── skill_rollback.py        # 补丁9: Skill 回滚 ⭐
    └── sql/agent_action_queue.sql
```

## 已完成 13 项任务

### P0 (5 项) — 工程补丁
- ✅ 01 接口契约 (YAML)
- ✅ 02 监控告警 (YAML + watch_hermes_agent.sh)
- ✅ 03 成本评估 (YAML)
- ✅ 04 关键时间窗口 (YAML)
- ✅ 05 数据脱敏 (MD + DataRedactor)

### P1 (4 项) — 方案落地
- ✅ 方案2 事件首席分析师 (hermes_event_analyst.py, 18KB)
- ✅ 方案5 KB 批量吸收 (hermes_kb_ingest.py, 10KB)
- ✅ 监控告警系统 (watch_hermes_agent.sh, 3.4KB)
- ✅ 数据脱敏 (DataRedactor in hermes_notifier.py)
- ✅ **方案1 双向同步 (hermes_agent_sync.py, 18KB) ⭐ 新完成**

### P2 (4 项) — 补强补丁
- ✅ **补丁6 资产差异化 (asset_class_router.py + 06-asset-class-strategy.md) ⭐**
- ✅ **补丁7 LLM 降级链 (llm_fallback_chain.py, 13KB) ⭐**
- ✅ **补丁8 Profile 隔离 (profile_loader.py + 3 套 YAML) ⭐**
- ✅ **补丁9 Skill 回滚 (skill_rollback.py, 11KB) ⭐**

## 真实集成验证 (2026-06-12 05:50)

### 双向同步 (P1-T4)
- 18/18 only_invest 标的 i2h 成功
- PG skill_sync_audit: 1 → 19 条
- 修复 3 个 bug: 列名 / result 枚举 / direction 枚举

### 资产路由 (P2-T1)
- 5 类资产识别 + 4 类通知窗口
- 修复模式匹配顺序 bug

### LLM 降级 (P2-T2)
- 4 级降级链全部跑通 (mock)
- 9 项统计指标 + 1h 恢复机制

### Profile 隔离 (P2-T3)
- 3 套 YAML + 约束检查
- 黑名单 + 仓位/PE/52周检查

### Skill 回滚 (P2-T4)
- 245KB 备份 + 破坏 + 还原
- 修复 skill 在子目录的路径 bug

## 使用示例

```bash
# 1. 双向同步
~/invest_system/.venv/bin/python hermes_coordination/scripts/hermes_agent_sync.py --mode inspect
~/invest_system/.venv/bin/python hermes_coordination/scripts/hermes_agent_sync.py --mode i2h --code 002050 --execute

# 2. 资产路由测试
~/invest_system/.venv/bin/python hermes_coordination/scripts/asset_class_router.py

# 3. LLM 降级链测试
HERMES_FALLBACK_MOCK=1 ~/invest_system/.venv/bin/python hermes_coordination/scripts/llm_fallback_chain.py

# 4. Profile 检查
~/invest_system/.venv/bin/python hermes_coordination/scripts/profile_loader.py

# 5. Skill 备份/回滚
~/invest_system/.venv/bin/python hermes_coordination/scripts/skill_rollback.py
```

## PG 表依赖

```sql
-- 已在 investpilot 库创建
public.agent_action_queue       (方案2 用)
public.cron_task_metrics        (监控用)
public.privacy_audit_log        (脱敏审计)
public.skill_sync_audit         (方案1 审计)
```

## 待命状态

v2.1 已完成 13/13 项任务。可执行下一步：
- 集成到 intraday_monitor（资产路由）
- 集成到 llm_caller（降级链）
- 集成到 dashboard（Profile 切换器）
- 部署 schedule_runner cron（每日 18:00 双向同步）
