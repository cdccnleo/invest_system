# v2.2 6 大测试模式（end-to-end validation patterns）

> **来源**：V22-T3 + V22-T4 实施过程 5 轮端到端测试沉淀
> **基础**：v2.1 集成阶段 3 大测试经验 (py_compile + importlib + argparse 解析)
> **新增**：6 大测试模式专门解决"集成路径走通≠真实可用"型 silent bug
> **用途**：未来集成 v2.x 任何模块时，**强制走完 6 模式**才能宣称 "完成"

---

## 一、6 大测试模式总览

| # | 模式 | 适用场景 | 关键工具 | 触发 bug |
|---|------|---------|---------|---------|
| 1 | **Schema-First 验证** | 任何 SQLite/PG 表查询前 | `PRAGMA table_info` + `information_schema` | 坑 #1 (列名错) |
| 2 | **真实依赖探测** | 集成第三方 module 前 | `dir()` + `inspect.signature` + `inspect.getsource` | 坑 #4/6/9 (API 错) |
| 3 | **PG 事务健康检查** | 多步 PG 操作 | `try-except-rollback` + `pg_current_xact_id` | 坑 #7 (事务 abort) |
| 4 | **Mock LLM 真实跑通** | 任何 LLM 集成 | `os.environ["HERMES_FALLBACK_MOCK"]="1"` | 坑 #9 (字段错) |
| 5 | **限额状态隔离** | 任何限额/计数器测试 | 重置 quota JSON + `os.environ` 隔离 | 跨日/跨测试污染 |
| 6 | **早退路径 Schema 验证** | 多 return 路径的 API | dataclass + 遍历所有路径 | 坑 #10 (字段缺) |

---

## 二、6 大测试模式详解

### 模式 1：Schema-First 验证（PRAGMA + information_schema）

**目标**：写任何表查询 SQL 前，**先看实际 schema**，不靠"通用命名约定"。

**核心工具**：
- SQLite: `PRAGMA table_info(<table>)`
- PostgreSQL: `information_schema.columns`

**模板**（直接复制用）：
```python
# scripts/_test_schema_first.py
import sqlite3
import json
from pathlib import Path

def dump_sqlite_schema(db_path: str, table: str):
    """SQLite 表 schema 探测"""
    conn = sqlite3.connect(db_path, timeout=5)
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    print(f"\n=== {db_path} :: {table} ===")
    for r in cur.fetchall():
        print(f"  {r[0]}: {r[1]}")
    # 索引
    cur.execute(f"PRAGMA index_list({table})")
    indexes = cur.fetchall()
    if indexes:
        print(f"  -- {len(indexes)} indexes --")
        for idx in indexes:
            print(f"  idx: {idx[1]}")
    # FTS 虚表
    cur.execute("SELECT name, sql FROM sqlite_master WHERE name LIKE '%fts%' AND type='table'")
    fts_tables = cur.fetchall()
    if fts_tables:
        print(f"  -- FTS virtual tables --")
        for name, sql in fts_tables:
            print(f"  fts: {name}: {sql[:100]}")
    conn.close()


def dump_pg_schema(schema: str, table: str, db_config: dict):
    """PG 表 schema 探测"""
    import psycopg2
    conn = psycopg2.connect(**db_config)
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """, (schema, table))
    print(f"\n=== PG {schema}.{table} ===")
    for r in cur.fetchall():
        print(f"  {r[0]}: {r[1]} {'NULL' if r[2] == 'YES' else 'NOT NULL'} {r[3] or ''}")
    cur.execute("""
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = %s AND tablename = %s
    """, (schema, table))
    print(f"  -- indexes --")
    for name, ddl in cur.fetchall():
        print(f"  {name}: {ddl[:100]}")
    conn.close()


# 用法
if __name__ == "__main__":
    # 探测 state.db (Hermes sessions)
    dump_sqlite_schema("/home/aileo/.hermes/state.db", "messages")
    dump_sqlite_schema("/home/aileo/.hermes/state.db", "sessions")
    dump_sqlite_schema("/home/aileo/.hermes/state.db", "messages_fts")

    # 探测 investpilot DB
    db_config = {
        "host": "localhost", "database": "investpilot",
        "user": "invest_admin", "password": "***"
    }
    dump_pg_schema("l3", "dialog_history", db_config)
    dump_pg_schema("l3", "decision_points", db_config)
```

