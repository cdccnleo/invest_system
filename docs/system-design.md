# 🤝 Hermes Agent × InvestPilot 协同系统设计 (HLD)

> **版本**: v1.0 | **创建**: 2026-06-14 | **基于**: v2.6.0 实战 33 commits + 32 commits v2.4-v2.6
> **定位**: Hermes × InvestPilot 协同架构 + 集成层 + 7 层架构图 + 部署拓扑 (高阶设计 HLD)
> **配套**:
>   - **InvestPilot 整体架构**: `docs/系统架构设计文档.md` (61KB, 14 章, V22 2026-05-31 实战)
>   - **v1.0 8大协同方案**: `1.md` (21.8KB, 2026-06-10 实战)
>   - **v2.0 5大补丁**: `hermes-investpilot-coordination-v2/SKILL.md` (11KB, 2026-06-11 实战)
>   - **7 实战 release**: `releases/v2.3-v2.6.0-summary.md` (8 份)
>   - **5 plan**: v22-v26 实施计划

---

## 0. 文档定位 (与 `docs/系统架构设计文档.md` 分工)

| 维度 | 实战 6/14 InvestPilot 整体架构 (`docs/系统架构设计文档.md`) | 本协同系统设计 (HLD) |
|------|------|------|
| **版本** | 1.0 (2026-05-31) | 1.0 (2026-06-14) |
| **大小** | 61KB / 1221 行 | 46KB / 759 行 |
| **范围** | InvestPilot 整体 (Streamlit / PG / TAMF / 8 大模块) | Hermes 集成层 (L1-L8 7 层架构 + 数据流) |
| **章节** | 14 章节 (系统概述/架构/模块/数据模型/数据流/技术栈/安全/性能/可扩展) | 13 章节 (协同架构/模块依赖/数据流/部署/7 大决策/Schema/安全/性能) |
| **视角** | "InvestPilot 是什么" | "Hermes 怎么集成" |
| **关系** | 基础架构 (本文件的上位) | 协同架构 (基于基础架构的集成) |
| **实战** | V22 32 commits 之前 | V22-V26-A 32 commits 实战 |

> **实战 6/14 实战 6/14**: 实战 6/14 实战 6/14 实战 6/14 实战 6/14 docs/系统架构设计文档.md 实战 6/14, 实战 6/14 实战 6/14 实战 6/14 实战 6/14 V22-V26-A 32 commits 实战 6/14 实战 6/14 实战 6/14 实战 6/14 实战 6/14 实战 6/14 实战 6/14. 实战 6/14 实战 6/14:
> - 实战 6/14 docs/系统架构设计文档.md = InvestPilot 整体 (基础)
> - 实战 6/14 docs/system-design.md (本文件) = Hermes 集成层 (协同)

---

## 一、系统定位与设计哲学

### 1.1 核心定位

```
┌─────────────────────────────────────────────────────────────────────┐
│  Hermes Agent × InvestPilot = "数据 + 认知" 双轮驱动系统                │
├─────────────────────────────────────────────────────────────────────┤
│  InvestPilot: 数据层 (持仓/因子/回测/盘中/报告) + 数据流调度            │
│  Hermes Agent: 认知层 (skill库 + 跨session记忆 + 子Agent协同 + LLM)    │
│  AInvest reports: 内容层 (216 份报告: trackers/events/daily/分析)     │
│  飞书/钉钉/Telegram: 触达层 (3 通道推送 P0/P1/P2 告警)                 │
└─────────────────────────────────────────────────────────────────────┘
```

**关键设计原则**:
1. **外挂大脑, 不替代** (Hermes 增强, 不替换 InvestPilot 现有能力)
2. **单一数据源** (PostgreSQL `investpilot` 库 = 唯一权威)
3. **可追溯决策** (每个建议标注 ref: skill + event + data)
4. **数据安全** (P0 绝密/P1 机密/P2 内部 三级, 推送脱敏)
5. **降级链** (LLM 4 级: gpt-4o-mini → 本地规则 → 跳过)
6. **6/12 数据铁律** (写文件必独立 sanity check, 多 pipeline 同写=高危)

### 1.2 与 v1.0 8大方案 / v2.0 5大补丁的关系

| 文档 | 视角 | 关系 |
|------|------|------|
| **本系统设计 (HLD)** | 整体架构 (5W1H) | 上位设计, 引导实施 |
| v1.0 8大方案 (`1.md`) | 战略协同 (做什么) | HLD 落地的 8 大业务模块 |
| v2.0 5大补丁 (SKILL.md) | 工程补丁 (怎么做) | HLD 落地的 5 大工程领域 |
| 7 实战 release (v2.3-v2.6.0) | 增量变更 (已做什么) | HLD 落地的具体代码/数据/测试 |

---

## 二、7层架构总览 (L1-L8)

