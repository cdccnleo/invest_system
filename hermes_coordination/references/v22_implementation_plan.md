# Hermes Agent × InvestPilot 系统协同方案 v2.2 实施计划

> **基础**: v2.1 (P0+P1+P2 13/13 + 4 生产集成) + PIT (教训文档)
> **目标**: 完整实现 v1.0 8大方案中 **方案3 + 方案4** (v2.0/v2.1 都没落地)
> **创建**: 2026-06-12 06:18

---

## 一、v1.0 8大方案实施状态 (实测)

| 方案 | v2.0/v2.1 状态 | 实测代码证据 |
|------|---------------|------------|
| 1. Skill ↔ TAMF 双向同步 | ✅ 完成 | hermes_agent_sync.py 18KB, PG audit 19→115 |
| 2. 事件首席分析师 | ✅ 完成 | hermes_event_analyst.py 18KB |
| 3. **盘中异动 + Hermes实时解读** | ❌ **未实现** | intraday_monitor.py **没有 chat()/LLM 调用** |
| 4. **Hermes 作为 L3 策略顾问** | ❌ **未实现** | L3DialogEngine.py 833 行, **没有 chat()/user_id/memory_recall** |
| 5. Hermes 批量吸收 AInvest 报告 | ✅ 完成 | hermes_kb_ingest.py 10KB |
| 6. Hermes 跨标协同矩阵 | 🟡 部分 | TAMF 已实现，跨标推理未 |
| 7. Web UI ↔ Dashboard 双端协同 | 🟡 部分 | 通知已通，UI 双向未 |
| 8. 知识沉淀 + 历史回测 | 🟡 部分 | backtest 已有 |

**v2.2 核心**: 补全方案3+4（最高价值"为什么"型功能）

---

## 二、方案3 实施：盘中异动 + Hermes 实时解读

### V22-T3-A: 新增 intraday_hermes_agent.py
- **位置**: `hermes_coordination/scripts/intraday_hermes_agent.py` (新)
- **接口**: `explain_anomaly(anomaly: dict) -> dict`
- **流程**:
  1. 接收 intraday_monitor 传来的 anomaly dict (ts_code/name/change_pct/...)
  2. 查 Hermes skill `stock-<code>-*` 加载该标的知识
  3. 构造 prompt: 异动数据 + skill 内容
  4. 调 LLM (用 LLMFallbackChain 4级降级)
  5. 返回 {"interpretation": str, "refs": [skill_name, ...], "fallback_level": str}

### V22-T3-B: 集成到 intraday_monitor
- 集成点: `detect_anomalies()` 返回前 + `send_notification()` 之前
- 限制: **每日限额 20 次** (避免 LLM 成本失控)
- 异步: 用 `threading.Thread` 不阻塞主扫描
- 失败回退: 降级链 L4 SKIPPED → 静默继续

### V22-T3-C: 推送增强
- 格式:
  ```
  ⚠️ {name}({code}) 涨跌幅 {pct}%
  📊 触发: {alert_type}
  💡 Hermes 解读: {interpretation[:80]}
  📚 参考: {refs}
  ```

---

## 三、方案4 实施：Hermes 作为 L3 策略顾问

### V22-T4-A: 扩展 L3DialogEngine
- 文件: `scripts/l3_dialog_engine.py` (现有 833 行)
- **新增方法**:
  - `chat(user_id: str, query: str) -> dict` — 对话接口
  - `build_context(user_query: str, user_id: str) -> dict` — 上下文构建
  - `post_decision(response: str, user_id: str)` — 决策后处理
- **存储**: `l3.dialog_history` (新建表) + `l3.decision_points` (新建表)

### V22-T4-B: 集成 Hermes 跨会话能力
- **session_search**: 调本机 `~/.hermes/.../session_search` 查历史
- **skill_match**: 基于 query 关键词找 TOP5 相关 skill
- **memory_recall**: 从 TAMF/PG 拉用户偏好
- **decision_extract**: 从 LLM 回复中抽取 buy/sell/hold 决策