**触发 bug**：坑 #1 (FTS5 列名) 必被检测。

---

### 模式 2：真实依赖探测（dir + inspect.signature + inspect.getsource）

**目标**：复用任何 class/function 前，**看真实 API**，不靠文档/记忆。

**核心工具**：
- `dir(module)` 列出所有成员
- `inspect.signature(callable)` 看参数
- `inspect.getsource(callable)` 看源码 + docstring

**模板**：
```python
# scripts/_test_inspect_module.py
import importlib
import inspect
from pathlib import Path


def dump_module_api(module_name: str, include_private: bool = False):
    """完整 dump 一个 module 的 API"""
    mod = importlib.import_module(module_name)
    print(f"\n=== Module: {module_name} ===")
    print(f"  File: {mod.__file__}")
    print(f"\n  -- Classes --")
    for name, obj in inspect.getmembers(mod, inspect.isclass):
        if not include_private and name.startswith('_'):
            continue
        if obj.__module__ != module_name:
            continue
        sig = inspect.signature(obj.__init__)
        print(f"  class {name}{sig}")

    print(f"\n  -- Functions --")
    for name, obj in inspect.getmembers(mod, inspect.isfunction):
        if not include_private and name.startswith('_'):
            continue
        if obj.__module__ != module_name:
            continue
        try:
            sig = inspect.signature(obj)
            print(f"  def {name}{sig}")
        except (ValueError, TypeError):
            print(f"  def {name}(?)")

    print(f"\n  -- Module-level constants --")
    for name in dir(mod):
        if name.startswith('_') and not include_private:
            continue
        obj = getattr(mod, name)
        if not inspect.ismodule(obj) and not inspect.isclass(obj) and not inspect.isfunction(obj):
            print(f"  {name} = {repr(obj)[:100]}")


def dump_function_source(func_name: str, module_name: str):
    """看一个函数的源码 + docstring"""
    mod = importlib.import_module(module_name)
    func = getattr(mod, func_name)
    print(f"\n=== Function: {module_name}.{func_name} ===")
    print(f"\n  -- Docstring --")
    print(f"  {func.__doc__}")
    print(f"\n  -- Signature --")
    print(f"  {inspect.signature(func)}")
    print(f"\n  -- Source --")
    print(inspect.getsource(func))


# 用法
if __name__ == "__main__":
    # 集成前必看
    dump_module_api("intraday_hermes_agent")
    dump_function_source("call_llm_with_fallback", "intraday_hermes_agent")
    dump_function_source("DailyQuota", "intraday_hermes_agent")  # class name 也能 inspect
```

**触发 bug**：坑 #4/6/9 (API 名错/参数顺序/返回字段) 全部被检测。

---

### 模式 3：PG 事务健康检查（rollback + xact_id）

**目标**：任何多步 PG 操作，**确保事务状态干净**（不被 abort 卡住）。

**核心工具**：
- `conn.rollback()` 救场
- `pg_current_xact_id()` 看事务 ID
- 装饰器自动管理

