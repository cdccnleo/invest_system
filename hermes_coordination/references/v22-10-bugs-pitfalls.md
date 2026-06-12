# v2.2 实施阶段 10 真实 bug 复盘（V22-T3 + V22-T4）

> **来源**：2026-06-12 V22-T3 (方案3) + V22-T4 (方案4) 实施过程，所有错误均经过 5 轮端到端验证。
> **基础**：v2.1 集成阶段 3 个教训 (LSP 误报 / PyYAML 缺失 / argparse 凭印象)
> **新增**：10 个新教训 — 全部为"文档说 vs 代码实际"或"集成路径走通≠真实可用"型 silent bug
> **用途**：未来向 InvestPilot 任何模块集成 v2.2 (L3 策略顾问 / 盘中解读) 时，按本文档逐项避坑。

---

## 一、10 真实 bug 总览

| # | 类别 | Bug 简述 | 触发文件 | 严重度 |
|---|------|---------|---------|:---:|
| 1 | Schema | FTS5 列名 `created_at` 不存在, 实际是 `timestamp` | l3_dialog_engine.py | P0 |
| 2 | Schema | FTS5 虚拟表 join 错, `FROM messages WHERE MATCH` 报错 | l3_dialog_engine.py | P0 |
| 3 | FTS5 | `?` 占位符在 FTS5 MATCH 报错, 需字面量拼 OR | l3_dialog_engine.py | P0 |
| 4 | API | `DailyQuota(quota_file, limit)` 参数顺序错 | l3_dialog_engine.py | P1 |
| 5 | Path | `intraday_hermes_agent` 路径错, 多算一层 `scripts/` | l3_dialog_engine.py | P0 |
| 6 | Naming | `load_skill_for_code` 函数不存在, 实际是 `find_skill_for_code` | l3_dialog_engine.py | P0 |
| 7 | PG | `portfolio.positions` 缺表导致事务 abort 阻断后续 SQL | l3_dialog_engine.py | P0 |
| 8 | None | `session_title` 可能为 None, 切片 NoneType 崩 | l3_dialog_engine.py | P1 |
| 9 | Field | `call_llm_with_fallback` 返回字段是 `level` 不是 `fallback_level` | l3_dialog_engine.py | P2 |
| 10 | Dict | L4 早退路径字典缺 `user_dialog_id/assistant_dialog_id` | l3_dialog_engine.py | P1 |

**P0 占比 60%** — 全部"集成时 silent 走通, 真实调用全错"型 bug。

---

## 二、10 真实 bug 详细复盘

### 坑 #1：FTS5 列名 `created_at` 不存在 (实际 `timestamp`)

**触发代码**：
```python
# scripts/l3_dialog_engine.py _session_search_t4()
cur.execute("""
    SELECT m.session_id, m.content, m.created_at, s.title as session_title
    FROM messages m
    LEFT JOIN sessions s ON m.session_id = s.id
    WHERE messages_fts MATCH ?
""", (query, limit))
```

**真实错误**：
```
sqlite3.OperationalError: no such column: m.created_at
```

**根因**：
- 凭印象写 `created_at`（标准命名）
- 实际查 `state.db` schema 是 `timestamp`（FTS5 索引字段也用 timestamp）
- messages 表共 17 列：id/session_id/role/content/tool_call_id/tool_calls/tool_name/**timestamp**/token_count/finish_reason/...

**修复**：
```python
# ⚠️ PIT 修复: messages 表用 timestamp 而非 created_at
cur.execute("""
    SELECT m.session_id, m.content, m.timestamp, s.title as session_title
    FROM messages_fts f
    JOIN messages m ON m.id = f.rowid
    LEFT JOIN sessions s ON m.session_id = s.id
    WHERE messages_fts MATCH ?
    ORDER BY rank
    LIMIT ?
""", (fts_query, limit))
```

**预防**：
1. 集成任何第三方 SQLite 表前，**先 PRAGMA table_info** 看实际 schema
2. 写 SQL 时不依赖"通用命名约定" (id/created_at/updated_at)
3. 用 `pragmatic_name_check.sql` 脚本（见 [测试模式] §1）

**验证脚本**：
```bash
/home/aileo/.hermes/hermes-agent/venv/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('/home/aileo/.hermes/state.db', timeout=5)
cur = conn.cursor()
cur.execute('PRAGMA table_info(messages)')
for r in cur.fetchall():
    print(f'  {r[0]}: {r[1]}')
"
```

---