### V22-T4-C: 决策沉淀
- 每次 L3 回复 → 抽取决策点 → 写 PG `l3.decision_points`
- 关联 stock code → 触发 `trigger_skill_update(code, response)` 把决策回流到 Hermes skill
- 形成 "对话→决策→skill 更新" 闭环

### V22-T4-D: Dashboard UI
- 文件: `scripts/dashboard_views/_l3_status.py` (现有 194 行)
- 新增: "💬 L3 策略顾问对话" 子页面
- 输入框 + 历史显示 + 决策点高亮

---

## 四、任务清单

| 任务 | 内容 | 交付物 | 验证 |
|:---:|------|-------|:---:|
| **V22-T3-A** | 写 intraday_hermes_agent.py | 1 个新文件, ~6KB | 解释 5 个 mock 异动 |
| **V22-T3-B** | 集成到 intraday_monitor | detect_anomalies 加 LLM 调用 | 1 异动 → 推送带解读 |
| **V22-T3-C** | 推送增强 | 推送格式带 💡 解读 | 真实推送测试 |
| **V22-T4-A** | 扩展 L3DialogEngine | chat/build_context/post_decision | 3 个方法 unit test |
| **V22-T4-B** | 集成 Hermes | session_search/skill_match/memory_recall | 1 真实对话 |
| **V22-T4-C** | 决策沉淀 | l3.dialog_history + decision_points 表 | 1 决策写入 |
| **V22-T4-D** | Dashboard UI | "L3 策略顾问" 子页面 | 1 截图 |

---

## 五、PG 表新增 (v2.2)

```sql
-- 方案4 决策沉淀
CREATE TABLE l3.dialog_history (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,  -- 'user' | 'assistant'
    content TEXT NOT NULL,
    session_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    refs TEXT[]  -- 引用的 skill/event id
);
CREATE INDEX idx_dialog_user_created ON l3.dialog_history (user_id, created_at DESC);

CREATE TABLE l3.decision_points (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    dialog_id BIGINT REFERENCES l3.dialog_history(id),
    decision TEXT NOT NULL,  -- 'buy'|'sell'|'hold'|'observe'
    stock_code TEXT,
    confidence FLOAT,
    reasoning TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_decision_user_created ON l3.decision_points (user_id, created_at DESC);
```

---

## 六、风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|-----|
| LLM 成本失控 | 中 | 高 | 每日限额 20 次 (T3-B) + L4 跳过 (T3) |
| 异步线程崩溃 | 中 | 中 | try/except 全包 + 失败计数 |
| session_search 慢 | 低 | 中 | 缓存 TOP10 + 限制 limit=3 |
| 决策抽取误识 | 中 | 高 | 加 confidence 字段 + 用户审核 UI |
| 方案4 改动大 | 高 | 中 | 分步提交, 每个方法独立 PR |

---

## 七、v2.2 vs v2.1 评分

| 维度 | v2.1 | v2.2 目标 | 提升 |
|------|------|---------|:----:|
| 战略完整性 (8 大方案覆盖) | 6/8 | 8/8 | +25% |
| 实时解读能力 | 0/10 | 8/10 | +8 |
| 跨会话决策记忆 | 0/10 | 8/10 | +8 |
| LLM 成本控制 | 8/10 | 9/10 | +1 |
| 决策可追溯 | 5/10 | 9/10 | +4 |
| **总分** | **8.7/10** | **9.5/10** | **+0.8** |

---

## 八、执行计划

**Phase A (本轮 V22-T3)**：先做方案3 (T3-A/B/C)，因为：
- 代码量小 (~150 行)
- 风险低 (LLM 限额 20/日)
- 验证快 (1 个 mock 异动即可)

**Phase B (下一轮 V22-T4)**：方案4 (T4-A/B/C/D)，因为：
- 改动大 (L3DialogEngine 833 行 + PG 2 表)
- 需先观察方案3 限额效果

请确认是否启动 **Phase A (V22-T3 方案3 实施)**。