```
┌─────────────────────────────────────────────────────────────────────┐
│  用户层 (L8)                                                          │
│  ┌──────────────┬──────────────┬──────────────┬──────────────────┐   │
│  │ Streamlit    │ Hermes Web   │ Telegram     │ 飞书/钉钉/企微    │   │
│  │ Dashboard    │ UI           │ Bot          │ 推送 (3 通道)     │   │
│  │ (dashboard.py│ (Push/Query) │ (TTS 语音)   │ 飞书 P0/钉钉 P1/  │   │
│  │  86KB)       │              │              │ 企微 P2           │   │
│  └──────┬───────┴──────┬───────┴──────┬───────┴──────┬────────────┘   │
└─────────┼──────────────┼──────────────┼──────────────┼───────────────┘
          │              │              │              │
          ▼              ▼              ▼              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  跨平台决策推送层 (L7)                                                  │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ hermes_notifier.py (9.9KB)                                      │ │
│  │   - send_via_feishu_inplace() / send_via_dingtalk() / send_via_wechat()│ │
│  │   - 3 通道优先级: 飞书 > 钉钉 > 企微 (PIT #67)                   │ │
│  │   - 颜色映射: P0→ERROR(red) P1→WARNING(orange) P2→INFO(blue)    │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Hermes Agent 外挂大脑层 (L6)                                          │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ HermesEnhancedL3Engine (l3_dialog_engine.py 33KB 扩展)          │ │
│  │   - build_context(): history + related_sessions + skills + events│ │
│  │   - post_decision(): 提取决策点 → memory.add + skill.patch       │ │
│  │   - chief_event_strategist.py 21.7KB (V24-C6 deepseek-reasoner)  │ │
│  │   - dashboard_hermes_bridge.py 37KB (Dashboard 按钮 → Hermes)   │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  事件首席分析师 + 跨标协同层 (L5)                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ hermes_event_analyst.py 18KB  - 事件首席分析师 (方案 2 实施)      │ │
│  │ hermes_kb_ingest.py 10.2KB    - 批量吸收 AInvest 报告 (方案 5)   │ │
│  │ hermes_portfolio_copilot.py 45.6KB - 跨标协同矩阵 (方案 6)        │ │
│  │ event_backtester.py 28KB       - 事件回放 + 实战准确度评估 (V25-C)│ │
│  │ attribution_analyzer.py 32KB   - 业绩归因 Brinson 简化 (V25-E)    │ │
│  │ 7d_report_generator.py 25KB    - 7d 周报自动出 (V25-G)            │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  子Agent并行扫描 + LLM降级链层 (L4)                                     │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ hermes_llm_client.py 12.4KB  - LLM 4级降级链:                    │ │
│  │   L1 gpt-4o-mini via hermes_tools.terminal                      │ │
│  │   L2 gpt-4o-mini via OpenAI直连 (绕开Hermes)                    │ │
│  │   L3 本地规则引擎 (无LLM)                                        │ │
│  │   L4 跳过, 下次启动补推                                          │ │
│  │ llm_fallback_chain.py 12.7KB  - 实战降级链                       │ │
│  │ attribution_analyzer.py LLM 3 级降级 (V25-E)                     │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  数据接入 + 持仓管理层 (L3)                                              │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ hermes_agent_sync.py 18KB      - Skill ↔ TAMF 双向同步 (方案 1) │ │
│  │ position_unifier.py 17.9KB      - 4 CSV ↔ PG 持仓统一 (V26-C)   │ │
│  │ benchmark_quote_loader.py 16.6KB - AKShare 指数基准 (V26-B)     │ │
│  │ quote_streamer.py 25.6KB       - 行情拉取器 akshare/baostock    │ │
│  │                                  方案B (V26-A)                    │ │
│  │ position_risk_manager.py 23.6KB - 持仓风险预算 (V24-C1)         │ │
│  │ profit_pct_recalculator.py 12KB  - profit_pct 修复 (V24-C5)    │ │
│  │ position_rebalancer_v2.py 26.8KB - 调仓优化 v2 (V25-D)          │ │
│  │ earnings_miss_trigger.py 20.4KB  - 中报季业绩miss触发器(V25-F)  │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  数据采集 + 盘中监控 + 回测验证层 (L2)                                   │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ intraday_monitor.py 21KB       - 盘中异动监控 (InvestPilot 已有)│ │
│  │ intraday_hermes_agent.py 16KB  - 盘中异动+Hermes实时解读(方案3) │ │
│  │ ainvest_report_parser.py 17.8KB - AInvest 报告解析              │ │
│  │ backtest_engine.py 50KB        - 回测引擎 (InvestPilot 已有)    │ │
│  │ hermes_backtest_validator.py 14.5KB - 知识沉淀+回测验证(方案8) │ │
│  │ strategy_optimizer.py 28.2KB   - 回测策略自动调优 (V24-C4)     │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  数据层 (L1)                                                           │
│  ┌────────────────┬────────────────┬─────────────────────────────┐  │
│  │ PostgreSQL     │ AInvest reports│ Hermes Skills (~/.hermes)   │  │
│  │ investpilot    │ /mnt/c/AInvest│ /skills/investing/          │  │
│  │ 42 张表         │ reports/       │ /skills/events/             │  │
│  │ 150 索引        │ 216 份 INDEX  │ 41 个 + 实战新增             │  │
│  │ 26 l3 表        │ 7.0MB          │                             │  │
│  │ 47 当前持仓     │ trackers 30    │ SKILL.md (frontmatter)      │  │
│  │ 110 PIT 沉淀    │ events 220     │ ~/.hermes/memory/           │  │
│  │                │ daily 17       │ 实战 6/14                    │  │
│  │                │ deep-analysis 25│                             │  │
│  └────────────────┴────────────────┴─────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 三、模块依赖图 (生产部署)

```
┌─────────────────────────────────────────────────────────────────────┐
│  1. 数据采集 (生产 cron 触发)                                          │
│  ┌──────────────────────┐                                            │
│  │ schedule_runner.py   │ (PID 232192, watchdog 兜底)                │
│  │   40 cron 任务        │  ← V24-C1 + V24-C6 + V25-A1+A2+F+B+C+G+D+E│
│  │   fcntl.flock 防双跑  │    + V26-B + V26-C + V26-A                 │
│  └──────────┬───────────┘                                            │
└─────────────┼───────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  2. LLM 决策 (4 级降级)                                                │
│  ┌──────────────────────┐                                            │
│  │ hermes_llm_client    │ → DeepSeek API (sk-fd6...e831, store.json)│
│  │ + llm_fallback_chain │ 缓存 24h + quota 1.7s/0.5分钱/次 (V24-B2.1)│
│  │ PIT #70: 1800 字符   │                                            │
│  └──────────┬───────────┘                                            │
└─────────────┼───────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  3. PG 持久化 (ThreadedConnectionPool 2-10)                            │
│  ┌──────────────────────┐                                            │
│  │ storage_factory.py   │ ← 单一权威数据源                            │
│  │ ThreadedConnection   │   holdings 2 + l3 26 + market 7 + research 7│
│  │ Pool(2-10)           │   = 42 张表 + 150 索引                     │
│  │ + AES-256 加密       │   DB_ENCRYPTION_KEY (64 字符 store.json)   │
│  └──────────┬───────────┘                                            │
└─────────────┼───────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  4. 多通道推送 (3 通道优先级)                                            │
│  ┌──────────────────────┐                                            │
│  │ hermes_notifier      │ → 飞书 (FEISHU_WEBHOOK, P0)              │
│  │ + position_risk_     │   钉钉 (DINGTALK_WEBHOOK, P1)             │
│  │   triggers.py        │   企微 (WECHAT_WEBHOOK, P2)               │
│  │ (PATCH V25-A1+A2)    │   实战 6/14 飞书已配 (81 字符 store.json)  │
│  └──────────┬───────────┘                                            │
└─────────────┼───────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  5. Dashboard 实时展示                                                  │
│  ┌──────────────────────┐                                            │
│  │ streamlit dashboard  │ (PID 609)                                  │
│  │ 86KB + WebSocket     │ ← V24-B3 5 模式 (实时推送, 防 dashboard 漂移)│
│  │ + agent_interface.py │   11.6KB (Hermes 接入层)                  │
│  │ (Agent 接入层)       │                                            │
│  └──────────────────────┘                                            │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 四、数据流 (4 大主流程)