### 坑 #2：FTS5 虚拟表 join 错 (`FROM messages WHERE MATCH` 报错)

**触发代码**：
```python
# 错误版本 — 把 FTS5 MATCH 写在 messages 表上
cur.execute("""
    SELECT m.session_id, m.content, m.timestamp, s.title as session_title
    FROM messages m
    LEFT JOIN sessions s ON m.session_id = s.id
    WHERE messages_fts MATCH ?
""", (query, limit))
```

**真实错误**：
```
sqlite3.OperationalError: no such column: messages_fts
```

**根因**：
- FTS5 虚拟表 `messages_fts` 是独立表，**不是** messages 表的列
- 不能 `WHERE messages_fts MATCH` 当成 messages 的过滤条件
- FTS5 rowid 指向 messages.id

**修复**：
```python
# ⚠️ PIT 修复: FTS5 虚拟表 rowid = messages.id, 直接走 m.id
cur.execute("""
    SELECT m.session_id, m.content, m.timestamp, s.title as session_title
    FROM messages_fts f
    JOIN messages m ON m.id = f.rowid
    LEFT JOIN sessions s ON m.session_id = s.id
    WHERE messages_fts MATCH ?
    ORDER BY rank
    LIMIT ?
""", (fts_query, limit))
```

**FTS5 标准模式**（所有 4 步必做）：
1. `CREATE VIRTUAL TABLE messages_fts USING fts5(content)` 建虚表
2. `CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN INSERT INTO messages_fts(rowid, content) VALUES (new.id, ...) END` 维护
3. 查询走 `FROM messages_fts f JOIN messages m ON m.id = f.rowid`
4. `WHERE messages_fts MATCH 'query' ORDER BY rank`

**预防**：
- 任何"全文检索"需求，先看目标库是否已有 FTS5 表，**别自建**
- 用 `SELECT name, sql FROM sqlite_master WHERE name LIKE '%fts%' AND type='table'` 看所有 FTS 虚表

---

### 坑 #3：FTS5 query 不能用 `?` 占位 (需字面量拼 OR)

**触发代码**：
```python
cur.execute("""
    SELECT ... FROM messages_fts f WHERE messages_fts MATCH ?
""", (query, limit))  # query = "信维通信 300136 现在能买吗?建议卖出吗?"
```

**真实错误**：
```
sqlite3.OperationalError: fts5: syntax error near "?"
```

**根因**：
- FTS5 MATCH 表达式有自己的语法解析器
- `?` 当成 FTS5 语法字符（不是参数占位符）
- `?` 被解析为 "可选词" 语法 → 报错

**修复**：
```python
# ⚠️ PIT 修复: FTS5 query 不能用 ? 占位, 必须字面量 + 转义双引号
safe_query = query.replace('"', '""')  # FTS5 双引号转义
words = safe_query.split()
if not words:
    return []
fts_query = " OR ".join([f'"{w}"' for w in words[:5]])  # 最多 5 词 + OR + 双引号包裹
cur.execute("""
    SELECT ... WHERE messages_fts MATCH ?
""", (fts_query, limit))
```

**FTS5 query 语法规则**（必读）：
- 双引号 `"word"` 精确匹配（含特殊字符也安全）
- `word1 OR word2` 多词 OR
- `word1 word2` 隐式 AND
- `word1*` 前缀匹配
- `NEAR(word1 word2)` 邻近
- **不能用 `?` 当占位符**

**预防**：
- 任何"用户输入"传给 FTS5 MATCH 前，**必须字面量拼字符串**
- 限制 query 长度（避免 DoS）
- 转义双引号 `""` 替代 `\"`

---

### 坑 #4：`DailyQuota(quota_file, limit)` 参数顺序错

**触发代码**：
```python
# 错误版本
_HERMES_QUOTA_T4 = DailyQuota(_QUOTA_FILE_T4, _QUOTA_DAILY_T4)
#                    实际效果: daily_limit="/tmp/...json" (Path), quota_file=20 (int)
```

**真实错误**：
- import 成功（DailyQuota __init__ 不强校验类型）
- 调用 `try_acquire()` 报 `TypeError: 'int' object has no attribute 'exists'`
- 静默 L4 skip（被 try/except 吞掉）

**根因**：
- V22-T3 写的 intraday_hermes_agent 用了 `DailyQuota(quota_file=..., daily_limit=...)` 关键字参数
- V22-T4 在 l3_dialog_engine 复用时**没看签名**，凭印象用位置参数写错顺序

