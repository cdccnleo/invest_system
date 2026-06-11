# v1.0 8大协同方案（完整版）

> 本文档是 v2.0 补丁的**基础内容**（v1.0方案完整版）
> 对应原文件: 1.md (440行)
> 创建时间: 2026-06-11

---

## 方案1: Hermes Skill库 ↔ InvestPilot标的记忆 双向同步

### 现状
- Hermes侧: 41个skill分散在 `~/.hermes/skills/investing/` 和 `~/.hermes/skills/events/`
- InvestPilot侧: `data/target_memories/` 28份标的记忆 (`002050.md` ~ `404002.md`)，独立维护
- 两边独立维护，更新不同步
- Hermes的深度认知无法被L3对话引擎调用
- InvestPilot的实时数据无法被Hermes主动引用

### 协同方案

```python
# scripts/agent_sync.py (新文件) —— 双向同步引擎

async def sync_hermes_skill_to_tamf():
    """Hermes Skill增量 → TAMF标的记忆"""
    skills_dir = Path("/home/aileo/.hermes/skills/investing")
    for skill_dir in skills_dir.iterdir():
        skill_file = skill_dir / "SKILL.md"
        content = parse_skill(skill_file)
        extracted = extract_key_metrics(content)  # PE/卡位/份额/持仓
        code = skill_dir.name.split('-')[1]
        await update_target_memory(code, extracted)

async def sync_tamf_to_hermes_skill():
    """TAMF实时数据 → Hermes Skill增量"""
    conn = await get_pg()
    rows = await conn.fetch(
        "SELECT * FROM target_memories WHERE updated_at > NOW() - INTERVAL '1 day'"
    )
    for row in rows:
        delta = format_real_time_delta(row)
        await patch_skill(f"stock-{row['code']}", delta)
```

### 触发机制
- 盘后18:00 cronjob
- 手动触发 ("同步标的记忆")
- skill patch后自动增量同步

### 收益
- 41个skill不再 "沉睡"，每天被InvestPilot调用
- L3对话引擎可直接引用Hermes skill中的 "五层投资逻辑"

---

## 方案2: Hermes Agent作为"事件首席分析师" ⭐最高ROI

### 现状
- AInvest每天产出10-44份events报告（最高44份/日）
- 已有 `portfolio-report-systematic-analysis` skill能批量扫描
- 但扫描结果只输出文本，未与持仓数据关联
- 220份里只有约10-20份 "真正值得读"
- 用户每天需手动识别哪些报告影响持仓
- **关键事件可能在6/8当天就触发操作，但当晚未识别**

### 协同方案
详见: `scripts/hermes_event_analyst.py`

### 关键能力
- 子Agent并行（3个并行扫描）
- 多源交叉（参考39个skill）
- 多渠道推送（钉钉+企微+Web UI）
- 可追溯（每个建议都标注参考的skill+report）

### 收益
- 用户每天22:00收到 "明日操作清单" —— 无需自己读220份
- 关键事件6/8当天即可识别

---

## 方案3: "盘中异动 + Hermes实时解读"

### 现状
- InvestPilot有 `intraday_monitor.py`（盘中监控）
- 监控规则: 涨跌幅>5%、量比>3、北向资金异动等阈值规则
- 但语义理解能力弱: "为什么这只股票跌了" 只能给数据不给逻辑

### 协同方案

```python
# scripts/intraday_hermes_agent.py (新文件)

async def hermes_intraday_explain(alert):
    """盘中异动 → Hermes实时解读"""
    skill_content = load_skill(f"stock-{alert.code}-{pinyin}")
    prompt = f"""
【盘中异动】
{alert.code} {alert.name} | 现价{alert.price} | 涨跌幅{alert.pct}%
异动类型: {alert.type} | 触发时间: {alert.time}

请基于以下skill给出30字内的解读:
{skill_content}
"""
    interpretation = await hermes_quick_interpret(prompt)
    await notify_dingtalk(
        f"{alert.name} 现价{alert.price} {alert.pct}%\n"
        f"📊触发: {alert.type}\n"
        f"💡 Hermes解读: {interpretation[:80]}"
    )
```

### 收益
盘中异动立即有 "为什么" —— 而非只是 "是什么"。

---

## 方案4: "Hermes作为L3对话引擎的'策略顾问'"

### 现状
- `l3_dialog_engine.py` (33KB) 已实现基础对话
- `prompt_builder.py` (50KB) 已构建Prompt模板
- 但对话是 "单轮LLM调用"，无历史决策积累

### 协同方案

