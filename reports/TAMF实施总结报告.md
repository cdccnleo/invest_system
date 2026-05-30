# InvestPilot TAMF 投资分析记忆系统 — 实施总结报告

**日期**: 2026-05-30
**阶段**: Phase T4（仪表盘视图 + 全面集成）
**系统版本**: v2.0 Phase T1-T4 完成

---

## 一、本次实施范围（Phase T4）

本次 Phase T4 在方案 v2.0 基础上完成了 TAMF（TARget-level Memory File）投资分析记忆系统的全面实施，覆盖以下模块：

### T4.1 — dashboard `?page=tamf` 视图
**状态**: ✅ 完成

| 组件 | 实现内容 |
|------|---------|
| `PAGES` 列表 | 新增 `"📊 TAMF分析记忆"` 选项 |
| `page_map` 路由 | `"tamf"` → `"📊 TAMF分析记忆"` URL映射 |
| `render_sidebar()` selectbox | 导航链路接通 |
| `main()` 路由分支 | `elif page == "📊 TAMF分析记忆": render_tamf_memory()` |
| `render_tamf_memory()` | 左侧持仓选择 + 右侧5子Tab + 底部时间线 |

**视图结构**:
- 左侧栏：46只持仓下拉选择 + 元数据卡片（版本/状态/行情日期/公告日期）
- 右侧5 Tab：`📋 完整文件` / `📊 基本面` / `📈 技术面` / `📰 消息面` / `🧠 监控`
- 底部：`memory.target_timeline_events` 近30条时间线（颜色区分严重级别）

**访问路径**: 侧边栏导航 → `📊 TAMF分析记忆`，或 URL 直链 `?page=tamf`

---

### T4.2 — Agent段落集成进增量更新流程
**状态**: ✅ 完成

`incremental_update()` 现在完整执行三层更新：

```
build_tamf_file()          → 重建文件框架结构（章节1-8）
  ↓
update_agent_sections_in_tamf() → LLM生成技术面/基本面/消息面三段落
  ↓
恢复手动编辑保护内容
```

**LLM路由**:
- 技术面简评 → Ollama 本地（`gemma4:e4b`）
- 基本面综合评估 → DeepSeek 云端
- 消息面综合判断 → Ollama 本地

**防幻觉机制**: 行情数据不足时直接返回 `⚠️ 数据不足`，不调用 LLM。

---

### T4.3 — 手动编辑保护与冲突处理
**状态**: ✅ 完成

**双重保护机制**:

1. **文件级保护** (`detect_manual_edit`):
   - 扫描 `<!-- MANUAL EDIT: [原因] -->` HTML注释
   - 增量更新时将受保护标记追加到文件末尾
   - 避免被 `build_tamf_file()` 整体覆盖丢失

2. **章节级保护** (`is_section_manually_edited`):
   - 定位每个 `### Agent XXX` 章节
   - 检查章节块内是否有 `<!-- MANUAL EDIT -->` 标记
   - 若有则跳过该章节的正则替换，保护手动修改内容
   - 应用于三个 Agent 段落：技术面简评 / 基本面综合评估 / 消息面综合判断

**编辑后操作**: 用户在 TAMF 文件中手动编辑内容后，在编辑处添加 `<!-- MANUAL EDIT: [原因] -->` 注释，下次增量更新时该章节不会被 LLM 覆盖。

---

### T4.4 — TAMF文件Git版本控制
**状态**: ✅ 完成

| 组件 | 内容 |
|------|------|
| `.gitignore` | 移除 `data/target_memories/*.md` 和 `data/target_memories/` 两项 |
| `tamf_git_commit.py` | 独立脚本：`get_git_changes()` / `commit_tamf_changes()` |
| job_tamf_update 钩子 | 每次增量更新后自动 `commit_tamf_changes()` |
| 首次提交 | 46个 `.md` 文件 + TEMPLATE.md 全部纳入 Git 追踪 |