**修复**：
```python
# ⚠️ PIT 修复: DailyQuota(daily_limit, quota_file) 位置参数顺序
_HERMES_QUOTA_T4 = DailyQuota(_QUOTA_DAILY_T4, _Path_t4(_QUOTA_FILE_T4))
```

**预防**：
- 复用任何 class 时，**先 `inspect.signature` 看参数顺序**
- 或**永远用关键字参数** `ClassName(arg1=val1, arg2=val2)`
- 写集成时**禁止凭印象**（用户规则 §约束 #5）

**验证脚本**：
```python
import inspect
from intraday_hermes_agent import DailyQuota
sig = inspect.signature(DailyQuota.__init__)
print(sig)  # (self, daily_limit: int = 20, quota_file: Path = ...)
```

---

### 坑 #5：intraday_hermes_agent 路径错（多算一层 `scripts/`）

**触发代码**：
```python
# 错误版本 — l3_dialog_engine 在 scripts/, intraday_hermes_agent 在 scripts/../hermes_coordination/scripts/
import sys as _sys_t4
_sys_t4.path.insert(0, str(_Path_t4(__file__).parent / "hermes_coordination" / "scripts"))
#                              ^^^^^ 这里是 scripts/   ^^^^^^^^^^^^^^^^^^^ 又加一层 scripts/ = scripts/hermes_coordination/scripts/
#                              Path(__file__).parent 已经是 /home/aileo/invest_system/scripts/
#                              再拼 hermes_coordination/scripts = scripts/hermes_coordination/scripts/ (不存在)
```

**真实错误**：
```
ModuleNotFoundError: No module named 'intraday_hermes_agent'
```

**正确路径**：
- `l3_dialog_engine.py` 位置: `/home/aileo/invest_system/scripts/l3_dialog_engine.py`
- `intraday_hermes_agent.py` 位置: `/home/aileo/invest_system/hermes_coordination/scripts/intraday_hermes_agent.py`
- 相对路径: `__file__.parent.parent / "hermes_coordination" / "scripts"`

**修复**：
```python
# ⚠️ PIT 修复: l3_dialog_engine.py 在 scripts/, intraday_hermes_agent 在 scripts/../hermes_coordination/scripts/
_HERMES_SCRIPTS_DIR_T4 = _Path_t4(__file__).parent.parent / "hermes_coordination" / "scripts"
_sys_t4.path.insert(0, str(_HERMES_SCRIPTS_DIR_T4))
```

**路径推算的 3 个心智模型**：
```python
# 模型 1: __file__ 是文件名 → parent 是目录
# /home/aileo/invest_system/scripts/l3_dialog_engine.py
# __file__.parent = /home/aileo/invest_system/scripts/

# 模型 2: 要访问 scripts/ 之外的 hermes_coordination/scripts/
# 需要 parent.parent = /home/aileo/invest_system/
# 再拼 hermes_coordination/scripts/

# 模型 3: 调试用
print(_Path_t4(__file__).parent)              # 当前文件所在目录
print(_Path_t4(__file__).parent.parent)       # 上一级
print((_Path_t4(__file__).parent / "X").exists())  # 验证路径存在
```

**预防**：
- 任何 `sys.path.insert` 前**打印路径**并 `os.path.exists()` 验证
- 在 `__init__.py` 用 `import os; print(__file__)` 调试
- 写一个 `path_resolver.py` 统一管路径（v3 候选）

---

### 坑 #6：`load_skill_for_code` 函数不存在 (实际 `find_skill_for_code` + `load_skill_excerpt`)

**触发代码**：
```python
# 错误版本 — V22-T4 计划文档写的, 但 V22-T3 实际命名不一样
from intraday_hermes_agent import (
    DailyQuota, load_skill_for_code, build_prompt,  # 这 2 个不存在
)
```

**真实错误**：
```
ImportError: cannot import name 'load_skill_for_code' from 'intraday_hermes_agent'
```

**真实 API**（V22-T3-A 实际写的）：
```python
# hermes_coordination/scripts/intraday_hermes_agent.py 真实函数
def find_skill_for_code(code: str) -> Optional[Path]: ...
def load_skill_excerpt(code: str, max_chars: int = 2000) -> Optional[str]: ...
def call_llm_with_fallback(system: str, prompt: str, max_retries: int = 1) -> Dict: ...
```

**根因**：
- V22-T2 计划文档**凭印象**写的 API 名
- V22-T3-A 实际写时**改了名字**（更准确）
- V22-T4 集成时**没看实际签名**，直接用计划文档的命名
- import 失败被 try/except 吞掉 → L4_skip silent 退

