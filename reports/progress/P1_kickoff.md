# InvestPilot P1 阶段 — 启动会纪要 & 实施计划

**会议日期**: 2026-05-31
**参与人**: Hermes Agent（开发执行）、用户（决策审批）
**阶段**: P1 — 性能达标 + 体验升级（第 1-4 周，24 人天）
**前置条件**: P0 阶段已完成（M1 里程碑 2026-05-31 达成）

---

## 一、P1 阶段目标

| 目标 | 量化指标 | 完成时间 |
|------|---------|:---:|
| 并行处理 | TAMF 更新耗时从 5 分钟降至 < 2 分钟 | 第 1 周末 |
| LLM 优化 | API 调用次数减少 50%+，Token 消耗减少 20%+ | 第 2 周末 |
| 仪表盘增强 | 排序/筛选/导出/自动刷新/指标卡片 | 第 2 周末 |
| 移动端适配 | PWA 可安装，手机端布局正常 | 第 3 周末 |
| 数据库备份 | 自动全量+增量备份，恢复验证通过 | 第 3 周末 |
| Agent 集成 | RouterAgent 注入主分析管线 | 第 4 周末 |

## 二、任务分解与排期

| 周次 | 任务 | 人天 | 交付物 |
|:---:|------|:---:|------|
| 第 1 周 | P1-P1: 并行处理激活 | 2 | tamf_updater.py 并行化 + schedule_runner.py 并行采集 |
| 第 1-2 周 | P1-P2: LLM 调用性能优化 | 5 | 语义缓存 + 批处理 + Token 压缩增强 |
| 第 2 周 | P1-U1: 仪表盘交互增强 | 4 | 排序/筛选/导出/刷新/指标卡片 |
| 第 2-3 周 | P1-U2: 移动端适配 | 5 | manifest.json + service worker + 响应式 CSS |
| 第 3 周 | P1-S1: 数据库自动备份 | 3 | backup_manager.py + 定时备份任务 |
| 第 3-4 周 | P1-F3: Agent 接口层集成 | 5 | RouterAgent 注入 run_analysis.py |

## 三、资源分配

| 资源 | 说明 | 状态 |
|------|------|:---:|
| 开发 | Hermes Agent（全职） | ✅ 就绪 |
| 审核 | 用户（关键节点 Review） | ✅ 就绪 |
| PostgreSQL | investpilot 数据库 | ✅ 连接可用 |
| 测试框架 | pytest（124 tests 基线） | ✅ 就绪 |

## 四、风险评估

| 风险ID | 风险 | 概率 | 影响 | 应对 |
|:---:|------|:---:|:---:|------|
| R1 | 并行处理导致数据库连接池耗尽 | 中 | 中 | max_workers=4，连接池配置 |
| R2 | LLM 批处理降低分析精度 | 中 | 中 | A/B 对比测试 |
| R3 | 移动端适配效果不佳 | 高 | 低 | 降级为仅桌面端优化 |
| R4 | Agent 集成影响现有分析管线 | 中 | 中 | 分支开发 + feature flag |

## 五、进度跟踪

- 日更新: [PROGRESS_TRACKER.md](file:///c:/PythonProject/invest_system/reports/progress/PROGRESS_TRACKER.md)
- 周报告: reports/progress/weekly/week-01.md ~ week-04.md
- Phase 总结: reports/progress/phase-summary/P1-completion.md

---

> 会议决议：P1 阶段正式启动，按计划执行。第一项任务 P1-P1 并行处理激活立即开始。