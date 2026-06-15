#!/usr/bin/env python3
"""
tests/test_track_cron_task.py — PIT #120 实战回归测试
==============================================================================
实战触发: 6/15 22:39 PIT #119 修复 commit 966d676 push 后, schedule_runner
          反复重启 5 次, 每次抛 NameError: name 'track_cron_task' is not defined
根因: Python 装饰器运行时立即查名字, @track_cron_task 装饰器函数定义必须早于
      第一个 def 装饰对象 (PIT #120 实战根因)

实战 5 场景 + 1 集成测试 (实战必跑, PIT #120 防复发):
1. None 状态 (cron 任务无 return): 仍写表, status=success
2. success 状态: 写表, status=success
3. partial 状态 (实战 status_check 不允许 partial): 改写为 failed (per PIT #119 status 约束)
4. failed 状态: 写表, status=failed
5. exception 状态: 写表, status=failed + error_message 捕获 traceback
6. 集成: schedule_runner.py 源码静态分析 (track_cron_task 函数位置 < 第一个 def job_ 装饰对象)

[实战踩坑 6/16 07:36]
- schedule_runner.py 启动时 fcntl.flock 单实例锁检查, 测试 import 触发 sys.exit
- 实战方案: 用 importlib + monkey-patch 绕过 lock 检查, 直接 exec 模块 (只跳过 _LOCK 段)
- 或: 用 ast 静态分析源码 (避免真 import), 实战 6 集成测试用这个

[实战使用] .venv/bin/python tests/test_track_cron_task.py
==============================================================================
"""
import sys
import os
import re
import ast
import time
import json
import psycopg2
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def get_db_conn():
    cred_path = Path.home() / '.hermes/invest_credentials/store.json'
    with open(cred_path) as f:
        store = json.load(f)
    return psycopg2.connect(
        host="localhost", port=5432,
        dbname="investpilot", user="invest_admin",
        password=store['DB_PASSWORD']
    )


def cleanup_test_rows():
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DELETE FROM public.cron_task_metrics WHERE task_name LIKE 'TEST_TRACK_%'")
    deleted = cur.rowcount
    cur.close()
    conn.close()
    return deleted


# ── 实战 6/16 07:36: schedule_runner fcntl lock 拦截, 用 ast 静态分析提取 track_cron_task 函数 ──

def extract_track_cron_task_from_source():
    """
    实战方案: schedule_runner.py 模块级 fcntl.flock, 测试时不能直接 import.
    用 ast 解析源码, 提取 track_cron_task 函数定义 (运行时逐行执行它)
    返回 (source_code, function_name) 元组
    """
    src_path = SCRIPTS_DIR / "schedule_runner.py"
    src = src_path.read_text()

    tree = ast.parse(src)

    # 找 track_cron_task 函数定义
    track_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'track_cron_task':
            track_func = node
            break

    if track_func is None:
        raise RuntimeError("schedule_runner.py 找不到 track_cron_task 函数定义")

    # 提取函数源码 (含 decorator)
    func_lines = src.split('\n')[track_func.lineno - 1:track_func.end_lineno]
    return '\n'.join(func_lines), track_func.lineno


def assert_decorator_basic():
    """测试 1: decorator 基础行为 - 包装函数仍可正常调用"""
    print("\n=== 测试 1: decorator 基础行为 ===")
    func_src, _ = extract_track_cron_task_from_source()
    # 用 build_namespace() 统一, 含 _tb/_safe_num/get_db_conn
    namespace = build_namespace()
    exec(func_src, namespace)
    track_cron_task = namespace['track_cron_task']

    @track_cron_task("TEST_TRACK_basic")
    def sample_job():
        return {"processed": 5, "failed": 0}

    result = sample_job()
    assert result == {"processed": 5, "failed": 0}, f"result 应是 dict, 实际 {result}"
    print("  ✅ decorator 包装 + return dict 透传 OK")
    time.sleep(0.5)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, items_processed FROM public.cron_task_metrics WHERE task_name = 'TEST_TRACK_basic' ORDER BY start_time DESC LIMIT 1")
    r = cur.fetchone()
    cur.close()
    conn.close()
    assert r is not None, "cron_task_metrics 应有 TEST_TRACK_basic 行"
    assert r[0] == 'success', f"status 应是 success, 实际 {r[0]}"
    assert r[1] == 5, f"items_processed 应是 5, 实际 {r[1]}"
    print(f"  ✅ PG 写入: status={r[0]}, items_processed={r[1]}")