**修复**：
```python
# ⚠️ PIT 修复: 实际函数名是 find_skill_for_code + load_skill_excerpt, 不是 load_skill_for_code
from intraday_hermes_agent import (
    DailyQuota, find_skill_for_code, load_skill_excerpt, call_llm_with_fallback,
)
```

**预防**：
1. **先 `dir(module)` 看真实 API**（永远）
2. **再 `inspect.getsource(func)` 看函数签名**
3. 计划文档用 `[API: 待定]` 标记，**集成时**才填实际名字
4. 集成脚本最末尾加 `import 验证`：

```python
# 验证脚本模板
import importlib
mod = importlib.import_module('intraday_hermes_agent')
print('Available functions:')
for name in dir(mod):
    if not name.startswith('_'):
        obj = getattr(mod, name)
        if callable(obj):
            import inspect
            try:
                sig = inspect.signature(obj)
                print(f'  {name}{sig}')
            except (ValueError, TypeError):
                print(f'  {name}(?)')
```

---

### 坑 #7：PG 事务 abort 阻断后续 SQL（`portfolio.positions` 缺表）

**触发代码**：
```python
# build_context 中
try:
    cur.execute("SELECT 1 FROM l3.dialog_history LIMIT 1")  # OK
    cur.execute("SELECT 1 FROM l3.stress_test_results LIMIT 1")  # OK
    cur.execute("SELECT 1 FROM portfolio.positions LIMIT 1")  # ❌ FAIL
    # ⚠️ 事务 abort！后续所有 SQL 全部失败
    # 但 try/except 把这次失败吞掉，没 rollback
except Exception:
    pass  # ⚠️ 没 rollback!

# chat() 中后续
cur.execute("INSERT INTO l3.dialog_history ...")  # ❌ 报错: current transaction is aborted
```

**真实错误**：
```
psycopg2.errors.InFailedSqlTransaction: current transaction is aborted, commands ignored until end of transaction block
```

**根因**：
- PostgreSQL 事务模型：任何 SQL 失败 → 整个事务 abort
- 后续 SQL 直到 `COMMIT` 或 `ROLLBACK` 都拒绝执行
- try/except `pass` 不调用 rollback → 事务卡在 abort 状态
- **import 成功 + 编译成功 + 集成走通 ≠ 真实可用**（用户反复强调的教训）

**修复**：
```python
try:
    cur.execute("... history ...")
    cur.execute("... events ...")
    cur.execute("... positions ...")
    self.conn.commit()  # ⚠️ PIT 修复: 显式 commit 避免后续 SQL 被 abort
except Exception as e:
    # ⚠️ PIT 修复: 表/列可能不存在, 必须 rollback 避免事务 abort 阻断后续 SQL
    self.conn.rollback()
    print(f"[T4 警告] build_context PG 部分失败: {e}")
```

**预防（PG 事务 3 铁律）**：
1. **任何 try/except 之后必须 rollback**（如果进了 except）
2. **任何 try 块成功完成必须 commit**（避免长事务）
3. **不同子系统的 SQL 用不同 cursor** 或拆成多个 connection

**PG 调试 4 步法**：
```python
# 1. 验证表存在
cur.execute("SELECT 1 FROM <table> LIMIT 1")  # 不存在会抛 UndefinedTable

# 2. 验证列存在
cur.execute("SELECT <col> FROM <table> LIMIT 1")  # 不存在会抛 UndefinedColumn

# 3. 看真实 schema
cur.execute("""SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema=%s AND table_name=%s""", (schema, table))

# 4. 事务状态检查
cur.execute("SELECT pg_current_xact_id()")  # 当前事务 ID
# 如果报错 "transaction is aborted" → 需要 ROLLBACK
```

---

### 坑 #8：`session_title` 可能为 None（NoneType 切片崩）

**触发代码**：
```python
# chat() 构造 prompt
session_titles = "; ".join([
    s.get("session_title", "")[:50] for s in context["related_sessions"]
])
#                                            ^^^^^^^^^^^^^^^^^
# 上面那条数据: {"session_title": None}  → s.get("session_title", "") 返回 None (不是 "")
#  → None[:50] 崩
```

**真实错误**：
```
TypeError: 'NoneType' object is not subscriptable
```

**根因**：
- `s.get("session_title", "")` 第二个参数是"key 不存在时"的默认值
- 但如果 key 存在但值是 `None`，**返回 None 而不是 ""**
- `None[:50]` 崩