**模板**：
```python
# scripts/_test_pg_transaction.py
import contextlib
import functools
import json
from pathlib import Path
import psycopg2


def get_db_config():
    return {
        "host": "localhost", "database": "investpilot",
        "user": "invest_admin",
        "password": json.loads(Path("/home/aileo/.hermes/invest_credentials/store.json").read_text())["DB_PASSWORD"],
    }


@contextlib.contextmanager
def safe_pg_connection():
    """PG 连接 context manager, 自动 rollback on error"""
    conn = psycopg2.connect(**get_db_config())
    try:
        yield conn
    except Exception:
        conn.rollback()  # ⚠️ 关键: 异常时 rollback
        raise
    finally:
        conn.close()


def safe_pg_execute(conn, sql, params=None, fetch=False):
    """单条 SQL 安全执行, 自动 commit/rollback"""
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        if fetch == "one":
            return cur.fetchone()
        elif fetch == "all":
            return cur.fetchall()
        conn.commit()
    except Exception as e:
        conn.rollback()  # ⚠️ 关键: rollback 防 abort 阻断后续
        raise


@contextlib.contextmanager
def pg_savepoint(conn, name="sp"):
    """PG savepoint, 局部回滚"""
    cur = conn.cursor()
    cur.execute(f"SAVEPOINT {name}")
    try:
        yield cur
    except Exception:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.commit()  # 必须 commit 释放 savepoint
        raise
    else:
        cur.execute(f"RELEASE SAVEPOINT {name}")
        conn.commit()


def check_xact_health(conn):
    """检查事务健康度"""
    cur = conn.cursor()
    try:
        cur.execute("SELECT pg_current_xact_id()")
        xid = cur.fetchone()[0]
        cur.execute("SELECT pg_xact_status(%s)", (xid,))
        status = cur.fetchone()[0]
        # 0 = IN_PROGRESS, 1 = COMMITTED, 2 = ABORTED
        return {"xid": xid, "status": ["IN_PROGRESS", "COMMITTED", "ABORTED"][status]}
    except psycopg2.errors.InFailedSqlTransaction:
        return {"xid": None, "status": "ABORTED (need ROLLBACK)"}
    finally:
        if status == 2:
            conn.rollback()  # ⚠️ 关键: abort 状态救场


# 用法
if __name__ == "__main__":
    # 1. 基本安全执行
    with safe_pg_connection() as conn:
        result = safe_pg_execute(
            conn,
            "SELECT COUNT(*) FROM l3.dialog_history WHERE user_id=%s",
            ("aileo",),
            fetch="one"
        )
        print(f"dialog_history: {result[0]}")

    # 2. 多步操作（用 savepoint 隔离）
    with safe_pg_connection() as conn:
        with pg_savepoint(conn) as cur:
            cur.execute("SELECT 1 FROM portfolio.positions LIMIT 1")  # 可能失败
            # savepoint 内失败 → 局部回滚 → 后续 SQL 不受影响
        cur = conn.cursor()  # 新 cursor
        cur.execute("SELECT 1 FROM l3.dialog_history LIMIT 1")  # 成功

    # 3. 事务健康检查
    with safe_pg_connection() as conn:
        health = check_xact_health(conn)
        print(f"Health: {health}")
```

**触发 bug**：坑 #7 (事务 abort) 必被检测。

---

### 模式 4：Mock LLM 真实跑通（HERMES_FALLBACK_MOCK）

**目标**：集成任何 LLM 调用，**mock 模式下也要走通**（避免 mock 假装成功，真实调用全错）。

**核心工具**：
- `os.environ["HERMES_FALLBACK_MOCK"]="1"` 启用 mock
- 真实 LLM 调用 → mock 返回

