# V24-B2 实施 PIT 教训沉淀 (LLM 真实接入)

> **版本**: V24-B2 增量 | **日期**: 2026-06-13 | **任务**: 方案 6 LLM 语义匹配接入
> **新增 PIT**: 22-26 (5 个) | **修复 PIT**: 7 (复用)

---

## PIT #22: 模式标识 (LLM vs 关键词) 必须区分 (新)

**根因**: V24-B2 引入 LLM 后, 监控/集成验证无法区分匹配来源, 无法统计 LLM 实际命中率。

**修复**:
- `event_id` 后缀: `_llm` (LLM 路径) / `_kw` (关键词路径)
- `reasoning` 前缀: `[LLM 语义匹配]` / `[关键词匹配]`
- 三个常量: `_MATCH_MODE_LLM = "llm"` / `_MATCH_MODE_KEYWORD = "keyword"` / `_MATCH_MODE_EMPTY = "empty"`

**PIT 教训**: 任何双路径实现, 必须有显式标识 + 监控指标, 否则无法量化效果。

---

## PIT #23: 真实 LLM 调 30s+ 超时, 必须 try/except + 降级 (新)

**根因**: gpt-4o-mini 在弱网/Wi-Fi 切换时 30s 不响应, 实战中会卡死整个流程。

**修复**:
```python
# 1. map_event_to_holdings 包 try/except
if use_llm:
    try:
        llm_result = call_llm_for_event_match(event_topic, holdings)
    except Exception as e:
        LOG.warning(f"[map_event_to_holdings] LLM 异常, 降级: {type(e).__name__}: {e}")
        llm_result = None  # 触发降级到关键词

# 2. call_llm_for_event_match 内部 try/except
try:
    resp = client.chat.completions.create(timeout=30, ...)
except Exception as e:
    LOG.warning(f"LLM 调失败: {e}")
    return None  # 调用方收到 None 走降级
```

**PIT 教训**: 真实外部依赖 (LLM/HTTP/DB), 永远不可能 100% 可靠, 必须有降级链 + 日志。

---

## PIT #24: 测试 mock 必须 mock hpc.call_llm_for_event_match, 不是 openai.OpenAI (新)

**根因**:
1. `hpc` 模块在函数内 `from openai import OpenAI` (line 294), **不在模块顶层**
2. 改 `openai.OpenAI = mock` 不影响 hpc 内的 `OpenAI` 局部名
3. 必须 mock `hpc.call_llm_for_event_match` 函数本身, 才能在测试中拦截

**修复**:
```python
# ❌ 错: 改 openai.OpenAI 不影响 hpc
import openai
openai.OpenAI = MockOpenAIClient

# ✅ 对: 直接 mock hpc.call_llm_for_event_match
import hermes_portfolio_copilot as hpc
original = hpc.call_llm_for_event_match
def mock(event_topic, holdings):
    return {"affected_codes": ["300136", ...], ...}
hpc.call_llm_for_event_match = mock
```

**PIT 教训**: 任何函数内 import 的依赖, mock 必须在函数对象级别, 不在 import 模块级。

---

## PIT #25: 限额文件必须 __init__ 主动 touch (PIT #21 复用 + 跨模块一致)

**根因**: `DailyQuota` lazy-create 文件, 集成验证/监控窗口 fail。