**修复**：
```python
# ⚠️ PIT 修复: session_title 可能为 None
session_titles = "; ".join([
    (s.get("session_title") or "(无标题)")[:50] for s in context["related_sessions"]
])
```

**预防（get vs or vs ?? 三选一）**：
```python
# 1. .get(key, default) — 仅当 key 不存在时用 default
d.get("k", "x")  # key 不存在 → "x"; key 存在但值是 None → None

# 2. .get(key) or default — key 不存在 OR 值为 falsy 都用 default
d.get("k") or "x"  # None/""/0/[]/False → "x"

# 3. ?? (Python 3.13+) — 仅当值为 None 用 default
d.get("k") ?? "x"  # None → "x"; ""/0/[] 保留

# 通用建议: 想要 "key 缺失 OR None 都用 default" → 用 .get() or
```

---

### 坑 #9：`call_llm_with_fallback` 返回字段是 `level` 不是 `fallback_level`

**触发代码**：
```python
# 错误版本
llm_result = _HERMES_LLM_T4(system, full_prompt, max_retries=1)
fallback_level = llm_result.get("fallback_level", "L1_normal")
#                                           ^^^^^^^^^^^^^^^
# 实际返回的字段是 "level", 这里 get 永远拿到 "L1_normal" 默认值
```

**真实 API**：
```python
# intraday_hermes_agent.py 真实定义
def call_llm_with_fallback(system: str, prompt: str, max_retries: int = 1) -> Dict:
    """Returns: {"content": str, "level": str, "error": str|None}"""
```

**根因**：
- V22-T3-A 内部 `call_llm_with_fallback` 函数返回 `{"content": ..., "level": ..., "error": ...}`
- 字段名是 **`level`**（简洁）
- V22-T4 计划文档/集成代码写 **`fallback_level`**（更长的命名）
- LLM mock 模式下 L1_normal 是 mock 的"伪造成功"——刚好误打误撞通过测试

**修复**：
```python
# ⚠️ PIT 修复: call_llm_with_fallback 返回字段是 level, 不是 fallback_level
fallback_level = llm_result.get("level", "L1_normal")
```

**预防**：
- 调用任何函数前，**用 `inspect.getsource` 看 docstring**
- 写 wrapper function 统一字段名（推荐）：
```python
def _call_llm_normalized(system, prompt) -> dict:
    """统一所有 LLM 调用的返回字段"""
    raw = call_llm_with_fallback(system, prompt)
    return {
        "content": raw.get("content", ""),
        "fallback_level": raw.get("level", "L1_normal"),  # 统一外部用 fallback_level
        "error": raw.get("error"),
    }
```

---

### 坑 #10：L4 早退路径字典缺字段

**触发代码**：
```python
# chat() 早退路径
if quota_remaining <= 0:
    return {
        "user_id": user_id,
        "query": query,
        "response": f"[L4 跳过] ...",
        "context": self.build_context(query, user_id),  # ⚠️ 构造完整 context (耗时)
        "fallback_level": "L4_skip",
        "decisions": [],
        # ⚠️ 缺 user_dialog_id / assistant_dialog_id
    }

# 调用方期待
result = advisor.chat(...)
print(result['user_dialog_id'])  # ❌ KeyError
```

**真实错误**：
```
KeyError: 'user_dialog_id'
```

**根因**：
- 早退路径**省了 build_context 调用**（避免限额检查失败后还做重活）
- 但调用方**不知道**是早退还是正常路径，统一 `result['user_dialog_id']`
- 字典 schema 不一致 → 集成时崩

**修复**：
```python
if quota_remaining <= 0:
    # ⚠️ PIT 修复: L4 早退时也要返回完整字段 (避免 KeyError)
    return {
        "user_id": user_id,
        "query": query,
        "response": f"[L4 跳过] 今日 LLM 限额 {_QUOTA_DAILY_T4}/日 已用完, 明日重试",
        "context": {"skills_count": 0, "history_count": 0, "memory_count": 0,
                    "holdings_count": 0, "related_sessions_count": 0, "skill_names": []},
        "fallback_level": "L4_skip",
        "decisions": [],
        "user_dialog_id": None,  # 早退时 None
        "assistant_dialog_id": None,
    }
```