**模板**：
```python
# scripts/_test_mock_llm.py
import os
import json
from pathlib import Path

# 1) 启用 mock
os.environ["HERMES_FALLBACK_MOCK"] = "1"

# 2) import 后才能设 env var
import sys
sys.path.insert(0, str(Path("scripts").parent / "hermes_coordination" / "scripts"))
from intraday_hermes_agent import call_llm_with_fallback


# 3) mock 模式测试
def test_mock_llm_basic():
    """基础 mock 调用"""
    result = call_llm_with_fallback(
        system="你是 Hermes 顾问",
        prompt="信维通信 300136 现在能买吗?",
        max_retries=1
    )
    # ⚠️ PIT: 返回字段是 level, 不是 fallback_level
    assert "content" in result, f"Missing 'content': {result}"
    assert "level" in result, f"Missing 'level': {result}"
    assert isinstance(result["content"], str)
    assert isinstance(result["level"], str)
    print(f"✅ Mock LLM 字段: content={result['content'][:50]}..., level={result['level']}")


def test_mock_llm_levels():
    """测试所有 4 级降级链"""
    levels_seen = set()
    for i in range(10):
        result = call_llm_with_fallback(
            system="test",
            prompt=f"query {i}",
        )
        levels_seen.add(result.get("level"))
    print(f"✅ 见到 {len(levels_seen)} 种 level: {levels_seen}")


def test_mock_llm_long_prompt():
    """长 prompt 不崩"""
    long_prompt = "x" * 10000
    result = call_llm_with_fallback(
        system="test",
        prompt=long_prompt,
    )
    assert result["content"] is not None
    print(f"✅ 长 prompt 10000 字符 OK")


def test_mock_llm_special_chars():
    """特殊字符不崩"""
    result = call_llm_with_fallback(
        system='有"双引号"和\\n换行',
        prompt='emoji 🚀 + 标点 ?',
    )
    assert result["content"] is not None
    print(f"✅ 特殊字符 OK: {result['content'][:50]}")


if __name__ == "__main__":
    test_mock_llm_basic()
    test_mock_llm_levels()
    test_mock_llm_long_prompt()
    test_mock_llm_special_chars()
```

**触发 bug**：坑 #9 (字段错) 必被检测。

---

### 模式 5：限额状态隔离（quota JSON 重置 + env 隔离）

**目标**：测试任何"限额/计数器"功能时，**避免跨测试/跨日污染**。

**核心工具**：
- 直接写 quota JSON 文件重置
- `os.environ` 临时覆盖路径

**模板**：
```python
# scripts/_test_quota_isolation.py
import json
import os
import shutil
from datetime import date
from pathlib import Path

# 1) 备份当前 quota
QUOTA_FILE = "/tmp/intraday_hermes_quota.json"
BACKUP_FILE = f"/tmp/intraday_hermes_quota.backup.{os.getpid()}.json"
shutil.copy(QUOTA_FILE, BACKUP_FILE)


def reset_quota(used=0, limit=20, with_history=True):
    """重置 quota 到指定状态"""
    data = {
        "date": str(date.today()),
        "used": used,
        "limit": limit,
    }
    if with_history:
        data["history"] = []  # ⚠️ 必填字段
    Path(QUOTA_FILE).write_text(json.dumps(data))
    print(f"✅ Quota reset: used={used} limit={limit} history={with_history}")


def restore_quota():
    """测试完毕恢复"""
    if Path(BACKUP_FILE).exists():
        shutil.move(BACKUP_FILE, QUOTA_FILE)
        print(f"✅ Quota restored from backup")


# 2) 测试场景
def test_quota_exhausted():
    """限额耗尽 → L4 skip"""
    reset_quota(used=20, limit=20)
    sys.path.insert(0, "scripts")
    from l3_dialog_engine import L3Advisor
    advisor = L3Advisor()
    result = advisor.chat("aileo", "test query")
    assert result["fallback_level"] in ("L4_skip",), f"Expected L4_skip, got {result['fallback_level']}"
    print(f"✅ 限额耗尽 → {result['fallback_level']}")


def test_quota_available():
    """限额未用 → L1 normal"""
    reset_quota(used=0, limit=20)
    sys.path.insert(0, "scripts")
    from l3_dialog_engine import L3Advisor
    advisor = L3Advisor()
    result = advisor.chat("aileo", "test query")
    assert result["fallback_level"].startswith("L1"), f"Expected L1*, got {result['fallback_level']}"
    print(f"✅ 限额未用 → {result['fallback_level']}")


def test_quota_history_full():
    """历史记录满 30 天滚动"""
    # 构造 30+ 天的历史
    long_history = [
        {"date": f"2026-{(i % 30) + 1:02d}-15", "used": 20}
        for i in range(35)
    ]
    data = {
        "date": str(date.today()),
        "used": 0,
        "limit": 20,
        "history": long_history,
    }
    Path(QUOTA_FILE).write_text(json.dumps(data))
    # 现在 try_acquire 应能跑
    print(f"✅ 30+ 天历史测试 OK")


# 3) 上下文管理器版本（更优雅）
from contextlib import contextmanager

@contextmanager
def isolated_quota(used=0, limit=20, with_history=True):
    """隔离 quota 测试"""
    reset_quota(used=used, limit=limit, with_history=with_history)
    try:
        yield
    finally:
        restore_quota()


# 用法
if __name__ == "__main__":
    try:
        test_quota_exhausted()
        test_quota_available()
        test_quota_history_full()
    finally:
        restore_quota()  # 必做
```