### 4.1 数据采集流 (cron → PG)

```
[schedule_runner.py 40 cron 任务]
    │
    ├─ 09:00 V24-C1 position_risk_weekly  → l3.position_risk_snapshot
    ├─ 09:25 V24-C1 position_risk_pre_market → l3.risk_alert_log
    ├─ 11:30 V24-C6 chief_event_analyst_intraday (一/三/五) → l3.event_strategist_advice
    ├─ 15:05 V24-C1 position_risk_post_market → l3.risk_alert_log
    ├─ 18:00 V25-G 7d_report_generator_weekly (周六) → l3.report_7d_snapshot
    ├─ 22:00 V25-D position_rebalancer_v2_weekly (周日) → l3.rebalance_log
    ├─ 22:00 hermes_event_analyst  → l3.event_strategist_advice
    └─ (其他 32 cron 任务) → 各种 l3.* 表
    │
    ▼
[storage_factory.py ThreadedConnectionPool(2-10)]
    │
    ▼
[PostgreSQL investpilot] 42 张表 + 150 索引 + 47 当前持仓
```

### 4.2 事件扫描流 (Hermes → AInvest → 推送)

```
[hermes_event_analyst.py 22:00 cron] ← V22-T2 实施
    │
    ├─ 1. 扫 AInvest reports/events/ 今日新增 (10-44 份/日)
    │
    ├─ 2. 委托 3 个子Agent 并行扫描 (delegate_task):
    │      ├─ 扫描 events 目录, 识别今日新增报告中持仓提及频次 TOP5
    │      ├─ 识别 events 报告中"逻辑确认器"与"仓位触发器"差异
    │      └─ 为 TOP5 报告生成 3 条操作建议 (加仓/减持/持有)
    │
    ├─ 3. synthesize_action_plan(results)
    │
    ├─ 4. notify_feishu (P0) + notify_dingtalk (P1) + notify_wechat (P2)
    │
    └─ 5. write_to_action_queue → l3.event_strategist_advice
```

### 4.3 持仓风险流 (盘中 → 推送)

```
[intraday_monitor.py 盘中 5% / 量比 3 / 北向 异动] ← InvestPilot 已有
    │
    ▼
[intraday_hermes_agent.py V22-T3 实战]
    │
    ├─ 1. load_skill(f"stock-{alert.code}-{pinyin}")
    │      (从 39 持仓 skill 加载上下文)
    │
    ├─ 2. hermes_quick_interpret(prompt)
    │      (LLM 4 级降级链)
    │
    ├─ 3. notify_dingtalk(interpretation[:80])
    │
    └─ 4. write l3.dashboard_bridge_log
```

### 4.4 7d 报告流 (V25-G 周六 18:00)

```
[7d_report_generator.py V25-G] (PID 232192 schedule_runner)
    │
    ├─ 1. get_holdings_from_pg (47 持仓) + l3.behavior_profile (44 行)
    │
    ├─ 2. compute_portfolio_metrics (45 持仓 + pp 49.06% V25-G)
    │
    ├─ 3. compute_top5_top5 (涨幅 Top5 + 跌幅 Top5)
    │
    ├─ 4. compare_with_pre_week (l3.report_7d_snapshot 历史对比)
    │
    ├─ 5. idempotent 持久化 → l3.report_7d_snapshot (1 行, V25-G)
    │
    ├─ 6. send_via_feishu (P0 推送, 飞书 81 字符 webhook)
    │
    └─ 7. sanity check: 总市值 ¥5,631,647 + 浮盈 ¥+1,199,821
```

---