**提交消息规范**:
- 有新增文件无修改: `TAMF新标的初始化 (YYYY-MM-DD)`
- 有修改文件: `TAMF每日增量更新 (YYYY-MM-DD HH:MM)`
- 同时有新增和修改: `TAMF文件变更 (YYYY-MM-DD HH:MM)`

---

### T4.5 — 数据脱敏器检查
**状态**: ✅ 已实现（gap分析误报）

gap分析报告称"未确认脱敏器实现"，经验证 `data_sanitizer.py` 已完整实现且链路接通：

```
run_analysis.py:
  Step 6: reset_mapping() → sanitize_snapshot(total_mv, positions)
  Step 7: build_analysis_prompt(sanitized_positions=sanitized, ...)
```

**脱敏规则**（`sanitize_snapshot`）:
- 股票代码 → 匿名 ID（`STK_001` / `STK_002` ...）
- 金额 → 占比百分比（`weight_pct`）
- 股数 → 不传输
- 盈亏具体数值 → 方向描述（"大幅盈利" / "小幅亏损" / "大幅亏损"）

---

### T2.5 — 周频深度分析
**状态**: ✅ 完成

`scheduled_deep_analysis_weekly()` 注册到 APScheduler：
- **时间**: 每周日 22:00
- **数据窗口**: 行情60日 / 财务12季 / 公告30条 / 研报10篇
- **操作**: 全部46只标的强制重生成 Agent 段落（第4/5/6章）
- **事件记录**: 写入 `memory.target_timeline_events`（`DEEP_ANALYSIS_WEEKLY` 类型）
- **告警**: 失败 > 0 时推送飞书通知

---

## 二、Git提交记录

```
41a4fcc  Phase T4 收尾: TAMF增量Agent更新+手动编辑保护+Git版本控制+Gitignore修正
659adb2  T2.5: 周频深度分析 (scheduled_deep_analysis_weekly) — TAMF Agent段落全量重生成 + APScheduler周日22:00注册
71ebf28  T4.1: dashboard ?page=tamf 视图 + render_tamf_memory() + 导航链路
6ed4345  Phase 前期: 初始版本
```

---

## 三、系统当前状态

### 3.1 APScheduler 任务清单（11个任务）

| ID | 任务 | 时间 | 状态 |
|----|------|------|------|
| `morning_routine` | 盘前工作流 | 08:30 | 🟢 |
| `midday_routine` | 午间快讯 | 11:30 | 🟢 |
| `closing_routine` | 盘后工作流 | 15:30 | 🟢 |
| `tamf_daily_update` | TAMF增量更新+Git提交 | 15:35 | 🟢 |
| `evening_routine` | 晚间工作流 | 21:00 | 🟢 |
| `reports_collection` | 研报采集 | 16:00 | 🟢 |
| `announcements_collection` | 公告采集 | 20:50 | 🟢 |
| `skill_spot_check` | 技能质量抽查 | 周日 21:00 | 🟢 |
| `deep_analysis_weekly` | 周频深度分析 | **周日 22:00** | 🟢 |
| `skill_solidification` | 技能固化 | 22:00 | 🟢 |
| `intraday_monitoring` | 盘中异动监控 | 每5分钟 | 🟢 |

### 3.2 TAMF系统文件清单

| 文件 | 说明 |
|------|------|
| `scripts/tamf_updater.py` | 核心引擎（~1220行），含init/update/check命令 |
| `scripts/tamf_git_commit.py` | Git自动提交脚本 |
| `data/target_memories/TEMPLATE.md` | 8章节标准模板 |
| `data/target_memories/{CODE}.md` | 46只持仓的TAMF文件 |
| `scripts/migrations/add_tamf_tables.sql` | memory schema DDL |

### 3.3 关键数据库表

| Schema | 表 | 说明 |
|--------|-----|------|
| `memory` | `target_memory_files` | 46行，元数据+版本+快照 |
| `memory` | `target_timeline_events` | 47条事件记录 |

---

## 四、TAMF系统架构总览