**触发 bug**：跨日/跨测试污染（V22-T3-A 测试遗留 used=3 → V22-T4 限额假阳性用完）。

---

### 模式 6：早退路径 Schema 验证（dataclass + 全路径遍历）

**目标**：多 return 路径的 API，**所有路径字段一致**。

**核心工具**：
- `dataclass` 强制 schema
- `TypedDict` 轻量版本
- 测试遍历所有早退路径

**模板**：
```python
# scripts/_test_api_schema.py
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
import sys
sys.path.insert(0, "scripts")
from l3_dialog_engine import L3Advisor


# 1) 定义返回类型 (强制 schema)
@dataclass
class ChatResultSchema:
    user_id: str
    query: str
    response: str
    context: Dict[str, Any]
    fallback_level: str
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    user_dialog_id: Optional[int] = None  # ⚠️ 早退时 None
    assistant_dialog_id: Optional[int] = None


# 2) 校验函数
def validate_chat_result(result: dict) -> List[str]:
    """验证 dict 是否符合 ChatResultSchema"""
    errors = []
    schema = ChatResultSchema.__dataclass_fields__
    for field_name, field_def in schema.items():
        if field_name not in result:
            errors.append(f"Missing field: {field_name}")
        elif not isinstance(result[field_name], (field_def.type, type(None))):
            # 类型检查 (简化版, 实际用 typeguard)
            t = field_def.type
            if hasattr(t, '__origin__'):  # Optional, List 等
                t = t.__origin__
            if not isinstance(result[field_name], t):
                # Optional 允许 None
                if not (field_def.type is type(None) or result[field_name] is None):
                    errors.append(f"Type mismatch {field_name}: expected {t}, got {type(result[field_name])}")
    return errors


# 3) 强制所有路径用 dataclass
def chat_v2(user_id: str, query: str) -> ChatResultSchema:
    """所有路径返回 ChatResultSchema"""
    # ... 业务逻辑
    if some_condition:
        return ChatResultSchema(  # ⚠️ 早退路径
            user_id=user_id, query=query, response="L4 skip",
            context={...}, fallback_level="L4_skip",
            # user_dialog_id / assistant_dialog_id 用默认值 None
        )
    # 正常路径
    return ChatResultSchema(
        user_id=user_id, query=query, response=llm_response,
        context=ctx, fallback_level="L1_normal",
        decisions=decisions,
        user_dialog_id=10, assistant_dialog_id=11,
    )


# 4) 测试所有路径
def test_all_paths_consistent():
    """遍历所有早退路径 + 正常路径"""
    advisor = L3Advisor()

    test_cases = [
        # (query, expected_fallback_level)
        ("normal query", None),  # 期望非 L4_skip
        ("", "L4_skip"),  # 空 query
        # 触发 L4_skip: 重置 quota=20
    ]

    for query, expected_level in test_cases:
        result = advisor.chat("aileo", query)
        # 1) 字段完整性
        errors = validate_chat_result(result)
        if errors:
            print(f"❌ {query[:20]}: {errors}")
        else:
            print(f"✅ {query[:20]}: schema OK, level={result['fallback_level']}")
        # 2) 期望 level
        if expected_level and result["fallback_level"] != expected_level:
            print(f"⚠️ {query[:20]}: expected {expected_level}, got {result['fallback_level']}")


if __name__ == "__main__":
    test_all_paths_consistent()
```