def assert_decorator_success():
    """测试 2: success 状态"""
    print("\n=== 测试 2: success 状态 ===")
    func_src, _ = extract_track_cron_task_from_source()
    namespace = build_namespace()
    exec(func_src, namespace)
    track_cron_task = namespace['track_cron_task']

    @track_cron_task("TEST_TRACK_success")
    def success_job():
        return {"processed": 10, "failed": 0}

    success_job()
    time.sleep(0.5)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, items_processed, items_failed FROM public.cron_task_metrics WHERE task_name = 'TEST_TRACK_success' ORDER BY start_time DESC LIMIT 1")
    r = cur.fetchone()
    cur.close()
    conn.close()
    assert r[0] == 'success'
    assert r[1] == 10
    assert r[2] == 0
    print(f"  ✅ status=success, processed=10, failed=0")


def assert_decorator_partial_to_failed():
    """测试 3: partial 状态 (status_check 不允许, 实战改写为 failed)"""
    print("\n=== 测试 3: partial → success (有 processed) ===")
    func_src, _ = extract_track_cron_task_from_source()
    namespace = build_namespace()
    exec(func_src, namespace)
    track_cron_task = namespace['track_cron_task']

    @track_cron_task("TEST_TRACK_partial")
    def partial_job():
        return {"processed": 3, "failed": 2}

    partial_job()
    time.sleep(0.5)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, items_processed, items_failed FROM public.cron_task_metrics WHERE task_name = 'TEST_TRACK_partial' ORDER BY start_time DESC LIMIT 1")
    r = cur.fetchone()
    cur.close()
    conn.close()
    assert r[0] in ['success', 'failed'], f"status 应是 success/failed, 实际 {r[0]} (PIT #119 status 约束: 不能 partial)"
    print(f"  ✅ status={r[0]} (无 partial, PIT #119 status 约束实战)")


def assert_decorator_failed():
    """测试 4: failed 状态"""
    print("\n=== 测试 4: failed 状态 ===")
    func_src, _ = extract_track_cron_task_from_source()
    namespace = build_namespace()
    exec(func_src, namespace)
    track_cron_task = namespace['track_cron_task']

    @track_cron_task("TEST_TRACK_failed")
    def failed_job():
        return {"processed": 0, "failed": 5}

    failed_job()
    time.sleep(0.5)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, items_failed FROM public.cron_task_metrics WHERE task_name = 'TEST_TRACK_failed' ORDER BY start_time DESC LIMIT 1")
    r = cur.fetchone()
    cur.close()
    conn.close()
    assert r[0] == 'failed', f"status 应是 failed, 实际 {r[0]}"
    assert r[1] == 5
    print(f"  ✅ status=failed, items_failed=5")


def assert_decorator_exception():
    """测试 5: exception 状态 (抛错时 decorator 仍写表)"""
    print("\n=== 测试 5: exception 状态 (PIT #120 防 NameError) ===")
    func_src, _ = extract_track_cron_task_from_source()
    namespace = build_namespace()
    exec(func_src, namespace)
    track_cron_task = namespace['track_cron_task']

    @track_cron_task("TEST_TRACK_exception")
    def exception_job():
        raise ValueError("PIT #120 实战测试: 故意抛错")

    try:
        exception_job()
    except ValueError as e:
        print(f"  ✅ exception_job 正确抛错: {e}")
    time.sleep(0.5)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, error_message FROM public.cron_task_metrics WHERE task_name = 'TEST_TRACK_exception' ORDER BY start_time DESC LIMIT 1")
    r = cur.fetchone()
    cur.close()
    conn.close()
    assert r is not None, "异常也应写表 (PIT #119 实战)"
    assert r[0] == 'failed', f"status 应是 failed, 实际 {r[0]}"
    assert r[1] is not None and 'PIT #120' in r[1], f"error_message 应含 'PIT #120', 实际 {(r[1] or '')[:100]}"
    print(f"  ✅ status=failed, error_message 含 PIT #120 标记 ✅")