**预防（API 设计铁律）**：
1. **所有 return 路径字段必须一致**（schema-first）
2. 写**返回类型 dataclass** 强制：
```python
from dataclasses import dataclass, field
from typing import Optional, List

@dataclass
class ChatResult:
    user_id: str
    query: str
    response: str
    context: dict
    fallback_level: str
    decisions: List[dict] = field(default_factory=list)
    user_dialog_id: Optional[int] = None  # 早退时 None
    assistant_dialog_id: Optional[int] = None

def chat(...) -> ChatResult:  # 强制返回完整字段
    ...
```
3. 写测试时**遍历所有早退路径** 验证 schema 一致

---

## 三、v2.2 vs v2.1 教训对比

| 维度 | v2.1 (3 教训) | v2.2 (10 教训) | 演进 |
|------|---------------|----------------|------|
| 类别 | 集成期 3 大类 | 实施期 10 大类 | +233% |
| 错误率 | 30% (3/10 集成踩坑) | 70% (7/10 是 silent 走通) | 隐性 bug ↑ |
| 检测方法 | 集成时人工发现 | 5 轮端到端测试发现 | 测试驱动 |
| 影响 | 集成阻塞 1-2 小时 | 集成阻塞 4-6 小时 | 时间 ↑ |
| 教训 | LSP 误报 / YAML 缺 / argparse 凭印象 | Schema / FTS5 / API / Path / Naming / 事务 / None / Field / Dict | 复杂度 ↑ |

**核心结论**：v2.2 阶段 bug 主要是"**集成路径走通≠真实可用**"型 silent bug，必须用端到端测试驱动而非凭编译/import 通过判断。

---

## 四、预防铁律（10 条）

1. **任何 SQLite 表查询前先 PRAGMA table_info**（列名实际是什么）
2. **FTS5 虚表必须用 messages_fts f JOIN messages m ON m.id=f.rowid** 模式
3. **FTS5 MATCH query 走字面量拼 OR 表达式**，禁用 `?` 占位
4. **复用 class 时用 `inspect.signature` 看参数顺序**，永远用关键字参数更安全
5. **sys.path.insert 前 `os.path.exists()` 验证**，打印路径调试
6. **`from module import name` 前先 `dir(module)` 看真实 API**
7. **PG 事务 try/except 后必须 `rollback()`，成功后必须 `commit()`**
8. **`dict.get(key, default)` 的 default 仅防 key 缺失，None 值要 `or default`**
9. **调用任何函数前用 `inspect.getsource` 看 docstring + 真实返回字段**
10. **所有 return 路径字段必须一致**（用 dataclass / TypedDict 强制）

---

## 五、git 提交信息模板

```bash
git commit -m "feat(<area>): <新功能>

[<阶段>] <变更点>
- ...
- ...

[<N> 真实 bug 修复 — 教训]
1. <类别>: <症状> → <修复> (根因: <根因>)
2. ...

[<评分>] <前分> → <后分> (+X.X)
- <维度 1>: <旧> → <新>
- <维度 2>: <旧> → <新>"
```

**V22-T4 真实 commit 信息**（参考）:
```
feat(v22-t4): 方案4 Hermes L3策略顾问 (3 方法 + 2 表 + Dashboard + 10 bug 修复)

[10 真实 bug 修复 — 教训]
1. FTS5 列名: m.created_at → m.timestamp (实测 schema)
2. FTS5 join: FROM messages WHERE MATCH → messages_fts f JOIN messages m ON m.id=f.rowid
3. FTS5 query: ? 占位错, 改字面量拼 OR 表达式
4. DailyQuota 参数顺序: (quota_file, limit) → (limit, quota_file)
5. intraday_hermes_agent 路径: scripts/hermes_coordination/scripts → scripts/../hermes_coordination/scripts
6. intraday_hermes_agent 函数名: load_skill_for_code → find_skill_for_code + load_skill_excerpt
7. PG 事务 abort: portfolio.positions 缺时事务 abort, 加 commit/rollback 隔离
8. session_title NoneType: LEFT JOIN None, 加 'or "(无标题)"' 兜底
9. call_llm_with_fallback 字段: fallback_level → level
10. L4 早退字典缺字段: user_dialog_id/assistant_dialog_id 补 None
```

---

## 六、待办（v3 候选）

- [ ] 写 `path_resolver.py` 统一管 `__file__.parent.parent` 路径
- [ ] 写 `api_introspector.py` 集成时自动 dump 目标 module 的真实 API
- [ ] 写 `pg_transaction_safety.py` 装饰器自动 commit/rollback
- [ ] 写 `silent_bug_detector.py` 集成后自动跑 5 轮端到端测试
- [ ] 集成 `pytest` + `coverage` 替代手写端到端测试
- [ ] 把 10 教训做成 pre-commit hook 自动检查