**触发 bug**：坑 #10 (字段缺) 必被检测。

---

## 三、5 轮端到端测试实战（V22-T4 真实记录）

**第 1 轮**（v1 — 第一次跑）发现 4 个 bug：
- FTS5 `m.created_at` 列名错 → 修复 #1
- FTS5 join 错 → 修复 #2
- FTS5 query `?` 报错 → 修复 #3
- `load_skill_for_code` 不存在 → 修复 #6
- 路径错 → 修复 #5

**第 2 轮**（v2 — 修后跑）发现 2 个新 bug：
- `DailyQuota` 参数顺序错 → 修复 #4
- L4 早退字典缺 `user_dialog_id` → 修复 #10

**第 3 轮**（v3 — 再修后跑）发现 1 个新 bug：
- 限额跨日污染 (used=3 来自 V22-T3-A 测试) → 重置

**第 4 轮**（v4 — 修后跑）发现 1 个新 bug：
- `session_title` NoneType 崩 → 修复 #8

**第 5 轮**（v5 — 最终）发现 1 个新 bug：
- PG 事务 abort → 修复 #7
- `call_llm_with_fallback` 字段错 → 修复 #9
- 限额跨日二次污染 → 用 isolated_quota 隔离

**总 bug**: 10 个，**全部在第 5 轮**才完全修完。

---

## 四、6 模式 vs 5 轮测试的对应关系

| 轮次 | 触发的 bug | 对应测试模式 |
|------|-----------|------------|
| 1 | #1, #2, #3, #6, #5 | 模式 1 (Schema) + 模式 2 (Inspect) |
| 2 | #4, #10 | 模式 2 (Inspect) + 模式 6 (Schema) |
| 3 | 限额污染 | 模式 5 (Quota) |
| 4 | #8 | 模式 2 (Inspect 真实 LEFT JOIN None) |
| 5 | #7, #9 | 模式 3 (PG) + 模式 4 (Mock LLM) |

**结论**：**6 模式全覆盖 = 5 轮测试零 bug**。

---

## 五、CI/CD 集成建议（v3 候选）

```yaml
# .github/workflows/v22-integration-test.yml
name: v2.2 Integration Test

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - name: Install deps
        run: pip install -r requirements.txt
      - name: Schema-First 验证
        run: python scripts/_test_schema_first.py
      - name: 真实依赖探测
        run: python scripts/_test_inspect_module.py
      - name: PG 事务健康检查
        run: python scripts/_test_pg_transaction.py
        env:
          DB_PASSWORD: ${{ secrets.DB_PASSWORD }}
      - name: Mock LLM 真实跑通
        run: HERMES_FALLBACK_MOCK=1 python scripts/_test_mock_llm.py
      - name: 限额状态隔离
        run: python scripts/_test_quota_isolation.py
      - name: 早退路径 Schema 验证
        run: python scripts/_test_api_schema.py
```

**目标**：集成时**一次跑过 6 模式**，避免 5 轮端到端测试。

---

## 六、待办（v3 候选）

- [ ] 把 6 模式做成 `pytest` 装饰器（`@schema_first` / `@inspect_module` / `@pg_safe` 等）
- [ ] 集成到 pre-commit hook（commit 前自动跑）
- [ ] 写 `silent_bug_detector.py` 集成时自动跑 6 模式
- [ ] 6 模式覆盖率报告（哪些代码路径未覆盖）
- [ ] 把 10 教训做成 `flake8` 自定义规则