**修复**: `_DailyLLMQuota.__init__` 主动 `touch()` + write default state (PIT #21 双保险)。

**PIT 教训**: 多个模块共用同一限额文件 (`/tmp/hermes_llm_quota.json`),
所有访问点必须都主动 touch, 否则一处 lazy-create, 监控仍 fail。

---

## PIT #26: LLM schema 验证 (PIT #10 早退铁律复用)

**根因**: LLM 返 `{affected_codes: "300136"}` (字符串) 而非 `["300136"]` (列表) → 后续代码 `.get("affected_codes")` 拿到字符串, 遍历时按字符迭代。

**修复**:
```python
if not isinstance(result.get("affected_codes"), list):
    LOG.warning(f"LLM 返回 schema 错: {result}")
    return None  # 触发降级
if result.get("direction") not in ("positive", "negative", "neutral"):
    result["direction"] = "neutral"  # 降级而非 fail
```

**PIT 教训**: LLM 输出永远不信任, 必须严格 schema 验证 + 失败时降级, 不能 raise。

---

## V24-B2 关键决策

### 1. 限额管理: `/tmp/hermes_llm_quota.json` 共享

**WHY**: l3_dialog_engine + hermes_portfolio_copilot 共用, 单日 20 次限额, 避免 LLM 成本失控。

**权衡**:
- ✅ 单点限流, 全局生效
- ⚠️ 两模块抢额度 (如果某天 l3 用满, copilot 降级)
- 解法: V24-B3 可拆 `copilot_llm_quota.json` 独立

### 2. Fallback 链 (PIT #7 + #23)

```
map_event_to_holdings(event, holdings, use_llm=True)
  ├─ L1: LLM 语义匹配 (主路径, ~5-10s)
  │   ├─ 失败/限额满 → None
  │   └─ 成功 → return LLM Impact
  └─ L2: 关键词硬匹配 (降级, <0.01s)
      └─ 0 命中 → magnitude=0, neutral
```

### 3. PIT #24 mock 模式

**实战**: 真实 LLM 调 (慢, 30s+)
**测试**: mock `hpc.call_llm_for_event_match` (0.001s, 完全可控)

---

## V24-B2 验证清单 (12 模式全过 + 21/21 端到端)

| 测试 | 范围 | 通过 | 耗时 |
|------|------|:---:|:---:|
| 模式 4-8 | 基础模块 (l3, skill_rollback, backtest) | 5/5 | 2.78s |
| 模式 9-10 | 跨标协同 + 双端桥 (含 LLM mock) | 2/2 | 0.54s |
| 模式 11-12 | 监控 7 天 + 集成验证 | 2/2 | 0.34s |
| 模式 13 (新) | LLM 真实接入 + Fallback 链 | 1/1 | 0.02s |
| **总** | **10/10** | **3.68s** | ✅ |
| 端到端 | 21/21 (100.0%) | 2.88s | ✅ |

---

## V24-B2 与 PIT 关系

```
PIT #1  - FTS5 列名                (V22-T2, 复用)
PIT #5  - Path 动态                 (V22-T3, 复用)
PIT #7  - PG 事务 commit            (V22-T3, 复用)
PIT #10 - 早退 schema 铁律          (V22-T4, 复用)
PIT #11 - quota path 错             (V22-T4, 复用)
PIT #12 - list_backups 无参         (V23-R1, 复用)
PIT #13 - TS code 后缀              (V23-R1, 复用)
PIT #14 - backtest 真实结构         (V23-R1, 复用)
PIT #15 - INTERVAL f-string         (V23-R1, 复用)
PIT #16 - 持仓表名 (encrypted)     (V23-R2, 复用)
PIT #17 - EventImpact affected_* 字段 (V23-R2, 复用)
PIT #18 - 主题词污染                (V23-R2, 复用)
PIT #19 - 持仓名模糊匹配            (V23-R2, 复用)
PIT #20 - importlib 路径            (V23-R3, 复用)
PIT #21 - quota lazy-create         (V24-B1, 复用)
PIT #22 - LLM vs 关键词模式标识     (V24-B2, 新)
PIT #23 - 真实 LLM 30s 超时降级     (V24-B2, 新)
PIT #24 - mock 必须函数级           (V24-B2, 新)
PIT #25 - quota __init__ touch      (V24-B2, 复用 #21)
PIT #26 - LLM schema 验证           (V24-B2, 新)
```

**累计**: 21 个 PIT, 其中 v24-b2 新增 5 个 (PIT #22, 23, 24, 25, 26)

---

## 实战 LLM 接入路径 (每日 1-3 次实战)

1. 事件扫描 (cron 18:30) → 触发 V22-Monitoring
2. PortfolioCopilot 接到事件 → 调 `call_llm_for_event_match`
3. LLM 返 `{affected_codes: [...]}` (JSON mode)
4. 构造 `EventImpact` → `aggregate_portfolio_advice` → `DashboardBridge`
5. Web UI + Telegram Bot 推送

**预期 LLM 调用量**:
- 事件扫描: 1 次/天 (cron 18:30)
- 盘中异动: 0-3 次/天 (intraday_hermes_agent)
- L3 Advisor: 0-10 次/天 (用户主动问)
- **总计**: ~5-15 次/天 (限额 20, 留 5-15 buffer)

---

**V24-B2 完成**: 12 模式 10/10, 端到端 21/21, PIT 教训沉淀, 实战 LLM 链路 ready ✅