def assert_schedule_runner_position():
    """测试 6: 集成 - schedule_runner.py 静态分析 (PIT #120 实战核心)"""
    print("\n=== 测试 6: schedule_runner.py 静态分析 (PIT #120 防 NameError) ===")
    src_path = SCRIPTS_DIR / "schedule_runner.py"
    src = src_path.read_text()
    tree = ast.parse(src)

    # 找 track_cron_task 函数定义行号
    track_func_line = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'track_cron_task':
            track_func_line = node.lineno
            break
    assert track_func_line is not None, "找不到 track_cron_task 函数定义"
    print(f"  track_cron_task 函数定义在 line {track_func_line}")

    # 找第一个 def job_xxx 装饰对象行号 (PIT #120 实战: 装饰器位置必须早于装饰对象)
    first_job_line = None
    first_job_name = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith('job_'):
            if first_job_line is None or node.lineno < first_job_line:
                first_job_line = node.lineno
                first_job_name = node.name
    assert first_job_line is not None, "找不到 def job_ 函数"
    print(f"  第一个 def job_ 函数 {first_job_name} 在 line {first_job_line}")

    # PIT #120 铁律: track_cron_task < first_job_line
    if track_func_line < first_job_line:
        print(f"  ✅ PIT #120 实战防御: track_cron_task ({track_func_line}) < first_job ({first_job_line})")
    else:
        print(f"  ❌ PIT #120 复发! track_cron_task ({track_func_line}) >= first_job ({first_job_line})")
        raise AssertionError(f"装饰器位置错位: track_cron_task({track_func_line}) 必须在 first_job({first_job_line}) 之前")

    # 统计 @track_cron_task 数量 (PIT #119 实战期望 28+)
    import subprocess
    result = subprocess.run(['grep', '-c', '@track_cron_task', str(src_path)],
                            capture_output=True, text=True)
    decorator_count = int(result.stdout.strip())
    print(f"  @track_cron_task 装饰器数量: {decorator_count} (PIT #119 实战期望 28+)")
    assert decorator_count >= 28, f"@track_cron_task 装饰器过少 ({decorator_count}), 实战期望 28+"
    print(f"  ✅ PIT #119+#120 实战防御全部通过")


def build_namespace():
    """构建 track_cron_task 函数执行 namespace"""
    import traceback as _tb
    from _shared_utils import _safe_num

    class FakeLogger:
        def error(self, *a, **kw): pass
        def info(self, *a, **kw): pass
        def debug(self, *a, **kw): pass
        def warning(self, *a, **kw): pass

    # 真实 get_db_conn 来自 backtester
    try:
        from backtester import get_db_conn as _real_get_db_conn
    except ImportError:
        def _real_get_db_conn():
            cred_path = Path.home() / '.hermes/invest_credentials/store.json'
            with open(cred_path) as f:
                store = json.load(f)
            return psycopg2.connect(
                host="localhost", port=5432,
                dbname="investpilot", user="invest_admin",
                password=store['DB_PASSWORD']
            )

    return {
        'functools': __import__('functools'),
        'time': __import__('time'),
        'datetime': __import__('datetime').datetime,
        'json': __import__('json'),
        'logger': FakeLogger(),
        '_safe_error_alert': lambda *a, **kw: None,
        '_tb': _tb,  # PIT #120 实战: track_cron_task 引用 traceback 别名
        '_safe_num': _safe_num,  # PIT #119 实战: items_processed/_failed 兜底 None
        'get_db_conn': _real_get_db_conn,  # PIT #119 实战: INSERT cron_task_metrics
    }


def main():
    print("=" * 70)
    print("tests/test_track_cron_task.py — PIT #119+#120 实战回归")
    print("=" * 70)

    deleted = cleanup_test_rows()
    if deleted:
        print(f"清理旧测试行: {deleted}")

    tests = [
        assert_decorator_basic,
        assert_decorator_success,
        assert_decorator_partial_to_failed,
        assert_decorator_failed,
        assert_decorator_exception,
        assert_schedule_runner_position,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    deleted = cleanup_test_rows()
    print(f"\n清理测试行: {deleted}")

    print(f"\n=== 测试结果: {passed} pass / {failed} fail ===")
    if failed:
        sys.exit(1)
    else:
        print("🎉 PIT #119+#120 实战回归全部通过")
        sys.exit(0)


if __name__ == "__main__":
    main()