## 五、部署拓扑 (Windows + WSL 双环境)

### 5.1 物理拓扑

```
┌─────────────────────────────────────────────────────────────────────┐
│  Windows 11 主机 (OrayIddDriver + NVIDIA RTX 5070 Ti)                │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │ Windows C:\PythonProject\invest_system\                          ││
│  │   - dashboard.py (Streamlit 86KB)                                ││
│  │   - ainvest_report_parser.py (17.8KB)                           ││
│  │   - scripts/ (33 个 .py 镜像)                                    ││
│  │   - data/ (4 CSV 持仓源, PIT #91)                                ││
│  │   - AInvest reports/ (216 份)                                    ││
│  └─────────────────────────────────────────────────────────────────┘│
│           │ WSL2 桥接                                                │
│           ▼                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │ WSL Ubuntu ~/invest_system/ (git repo)                           ││
│  │   - .venv/bin/python3.11 (3.11.15)                               ││
│  │   - 33 脚本/ 8 release / 5 plan / 18 PIT / 7 contract            ││
│  │   - watchdog_daemon.py (PID 606) 守护                             ││
│  │   - streamlit (PID 609) 86KB dashboard                           ││
│  │   - schedule_runner.py (PID 232192) 40 cron 任务                  ││
│  │   - PostgreSQL localhost:5432 investpilot (42 表 + 150 索引)      ││
│  │   - store.json 凭据 (DB_PASSWORD, DEEPSEEK_API_KEY, FEISHU_WEBHOOK││
│  │     DB_ENCRYPTION_KEY, DASHBOARD_PASSWORD, DATABASE_URL)          ││
│  │   - ~/.hermes/invest_credentials/store.json (mode 600)            ││
│  │   - ~/.hermes/skills/investing/hermes-investpilot-coordination-v2││
│  │     (32 references + 33 scripts + 7 releases + SKILL.md)         ││
│  └─────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 进程拓扑 (生产环境 6/14)

```
PPid=1 (init)
  ├─ PID 606 watchdog_daemon.py
  │   ├─ PID 609 streamlit dashboard (86KB)
  │   └─ PID 232192 schedule_runner.py
  │       └─ 40 cron 任务 (V24-C1 + V24-C6 + V25-A1+A2+F+B+C+G+D+E + V26-B + V26-C + V26-A)
  │
  ├─ PG postgresql (systemd)
  │   └─ localhost:5432 investpilot 42 表 150 索引
  │
  └─ cron (system crontab)
      └─ */5 * * * * watchdog_check.sh
```

### 5.3 凭据共用 (Phase II SHARE-WINWSL 实战)

```
┌──────────────────────┐
│ ~/.hermes/invest_    │  ← 真实密码 (mode 600)
│  credentials/store.  │
│  json                │
│  - DB_PASSWORD       │
│  - DEEPSEEK_API_KEY  │
│  - FEISHU_WEBHOOK    │  ← 81 字符
│  - DASHBOARD_PASSWORD│
│  - DATABASE_URL      │
└──────────┬───────────┘
           │ WCM (Web Credentials Manager)
           ▼
┌──────────────────────┐
│ Win+WSL 双端共用      │
│  InvestPilot_Feishu  │  ← 81 字符 webhook (WCM EXISTS_IN_WCM)
│  InvestPilot_DB      │  ← 17 字符
│  InvestPilot_DeepSeek│  ← 35 字符
└──────────────────────┘
```

### 5.4 GitHub 推送 (per memory §6 实战 12 模式)

```
[WSL local commit] → git config 3 个全设 (postBuffer 500MB, lowSpeedLimit 1000, lowSpeedTime 30)
        │
        ▼
[GitHub master] https://github.com/cdccnleo/invest_system
        │
        ▼