```
每日 15:35 TAMF增量更新
    │
    ├─ load_positions()        → 持仓数据（46只）
    ├─ load_recent_quotes()    → 20日行情
    ├─ load_financial_trend()  → 8季财务
    ├─ load_recent_announcements() → 10条公告
    ├─ load_recent_reports()   → 5篇研报
    │
    ├─ build_tamf_file()       → 框架结构（章节1-8，placeholder）
    │     ├─ build_section_2_holdings()
    │     ├─ build_section_3_fundamentals()
    │     ├─ build_section_4_technical()
    │     └─ build_section_5_news()
    │
    ├─ update_agent_sections_in_tamf()  ← LLM驱动的智能段落
    │     ├─ generate_agent_section("technical_assessment") → Ollama本地
    │     ├─ generate_agent_section("fundamental_assessment") → DeepSeek云端
    │     └─ generate_agent_section("news_assessment") → Ollama本地
    │
    ├─ detect_manual_edit()     → 恢复手动编辑保护内容
    ├─ write_tamf()             → 写入 data/target_memories/{code}.md
    ├─ upsert_tamf_metadata()   → 更新 memory.target_memory_files
    ├─ _record_timeline_event() → 记录 timeline 事件
    └─ commit_tamf_changes()    → Git自动提交

每周日 22:00 周频深度分析
    └─ scheduled_deep_analysis_weekly()
          └─ 全量60日行情+12季财务+30条公告+10篇研报 → LLM全量重生成
```

---

## 五、与方案 v2.0 的对照

### 5.1 方案承诺 vs 实施状态

| 方案 v2.0 设计项 | 实施状态 | 备注 |
|-----------------|---------|------|
| TAMF文件结构（8章节） | ✅ 完整实现 | 含 Agent 智能段落 |
| 增量更新（仅更新受影响章节） | ✅ 实现 | `incremental_update()` |
| 手动编辑保护（MANUAL EDIT标注） | ✅ 双重保护 | 文件级+章节级 |
| 周频深度分析 | ✅ 实现 | 每周日22:00 |
| Git版本控制 | ✅ 实现 | `tamf_git_commit.py` |
| 数据脱敏（金额→百分比，代码→匿名ID） | ✅ 已实现 | gap报告误报 |
| dashboard TAMF视图 | ✅ 实现 | `?page=tamf` |

### 5.2 剩余未完成项

| 设计项 | 状态 | 说明 |
|--------|------|------|
| Agent抽象接口层（隔离Hermes） | 🔴 未实现 | 当前直接调用llm_caller |
| 回测引擎 | 🟡 待激活 | 需≥30天历史数据 |
| 券商API接入 | 🟢 低优先级 | 计划Phase 5 |

---

## 六、使用指南

### 6.1 手动编辑TAMF文件

在要保护的内容块内添加 `<!-- MANUAL EDIT: [原因] -->` 标记：

````markdown
### Agent 技术面简评
<!-- MANUAL EDIT: 用户补充趋势判断 -->
```
系统生成的判断内容...
```
````

下次增量更新时，该章节不会被 LLM 覆盖。

### 6.2 查看TAMF视图

```
http://localhost:8501/?page=tamf
```

或在 dashboard 侧边栏选择 `📊 TAMF分析记忆`。

### 6.3 GitHub推送（待网络恢复）

```bash
# 认证
gh auth login

# 推送全部pending commits
cd /home/aileo/invest_system
git push origin master
```

---

## 七、已知限制

1. **financial_indicators覆盖率低** — 仅3只标的(002943/300059/002709)有财务数据，Agent基本面评估段落部分标的会返回"数据不足"
2. **国际投行研究** — 华尔街见闻RSS可访问，实测含投行引用内容；Bloomberg/Reuter/FT/MS/GS不可达
3. **GitHub push** — 当前网络不可达，待恢复后需用户手动认证 `gh`
4. **回测引擎** — 需≥30个交易日数据积累后激活

---

*本报告由 Hermes Agent 自动生成 | 2026-05-30*