```python
class HermesEnhancedL3Engine(L3DialogEngine):
    async def build_context(self, user_query: str, user_id: str) -> dict:
        history = await hermes_memory_recall(user_id, limit=10)
        related = await hermes_session_search(user_query, limit=3)
        relevant_skills = await match_relevant_skills(user_query)
        recent_events = await get_recent_events(days=3)
        return {
            "system_prompt": self.system_template,
            "history": history,
            "related_sessions": related,
            "skills": relevant_skills,
            "events": recent_events
        }

    async def post_decision(self, response: str, user_id: str):
        decisions = extract_decision_points(response)
        for d in decisions:
            await hermes_memory_add(target="memory", content=f"[{datetime.now()}] {d}")
        mentioned = extract_stock_codes(response)
        for code in mentioned:
            await trigger_skill_update(code, response)
```

### 关键能力
- 跨session决策记忆: 6/10的减持决策在6/16 FOMC后还可调用
- 历史会话检索: 用户问 "信维通信什么时候估值破灭"，自动检索6/6/6/8/6/10所有相关对话
- 39个skill按需加载: 避免Prompt膨胀，按相关性选TOP5

### 收益
- L3对话从 "一问一答" 升级为 "持续投资顾问"
- 用户问 "6/10减持逻辑" → 自动调取当时的 skill + events + 决策链

---

## 方案5: "Hermes批量吸收AInvest新报告" ⭐最高ROI

### 现状
- AInvest每天产出10-44份events报告
- 用户每次手动扫描耗时巨大（本次session已实战: 扫描220份耗约15分钟）
- `ainvest_report_parser.py` (17.8KB) 已存在但未与Hermes打通

### 协同方案
详见: `scripts/hermes_kb_ingest.py`

### 关键能力
- 子Agent并行（3个并行扫描）
- 增量patch优于create（保留v2.5最新内容）
- 每日自动运行（22:00 cronjob）

### 收益
- 220份报告从 "用户手动扫描15分钟" → "Hermes自动处理2分钟"
- patch后的skill立即被L3对话引擎调用
- 形成 "事件→skill→对话" 完整闭环

---

## 方案6: "Hermes跨标协同矩阵（29持仓 × 事件 × 主线）"

### 现状
- 已有 `target_trend_predictor.py` (29KB) 做标的预测
- 已有 `factor_engine.py` (16KB) 做多因子
- 但都单标分析，未形成 "组合级协同"

### 协同方案

```python
class HermesPortfolioCopilot:
    """组合协同顾问"""

    async def analyze_event_impact(self, event: str) -> dict:
        holdings = await load_all_holdings_skill()
        prompt = f"""
事件: {event}
持仓组合 (共{len(holdings)}只):
{format_holdings(holdings)}

请输出:
1. 直接受益持仓 (+5%以上影响)
2. 间接受益持仓 (+1~5%影响)
3. 受损持仓 (-1%以上影响)
4. 无影响持仓 (理由)
5. 整体组合应对策略
"""
        result = await hermes_llm_call(prompt)
        return format_heatmap(result)

    async def weekly_strategy_review(self):
        events = await get_events_since(days=7)
        skills = await load_all_holdings_skill()
        tasks = [{
            "goal": "基于以下events和skills，评估本周持仓组合表现",
            "context": f"events: {events}\nskills: {skills}",
            "toolsets": ["skills", "terminal"]
        }]
        results = await delegate_task(tasks=tasks)
        return synthesize_review(results)
```

### 收益
- 每个事件都能立即映射到持仓影响（如SpaceX 4倍 → 卫星ETF+50%、西部+30%、信维-20%）
- 每周自动复盘（避免人工遗漏）
- 决策可追溯（每个建议都标注 "参考的skill + events"）

---

## 方案7: "Hermes Web UI ↔ InvestPilot Dashboard" 双端协同

### 现状
- InvestPilot有Streamlit dashboard (86KB)
- Hermes有Web UI（推送/查询）

### 协同架构
```
Streamlit Dashboard ──┐
                      ├─→ agent_interface.py ──→ Hermes Agent ──┐
Web UI/Telegram ──────┘                                          ├─→ 钉钉/企微
                                                              ─┘
```

### 具体落地

```python
# scripts/dashboard_hermes_bridge.py

async def on_dashboard_action(user_action: str, context: dict):
    """Dashboard按钮 → 触发Hermes"""
    # 例: 用户点击 "深度分析信维通信"
    if user_action == "deep_dive":
        skill_content = await load_skill("stock-300136-xinwei")
        recent_events = await get_events_for_stock("300136", days=7)
        prompt = f"""
基于以下skill和events，对信维通信做深度分析:

skill: {skill_content}
events: {recent_events}

请输出:
1. 当前估值泡沫的判断
2. 6/6/6/10事件链的影响
3. 6/12 SpaceX IPO的关联
4. 操作建议 (继续持有/减持/清仓)
"""
        result = await hermes_llm_call(prompt)
        await display_on_dashboard(result)
        await notify_dingtalk(result)
```