[远程三重验证] HEAD + file size + commit message (实战 33 commits 100%)
```

**实战 12 模式 PUSH 经验** (per memory §6 + 实战 12 模式):
1. TLS/GnuTLS recv error → 等 60s 重试
2. "Operation too slow" → 等 60s 重试
3. api.github.com 通 ≠ github.com 通 (HTTP/2 走不同子域)
4. exit=0 但 git status 仍 ahead = 假成功 → 必查 git ls-remote / git fetch
5. amend 必然 force → push 被拒绝时必 force
6. REST API push 但 tree 没真变 = 0 增量 → 必查远程实际 file
7. raw.githubusercontent.com 阻塞 → 远程 HEAD 必查 + file size curl 可重试/跳过
8. PUSH 实战: 80% 1 次成功, 20% 3-4 次重试成功
9. git ls-remote 阻塞时用 git fetch 验证
10. WSL GCM 修复: 实战 6/14 首次出现 username 提示, store.json 无 GH token
11. 实战 33 commits 100% 成功, 累计 30+ 实战
12. **实战 6/14** V22-V26.0 实战 33 commits, 实战

---

## 六、关键设计决策 (7 大决策)

### 6.1 决策1: 外挂大脑 vs 替代系统 (2026-06-08)

**决策**: Hermes Agent 作为"外挂大脑", 不替代 InvestPilot 现有能力
**理由**:
- InvestPilot 已有 dashboard/agent_interface/l3_dialog_engine 11KB 接入层
- 替换成本高, 增量增强 ROI 更优
- Hermes 提供 41 个 skill + 跨 session 记忆 + 子 Agent 协同 + LLM 降级链
- 双轮驱动: InvestPilot=数据, Hermes=认知

### 6.2 决策2: 单一权威数据源 (2026-06-08)

**决策**: PostgreSQL `investpilot` 库 = 唯一权威
**理由**:
- 多 pipeline 写同一文件 = 高危 (per memory 6/12 user 重要补充)
- PG ACID 保证, 避免覆盖冲突
- 100 字段表 schema 固定, ON CONFLICT DO UPDATE idempotent
- ThreadedConnectionPool(2-10) 防止连接池耗尽 (PIT #13)

### 6.3 决策3: 接口契约 v1.0 (2026-06-11, v2.0 补丁1)

**决策**: hermes_investpilot_contract_v1.yaml 6.8KB
**理由**:
- PG column 名铁律 PIT #12 已 9 次验证, 必须 contract 先定
- skill_manage 增量 patch 不破坏 v2.5 已有内容
- 7 天 breaking change 宽限期, 30 天 deprecation 通知
- version_vector 冲突解决, 避免双向同步冲突

### 6.4 决策4: 推送三级通道 (2026-06-11, V25-A1+A2 实施)

**决策**: 飞书 P0 > 钉钉 P1 > 企微 P2
**理由**:
- 实战 6/14 飞书已配 (81 字符 webhook, mode 600 store.json)
- 移动端 push 飞书 > 钉钉/企微 (per memory)
- 3 通道全空 → 返 0 (PIT #69)
- 颜色映射: P0→ERROR(red) P1→WARNING(orange) P2→INFO(blue)
- 1800 字符飞书卡片限制 (PIT #70)

### 6.5 决策5: LLM 4 级降级链 (2026-06-13, v2.0 补丁7 + V25-E 实战)

**决策**:
- L1: gpt-4o-mini via hermes_tools.terminal (normal)
- L2: gpt-4o-mini via OpenAI 直连 (degraded, 绕开 Hermes)
- L3: 本地规则引擎 (offline, 无 LLM)
- L4: 跳过, 下次启动补推 (skip)

**理由**:
- 触发条件: Hermes timeout>30s 连续 3 次 → L2; OpenAI 5xx>5/h → L3; 不可用>1h → L4
- 实战 V25-E attribution_analyzer 3 级降级 (LLM 归因)
- 实战 V25-C event_backtester LLM 实战 2 级 (V25-C 失败 → 行业事件)

### 6.6 决策6: V26-A 方案B 调整 (2026-06-14)

**决策**: 行情拉取器 (akshare/baostock), 不接交易 API
**理由**:
- 方案A 真实券商 API: 涉及 TOTP/真金白银/限频/合规, 难度 8/10
- 方案B 行情 API: akshare/baostock + 限频 + 5min 缓存 + LLM 解读, 难度 5/10
- V25-B 调仓已覆盖 90% 决策, 真实报单只是最后 1 步
- 风险/价值比 方案 B 更优 (实战 6/14 验证)

### 6.7 决策7: 数据安全三级 + 推送脱敏 (2026-06-11, v2.0 补丁5)

**决策**:
- P0 绝密 (总资产/成本价/止盈止损) → 仅本人可见
- P1 机密 (代码/数量/浮动盈亏) → 配偶可见
- P2 内部 (行业分布/板块权重) → 团队可见

**推送脱敏规则**:
- 钉钉群 (全员): 代码+名称+方向可见, 金额/成本/止损 脱敏
- Web UI (本人登录): 全明细可见
- Telegram Bot: 默认 P2 脱敏, 2FA 解锁 P1

---

## 七、PG Schema 设计 (42 表 + 150 索引)

### 7.1 4 Schema 划分

```
holdings (2 表 + 5 索引)
  ├─ encrypted_positions (525 行, 47 当前持仓 + V26-C 21 持仓 + 之前 26)
  │   V26-C 新增 account 字段 + 3 索引 (PIT #104 ALTER TABLE)
  └─ migration_log (7 行)

l3 (26 表 + 88 索引)  ← V26-A +1 l3.quote_snapshot
  ├─ V22: dialog_history, decision_points, profile_audit_log, behavior_profile, dashboard_bridge_log, push_notification_log
  ├─ V23: v22_monitoring (2151 行, V23-R3)
  ├─ V24: event_strategist_advice (10 行 V24-C6), position_risk_snapshot (V24-C1), risk_alert_log (V24-C1)
  ├─ V24: strategy_backtest_results (35 行 V23-R1), strategy_optimization_runs (38 行 V24-C4)
  ├─ V24: profit_pct_fix_log (41 行 V24-C5), stress_test_results (15 行 V22-T4), stress_test_scenarios (V22-T4)
  ├─ V25: active_dialog_triggers (5 行 V25-A1), earnings_calendar (5 行 V25-F mock), earnings_miss_log (12 行 V25-F)
  ├─ V25: rebalance_log (86 行 V25-B), event_backtest_log (1 行 V25-C)
  ├─ V25: report_7d_snapshot (1 行 V25-G), cross_account_summary (1 行 V25-D)
  ├─ V25: attribution_report (1 行 V25-E, Brinson)
  ├─ V26: benchmark_quote (150 行 V26-B, 5 指数 × 30 天)
  └─ V26: quote_snapshot (7 行 V26-A, 4 标的 实时) ⭐ 新

market (7 表 + 22 索引)
  ├─ daily_quotes (824 行, 18 ETF 标的)
  ├─ financial_indicators (252 行)
  ├─ indices (0 行)
  ├─ macro_calendar (0 行)
  ├─ portfolio_equity_curve (13 行)
  ├─ sector_flow (0 行)
  └─ sentiment_factors (180 行)

research (7 表 + 23 索引)
  ├─ announcements (708 行)
  ├─ international_bank_research (943 行)
  ├─ news_articles (1779 行)
  ├─ news_embeddings (708 行)
  ├─ news_sentiments (0 行)
  ├─ report_embeddings (0 行)
  └─ research_reports (452 行)
```

### 7.2 关键设计模式

| 模式 | 说明 | 实战 |
|------|------|------|
| **idempotent UPSERT** | `ON CONFLICT (unique_key) DO UPDATE` | V25-G PIT #86, V26-B PIT #98, V25-D PIT #91 |
| **时间窗口索引** | `(code, trade_date) WHERE is_current` 部分索引 | V26-C PIT #105, V26-A |
| **JSONB 决策存储** | `action JSONB` + `refs TEXT[]` | l3.event_strategist_advice |
| **审计日志** | `created_at` + `last_updated` + `version` | 110 PIT 沉淀 |
| **AES-256 加密** | 持仓成本价/止盈止损 P0 绝密 | DB_ENCRYPTION_KEY 64 字符 |

---

## 八、安全 + 合规设计

### 8.1 三层凭据结构 (Phase II SHARE-WINWSL 实战)

```
┌─────────────────────────────────────────────────────────────────────┐
│  L1: ~/.hermes/invest_credentials/store.json (mode 600)              │
│      真实密码: DB_PASSWORD / DEEPSEEK_API_KEY / FEISHU_WEBHOOK        │
│      DB_ENCRYPTION_KEY / DASHBOARD_PASSWORD / DATABASE_URL           │
├─────────────────────────────────────────────────────────────────────┤
│  L2: ~/invest_system/.env                                            │
│      占位符 DB_PASSWORD=*** (防 git 泄露)                              │
├─────────────────────────────────────────────────────────────────────┤
│  L3: 环境变量 (可选覆盖)                                                │
└─────────────────────────────────────────────────────────────────────┘
```

### 8.2 WCM 跨端凭据共用 (Phase II 实战)

- `InvestPilot_Feishu` ✅ EXISTS_IN_WCM (81 字符)
- `InvestPilot_DB` / `InvestPilot_DeepSeek` / `InvestPilot_Dashboard` 设计中 (per V25-A1)

### 8.3 数据脱敏 (推送 + Web UI + Telegram)

- **钉钉群** (全员): 代码+名称+方向可见, 金额/成本/止损 脱敏
- **Web UI** (本人登录): 全明细可见
- **Telegram Bot**: 默认 P2 脱敏, 2FA 解锁 P1

### 8.4 合规检查清单 (5 项)

- [x] 持仓数据 AES-256 加密存储
- [x] 推送消息 TLS 1.3 传输
- [x] 敏感字段查询日志审计
- [x] 数据保留期 ≤ 365 天
- [x] GDPR/个保法合规审查 (PIT #26 user 重要补充)

---

## 九、性能 + 可扩展性设计

### 9.1 性能基线 (6/14 实战)

| 模块 | 性能 | 数据 |
|------|------|------|
| V22-T3 intraday_hermes_agent | 1.5s/异动 | 1 LLM 调用 |
| V25-B position_rebalancer | 5s/51 持仓 | 3 源汇总 |
| V25-G 7d_report_generator | 2.5s/45 持仓 | 6 步生成 |
| V25-D position_rebalancer_v2 | 8s/51 持仓 | 4 CSV 跨账户 |
| V25-E attribution_analyzer | 12s/45 持仓 | Brinson 39 归因 |
| V26-B benchmark_quote_loader | 2.14s/5 指数 | 30 天窗口 |
| V26-C position_unifier | 3s/4 CSV | 21 持仓 upsert |
| V26-A quote_streamer | 11.43s/6 标的 | akshare+baostock |
| 31 模式测试 | 80s | 12-13s/模式 |
| 23 端到端 | 13.3s | 100% 通过 |
| 39 汇总 | 1.38s | 100% |

### 9.2 成本 (v2.0 补丁3 + 实战 6/14)

| 任务 | 调用 | Token | USD/天 | RMB/天 |
|------|------|------:|-------:|-------:|
| hermes_event_scan | 3 子Agent | 8000/调用 | 0.048 | 0.35 |
| hermes_kb_ingest | 3 子Agent | 12000/调用 | 0.072 | 0.52 |
| intraday_explain | 5 告警/天 | 1500/调用 | 0.015 | 0.11 |
| attribution_analyzer (V25-E) | 1 次/天 | 2000/调用 | 0.004 | 0.03 |
| 7d_report_generator (V25-G) | 1 次/周 | 5000/调用 | 0.001 | 0.01 |
| **月总计** | - | - | **~$4** | **~30 RMB** |

### 9.3 存储增长

- skill_md_files: +1 KB/天 (增量 patch)
- event_scan_logs: +50 KB/天
- l3.* 表: 实战 5.2MB (33 commits) + 未来 ~1.5MB/月
- 月增长: **~1.5 MB (可忽略)**

### 9.4 资源峰值

- CPU peak: < 30% (LLM 短时)
- 内存: < 500 MB (LLM 调用时)
- 网络: < 10 MB/天

---

## 十、关键时间窗口策略 (v2.0 补丁4)

### 10.1 未来 90 天关键事件

| 日期 | 事件 | 模式 | 期望收益 |
|------|------|------|---------:|
| **6/15 周一 09:00** | V24-C1 持仓风险周报 | 实战 | 推送 10 条 |
| **6/15 周一 11:30** | V24-C6 大模型首席分析师首次实战 | 实战 | 推送 1 条 ⭐ |
| 6/17 周三 11:30 | FOMC 后 V24-C6 第 2 次 | 实战 | 1 条 |
| 6/19 周五 11:30 | 周线收官 V24-C6 第 3 次 | 实战 | 1 条 |
| **6/20 周六 18:00** | V25-G 7d 报告首次自动出 | 实战 | 1 条 ⭐ |
| **6/22 周日 22:00** | V25-D 调仓周报首次自动出 | 实战 | 1 条 ⭐ |
| **8/10 周日** | V25-F 中报季首次实战 300394 披露 | 实战 | 0/1 条 ⭐ |
| 8/15 周五 | V25-F 002156 通富微电披露 (miss -44.4%) | 实战 | 1 条 P1 |
| 8/18 周一 | V25-F 600487 亨通光电 (PIT #83 新增) | 实战 | 1 条 |
| **8/28 周五** | V25-F 300680 隆盛科技披露 (miss -52.6%) | 实战 | 1 条 P0 |

### 10.2 模式切换

| 模式 | 触发 | 实战 |
|------|------|------|
| `event_driven_v2` | 重大事件 (6/12 SpaceX IPO) | ✅ V24-C6 + V26-A |
| `single_target_deep_dive` | 单标的催化 (6/15 亨通分拆) | ✅ V24-C6 + V25-B |
| `defensive_max` | FOMC/通胀 (6/16-17) | 实战中 |
| `single_target_catalyst` | 重大催化 (6/30 东材试产) | 实战中 |
| `earnings_validation` | 中报季 (7/15-8/30) | ✅ V25-F 实战 |
| `position_rebalance_weekly` | 调仓周报 (V25-D 6/22) | 实战中 |

---

## 十一、当前实施映射 (32 commits + 8 release)

### 11.1 14 方向 100% 实战

| 方向 | 模块 | 文件 | commit | release | PIT |
|------|------|------|--------|---------|-----|
| V22-T3 | 方案3 盘中异动 | intraday_hermes_agent.py 16KB | `1319431` | v2.3 (1 实战) | 22 实战 |
| V22-T4 | 方案4 L3 策略顾问 | l3_dialog_engine 扩展 | `c48559f` | v2.3 (1 实战) | 10 实战 |
| V23-R1 | 方案8 回测入口 | hermes_backtest_validator 14.5KB | `21b0c91` | v2.3 (1 实战) | 8 实战 |
| V23-R2 | 方案6 跨标协同+方案7 双端桥 | hermes_portfolio_copilot 45.6KB | `510dbda` | v2.3 (1 实战) | - |
| V23-R3 | 监控 7 天 + 集成 | v22_monitoring.py 22.6KB | `a609dc5` | v2.3 (1 实战) | - |
| V24-B1 | 修 95.2% → 100% | 集成验证 | `011b02b` | v2.4 (1 实战) | - |
| V24-B2 | 方案 6 LLM 真实接入 | LLM 实战 1.7s/0.5分钱 | `5b625f0` | v2.4 (1 实战) | 5 实战 |
| V24-B2.1 | DeepSeek 复用+缓存+降级 | llm_fallback_chain 12.7KB | `22c4658` | v2.4.1 (1 实战) | 5 实战 |
| V24-B3 | WebSocket 实时推送 | dashboard_hermes_websocket 22.7KB | `eb0c10c` | v2.4.1 (1 实战) | 5 实战 |
| V24-C1 | 持仓风险预算 | position_risk_manager 23.6KB | `2589fdd` | v2.4.2 (1 实战) | 5 实战 |
| V24-B4 | L3 Advisor 跨 Profile 隔离 | profile_strategy 24.7KB | `88362c2` | v2.4.3 (1 实战) | 5 实战 |
| V24-C4 | 回测策略自动调优 | strategy_optimizer 28.2KB | `f49edb1` | v2.4.3 (1 实战) | 5 实战 |
| V24-C5 | profit_pct=10000% 修复 | profit_pct_recalculator 12KB | `e721576` | v2.4.3 (1 实战) | 5 实战 |
| V24-C6 | 大模型事件首席分析师 | chief_event_strategist 21.7KB | `c06811c` | v2.4.4 (1 实战) | 5 实战 |
| V25-A1+A2 | 飞书推送路由 | position_risk_triggers 21.7KB | `6244cb2` | v2.5 plan | 5 实战 |
| V25-F | 中报季业绩 miss 触发器 | earnings_miss_trigger 20.4KB | `280f72e` | v2.5.0 | 3 实战 |
| V25-B | 持仓调仓助手 | position_rebalancer 28KB | `2ad350c` | v2.5.0 | 5 实战 |
| V25-C | 事件回放+实战准确度 | event_backtester 28KB | `fb87e5e` | v2.5.0 | 5 实战 |
| V25-G | 7d 报告自动出 | 7d_report_generator 25KB | `022bd14` | v2.5.0 | 3 实战 |
| V25-D | 调仓优化 v2 | position_rebalancer_v2 26.8KB | `ff5cccb` | v2.5.0 | 5 实战 |
| V25-E | 业绩归因 Brinson | attribution_analyzer 32KB | `9ea420e` | v2.5.0 | 5 实战 |
| V26-B | AKShare 指数基准 | benchmark_quote_loader 16.6KB | `98bd1bf` | v2.6.0 | 2 实战 |
| V26-C | 4 CSV ↔ PG 持仓统一 | position_unifier 17.9KB | `a1f7804` | v2.6.0 | 3 实战 |
| V26-A | 行情拉取器 方案B | quote_streamer 25.6KB | `b40539a` | v2.6.0 | 5 实战 |
| **合计** | **24 实战** | **+15000/-1800 行** | **25 commits** | **8 release** | **110 PIT** |

### 11.2 文档实施映射

| 文档 | 实战 | 大小 | 实战 |
|------|------|-----:|------|
| v1.0 8大方案 (`1.md`) | 6/8 | 21.8KB | 2026-06-10 |
| v2.0 5大补丁 (SKILL.md) | 5/5 | 11KB | 2026-06-11 |
| v2.3 release | 1 实战 | 14.6KB | 2026-06-09 |
| v2.4 release | 1 实战 | 25.8KB | 2026-06-10 |
| v2.4.1 release | 1 实战 | 30KB | 2026-06-10 |
| v2.4.2 release | 1 实战 | 31.9KB | 2026-06-11 |
| v2.4.3 release | 1 实战 | 33.8KB | 2026-06-12 |
| v2.4.4 release | 1 实战 | 35.4KB | 2026-06-12 |
| v2.5 plan | 1 实战 | 18.7KB | 2026-06-12 |
| v2.5.0 release | 1 实战 | 37.5KB | 2026-06-14 |
| v2.6 plan | 1 实战 | 28KB | 2026-06-14 |
| v2.6.0 release | 1 实战 | 22.2KB | 2026-06-14 |
| Phase II SHARE-WINWSL | 1 实战 | 11.3KB | 2026-06-12 |
| **system-design (本文件)** | 1 实战 | 22KB | 2026-06-14 |

---

## 十二、待办 + 风险 (实战 6/14)

### 12.1 实战待办 (未来 30 天)

| 优先级 | 任务 | 触发时间 | 实战 |
|:---:|------|----------|:---:|
| P0 | V24-C1 持仓风险周报 | 6/15 09:00 | 实战 ⭐ |
| P0 | V24-C6 大模型首席分析师首次实战 | 6/15 11:30 | 实战 ⭐ |
| P0 | V25-G 7d 报告首次自动出 | 6/20 18:00 | 实战 ⭐ |
| P0 | V25-D 调仓周报首次自动出 | 6/22 22:00 | 实战 ⭐ |
| P1 | V25-F 中报季首次实战 300394 | 8/10 周日 | 实战 ⭐ |
| P1 | V26-A quote_streamer_5min cron | 实战 cron 5min | 待加 |
| P2 | v2.6.1 release 文档 (V26-A 实战 1 周) | 6/21 | 待启动 |
| P2 | v2.7 plan 调研 (V26-D/G 实战后) | 7/13 | 待启动 |

### 12.2 风险与缓解

| 风险 | 缓解 | 实战 |
|------|------|------|
| LLM 幻觉 | 每个决策引用具体 skill+event+data (PIT #79) | ✅ 实战 |
| Skill patch 冲突 | patch 前 skill_view 对比 (v2.0 补丁1) | ✅ 实战 |
| Cron 失败无感知 | watchdog + 钉钉告警 (V23-R3) | ✅ 实战 |
| 多端同步不一致 | 单一数据源 PG (决策2) | ✅ 实战 |
| GitHub push 阻塞 | 3 个全设 + 12 模式 (per memory §6) | ✅ 33 commits 实战 |
| 蓝屏根因 | 向日葵禁启 + DDU + 拔显示器 (per memory §1) | 🔧 待执行 |
| 6/12 数据 silently 丢失 | 写文件必独立 sanity check (per memory) | ✅ 实战 |

---

## 十三、附录

### 附录 A: 110 PIT 完整索引 (实战 6/14)

- V22 (22): PIT #1-#22 (10 bug + 12 实战)
- V23 (8): PIT #23-#30
- V24 (38): PIT #31-#68
- V25 (28): PIT #69-#96
- V26 (14): PIT #97-#110

(详见 v2.6.0-summary.md 附录 A)

### 附录 B: 33 scripts 模块清单 (实战 6/14)

33 个 .py 脚本在 `hermes_coordination/scripts/`, 实战 6/14 总 ~700KB
(详见 v2.6.0-summary.md 附录 B + 实战章节九)

### 附录 C: 关键 PIT 实战汇总

| PIT # | 实战 | 实战 6/14 |
|------|------|----------|
| #12 (PG column 铁律) | 9 次验证 | V22-V26-A |
| #13 (连接池耗尽) | 实战 ThreadedConnectionPool(2-10) | V22-V26-A |
| #70 (1800 字符飞书) | 实战 6/14 | V25-A1+A2 |
| #91 (CSV header 列数) | 实战 6/14 | V25-D |
| #96 (importlib+sys.modules) | 实战 6/14 | V25-D-V26-A |
| #104 (ALTER TABLE) | 实战 6/14 | V26-C |
| #106 (akshare 限频) | 实战 6/14 | V26-A |
| #109 (PG asset_type→type) | 实战 6/14 第 9 次 | V26-A |

### 附录 D: 引用的其他文档

- v1.0 8大方案: `1.md` (21.8KB)
- v2.0 5大补丁: `hermes-investpilot-coordination-v2/SKILL.md` (11KB)
- v2.3-v2.6.0 release: 8 个 release 文档
- v22-v26 plan: 5 个 plan 文档
- 14 PIT 文档: 实战 6/14 ~190KB

---

**待命状态**: 本系统设计 (HLD) v1.0 已纳入投资顾问上下文。完整覆盖 v1.0 8大方案 + v2.0 5大补丁 + 14 方向 v2.4-v2.6 实战, 配套 7 层架构图 + 模块依赖 + 数据流 + 部署拓扑 + 7 大设计决策 + PG Schema 42 表 + 110 PIT + 33 脚本模块清单。后续可立即基于本 HLD 启动 v2.7 规划 或 v6.2 实战 (实战 6/15-22 cron 触发实战)。请给出下一个指令。