### 收益
- Dashboard的 "数据可视化" + Hermes的 "决策推理" 融合
- 用户在Dashboard点击 → 立即获得AI解读
- 决策自动同步到钉钉/企微/Telegram

---

## 方案8: "Hermes知识沉淀 + 历史回测验证"

### 现状
- `backtest_engine.py` (50KB) 已有回测
- 但回测策略是 "静态因子"，无事件语义

### 协同方案

```python
async def validate_hermes_strategy(strategy: dict, period: str):
    """Hermes生成的策略 → 回测验证"""
    # 1. 调用Hermes生成策略
    # 例如: 基于6/6非农事件生成的 "减持信维+加仓黄金" 策略
    # 2. 转入backtest_engine回测
    result = await backtest_engine.run(
        strategy=strategy,
        start=period.start,
        end=period.end
    )
    # 3. 对比基准（沪深300）
    metrics = compare_with_benchmark(result)
    # 4. 反馈给Hermes（强化学习）
    await hermes_memory_add(
        content=f"[{strategy.name}] 收益率{metrics.return}% 超额{metrics.alpha}%"
    )
    # 5. 输出 "策略有效性报告"
    return format_validation_report(metrics)
```

### 收益
- Hermes的 "事件驱动决策" 被回测验证（避免幻觉）
- 决策质量量化（夏普/最大回撤/超额收益）
- 形成 "决策→验证→沉淀→下次更准" 闭环

---

## v1.0 实施优先级（基于ROI）

### P0 - 立即落地（1周内）

| 方案 | 工作量 | 收益 | 关键技术 |
|------|--------|------|---------|
| **方案2** Hermes事件首席分析师 | 3天 | 每晚自动扫描220份 | delegate_task + cronjob |
| **方案5** Hermes批量吸收新报告 | 2天 | 解决报告"读不完" | ainvest_report_parser打通 |

**理由**: 直接解决 "用户每天被220份events淹没" 的痛点，立竿见影。

### P1 - 1个月内

| 方案 | 工作量 | 收益 | 关键技术 |
|------|--------|------|---------|
| **方案1** Skill ↔ TAMF双向同步 | 5天 | 41个skill不再沉睡 | skill_manage + tamf_updater打通 |
| **方案4** L3引擎升级为持续顾问 | 5天 | 跨session决策记忆 | memory + session_search |
| **方案6** 跨标协同矩阵 | 7天 | 事件→持仓自动映射 | skill + LLM语义匹配 |

### P2 - 3个月内

| 方案 | 工作量 | 收益 | 关键技术 |
|------|--------|------|---------|
| **方案3** 盘中异动实时解读 | 5天 | 异动立即给逻辑 | intraday_monitor + skill匹配 |
| **方案7** Dashboard ↔ Web UI双端 | 7天 | Dashboard点击→AI解读 | agent_interface扩展 |
| **方案8** 回测验证闭环 | 7天 | 决策可量化验证 | backtest_engine集成 |

---

## v1.0 ROI评估

| 维度 | 当前状态 | 协同后 | 提升 |
|------|---------|--------|------|
| 报告处理 | 手动30-60分钟/日 | 自动2分钟 | **15-30倍** |
| 关键事件识别 | 人工可能漏掉 | 100%识别 | **覆盖率↑** |
| Skill维护 | 手动patch | 自动patch | **人工→自动** |
| 跨session决策 | 无记忆 | 完整记忆链 | **决策可追溯** |
| 推送渠道 | 单渠道 | 多渠道 | **触达↑** |
| 回测验证 | 无 | 策略可量化 | **决策质量↑** |

---

## v1.0 → v2.0 升级路径

v1.0 5大缺失领域已通过 v2.0 补丁补齐:
1. ✅ **接口契约**: `references/contracts/01-hermes-investpilot-contract.yaml`
2. ✅ **可观测性**: `references/contracts/02-hermes-monitoring.yaml`
3. ✅ **成本评估**: `references/contracts/03-cost-estimation.yaml`
4. ✅ **关键时间窗口**: `references/contracts/04-key-window-strategy.yaml`
5. ✅ **安全合规**: `references/contracts/05-data-privacy.md`

**v2.0 总分**: 8.4/10（vs v1.0 5.1/10）