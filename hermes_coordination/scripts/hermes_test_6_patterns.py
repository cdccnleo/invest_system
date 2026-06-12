#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
6 大测试模式 真实可执行脚本（hermes_invest_system）
=====================================================
基于 v2.2 实施阶段 (V22-T3 + V22-T4) 5 轮端到端测试沉淀。
每个模式都是独立的可执行函数, 可单独运行也可批量跑。

Usage:
    # 跑全部 6 模式
    python scripts/hermes_test_6_patterns.py --all

    # 跑指定模式
    python scripts/hermes_test_6_patterns.py --pattern 1

    # 跑模式 1+2+5
    python scripts/hermes_test_6_patterns.py --pattern 1 --pattern 2 --pattern 5

    # 输出 JSON 报告
    python scripts/hermes_test_6_patterns.py --all --json
"""

import argparse
import contextlib
import dataclasses
import importlib
import inspect
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ⚠️ 启用 mock LLM (必须在 import 前)
os.environ.setdefault("HERMES_FALLBACK_MOCK", "1")

# 路径设置
INVEST_ROOT = Path("/home/aileo/invest_system")
SCRIPTS_DIR = INVEST_ROOT / "scripts"
HERMES_COORD_DIR = INVEST_ROOT / "hermes_coordination"
HERMES_SCRIPTS_DIR = HERMES_COORD_DIR / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(HERMES_SCRIPTS_DIR))

# 凭据
CREDS_FILE = Path("/home/aileo/.hermes/invest_credentials/store.json")
# ⚠️ PIT 修复: l3_dialog_engine._HERMES_QUOTA_T4 实际用 /tmp/hermes_llm_quota.json
# 不是 intraday_hermes_agent 默认的 /tmp/intraday_hermes_quota.json
QUOTA_FILE = "/tmp/hermes_llm_quota.json"
QUOTA_BACKUP = f"/tmp/hermes_llm_quota.backup.{os.getpid()}.json"


def get_db_config() -> dict:
    """从 ~/.hermes/invest_credentials/store.json 取 PG 密码"""
    pw = json.loads(CREDS_FILE.read_text())["DB_PASSWORD"]
    return {
        "host": "localhost", "database": "investpilot",
        "user": "invest_admin", "password": pw,
    }


# ============================================================
# 模式 1: Schema-First 验证
# ============================================================
def pattern_1_schema_first() -> Tuple[bool, List[str]]:
    """探测 SQLite + PG 表的 schema, 验证列名/索引/触发器"""
    errors = []
    print("\n=== [模式 1] Schema-First 验证 ===")
    try:
        # 1.1 SQLite (state.db)
        state_db = Path("/home/aileo/.hermes/state.db")
        if not state_db.exists():
            errors.append("state.db 不存在")
            return False, errors
        conn = sqlite3.connect(str(state_db), timeout=5)
        cur = conn.cursor()
        # 关键表
        for table in ["messages", "sessions", "messages_fts"]:
            cur.execute(f"PRAGMA table_info({table})")
            cols = cur.fetchall()
            if not cols:
                errors.append(f"SQLite 表 {table} 不存在或无列")
                continue
            col_names = [c[1] for c in cols]
            print(f"  ✅ SQLite {table}: {len(col_names)} 列")
        # 触发器
        cur.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'messages_fts%'")
        triggers = [r[0] for r in cur.fetchall()]
        if not triggers:
            errors.append("messages_fts 触发器缺失")
        else:
            print(f"  ✅ FTS 触发器: {len(triggers)} 个")
        conn.close()

        # 1.2 PG (investpilot)
        try:
            import psycopg2
            conn = psycopg2.connect(**get_db_config())
            cur = conn.cursor()
            for schema, table in [("l3", "dialog_history"), ("l3", "decision_points")]:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """, (schema, table))
                cols = [r[0] for r in cur.fetchall()]
                if not cols:
                    errors.append(f"PG {schema}.{table} 不存在")
                    continue
                print(f"  ✅ PG {schema}.{table}: {cols}")
            conn.close()
        except Exception as e:
            errors.append(f"PG 连接/查询失败: {e}")

        success = len(errors) == 0
        if success:
            print(f"  ✅ 模式 1 通过")
        else:
            print(f"  ❌ 模式 1 失败: {errors}")
        return success, errors
    except Exception as e:
        return False, [f"模式 1 异常: {e}"]


# ============================================================
# 模式 2: 真实依赖探测
# ============================================================
def pattern_2_inspect_module() -> Tuple[bool, List[str]]:
    """探测 intraday_hermes_agent + l3_dialog_engine 的真实 API"""
    errors = []
    print("\n=== [模式 2] 真实依赖探测 ===")
    try:
        # 2.1 intraday_hermes_agent
        try:
            mod = importlib.import_module("intraday_hermes_agent")
            expected = ["DailyQuota", "find_skill_for_code", "load_skill_excerpt", "call_llm_with_fallback"]
            for name in expected:
                if not hasattr(mod, name):
                    errors.append(f"intraday_hermes_agent.{name} 缺失")
                else:
                    obj = getattr(mod, name)
                    print(f"  ✅ intraday_hermes_agent.{name}")
            # 验证 DailyQuota 签名
            sig = inspect.signature(mod.DailyQuota.__init__)
            params = list(sig.parameters.keys())
            if params != ["self", "daily_limit", "quota_file"]:
                errors.append(f"DailyQuota.__init__ 签名错: {params}")
            else:
                print(f"  ✅ DailyQuota 签名: {params}")
        except Exception as e:
            errors.append(f"intraday_hermes_agent import 失败: {e}")

        # 2.2 l3_dialog_engine.L3Advisor
        try:
            from l3_dialog_engine import L3Advisor
            methods = ["chat", "build_context", "post_decision"]
            for m in methods:
                if not hasattr(L3Advisor, m):
                    errors.append(f"L3Advisor.{m} 缺失")
                else:
                    print(f"  ✅ L3Advisor.{m}")
        except Exception as e:
            errors.append(f"l3_dialog_engine.L3Advisor import 失败: {e}")

        # 2.3 call_llm_with_fallback 返回字段
        try:
            from intraday_hermes_agent import call_llm_with_fallback
            sig = inspect.signature(call_llm_with_fallback)
            print(f"  ✅ call_llm_with_fallback{sig}")
            # 看 docstring
            if call_llm_with_fallback.__doc__:
                if "level" in call_llm_with_fallback.__doc__:
                    print(f"  ✅ docstring 提到 'level' 字段")
                else:
                    errors.append("call_llm_with_fallback docstring 未提 'level' 字段")
        except Exception as e:
            errors.append(f"call_llm_with_fallback inspect 失败: {e}")

        success = len(errors) == 0
        if success:
            print(f"  ✅ 模式 2 通过")
        else:
            print(f"  ❌ 模式 2 失败: {errors}")
        return success, errors
    except Exception as e:
        return False, [f"模式 2 异常: {e}"]


# ============================================================
# 模式 3: PG 事务健康检查
# ============================================================
def pattern_3_pg_transaction() -> Tuple[bool, List[str]]:
    """验证 PG 事务 rollback + savepoint + 健康度"""
    errors = []
    print("\n=== [模式 3] PG 事务健康检查 ===")
    try:
        import psycopg2
        conn = psycopg2.connect(**get_db_config())

        # 3.1 基础 rollback 测试
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM l3.dialog_history LIMIT 1")
            cur.execute("SELECT 1 FROM portfolio.positions LIMIT 1")  # 不存在, 应失败
            conn.commit()
        except psycopg2.errors.UndefinedTable:
            conn.rollback()  # 救场
            print(f"  ✅ 异常后 rollback 成功")
        except Exception as e:
            conn.rollback()
            errors.append(f"基础 rollback 失败: {e}")

        # 3.2 事务健康度
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_current_xact_id()")
            xid = cur.fetchone()[0]
            print(f"  ✅ 事务 ID: {xid}")
            conn.rollback()  # 释放
        except Exception as e:
            errors.append(f"事务健康度检查失败: {e}")

        # 3.3 savepoint 测试
        try:
            cur = conn.cursor()
            cur.execute("SAVEPOINT sp_test")
            cur.execute("SELECT 1 FROM portfolio.positions LIMIT 1")  # 失败
        except psycopg2.errors.UndefinedTable:
            cur.execute("ROLLBACK TO SAVEPOINT sp_test")
            conn.commit()  # 释放 savepoint
            print(f"  ✅ savepoint 隔离成功")
            # 后续 SQL 应可用
            cur.execute("SELECT 1 FROM l3.dialog_history LIMIT 1")
            print(f"  ✅ savepoint 后续 SQL OK")
        except Exception as e:
            errors.append(f"savepoint 测试失败: {e}")
        finally:
            conn.close()

        success = len(errors) == 0
        if success:
            print(f"  ✅ 模式 3 通过")
        else:
            print(f"  ❌ 模式 3 失败: {errors}")
        return success, errors
    except Exception as e:
        return False, [f"模式 3 异常: {e}"]


# ============================================================
# 模式 4: Mock LLM 真实跑通
# ============================================================
def pattern_4_mock_llm() -> Tuple[bool, List[str]]:
    """用 mock LLM 跑通所有调用, 验证返回字段"""
    errors = []
    print("\n=== [模式 4] Mock LLM 真实跑通 ===")
    try:
        from intraday_hermes_agent import call_llm_with_fallback

        # 4.1 基础调用
        result = call_llm_with_fallback("test system", "test prompt")
        if "content" not in result:
            errors.append("'content' 字段缺失")
        if "level" not in result:
            errors.append("'level' 字段缺失 (注意: 不是 fallback_level)")
        else:
            print(f"  ✅ level: {result.get('level')}")
        if "error" not in result:
            errors.append("'error' 字段缺失")

        # 4.2 多调用看不同 level
        levels_seen = set()
        for i in range(5):
            r = call_llm_with_fallback("system", f"query {i}")
            levels_seen.add(r.get("level"))
        print(f"  ✅ 见到 {len(levels_seen)} 种 level: {levels_seen}")

        # 4.3 长 prompt
        long_prompt = "x" * 5000
        r = call_llm_with_fallback("system", long_prompt)
        if not r.get("content"):
            errors.append("长 prompt 返回空")
        else:
            print(f"  ✅ 长 prompt 5000 字符 OK")

        # 4.4 特殊字符
        r = call_llm_with_fallback('有"引号"和\\n', 'emoji 🚀 + ?')
        if not r.get("content"):
            errors.append("特殊字符返回空")
        else:
            print(f"  ✅ 特殊字符 OK")

        success = len(errors) == 0
        if success:
            print(f"  ✅ 模式 4 通过")
        else:
            print(f"  ❌ 模式 4 失败: {errors}")
        return success, errors
    except Exception as e:
        return False, [f"模式 4 异常: {e}"]


# ============================================================
# 模式 5: 限额状态隔离
# ============================================================
def pattern_5_quota_isolation() -> Tuple[bool, List[str]]:
    """验证 quota JSON 重置 + 测试后恢复"""
    errors = []
    print("\n=== [模式 5] 限额状态隔离 ===")
    try:
        # 5.1 备份 + 重置
        if Path(QUOTA_FILE).exists():
            shutil.copy(QUOTA_FILE, QUOTA_BACKUP)
        reset_data = {"date": str(date.today()), "used": 0, "limit": 20, "history": []}
        Path(QUOTA_FILE).write_text(json.dumps(reset_data))
        print(f"  ✅ Quota 重置: {reset_data}")

        # 5.2 验证文件 schema
        data = json.loads(Path(QUOTA_FILE).read_text())
        for key in ["date", "used", "limit", "history"]:
            if key not in data:
                errors.append(f"quota JSON 缺字段: {key}")
        if not errors:
            print(f"  ✅ Quota JSON schema OK: {list(data.keys())}")

        # 5.3 限额耗尽场景
        exhausted_data = {"date": str(date.today()), "used": 20, "limit": 20, "history": []}
        Path(QUOTA_FILE).write_text(json.dumps(exhausted_data))
        from intraday_hermes_agent import DailyQuota
        quota = DailyQuota(20, Path(QUOTA_FILE))
        remaining = quota.get_remaining()
        if remaining != 0:
            errors.append(f"限额耗尽时 get_remaining 应为 0, 实际 {remaining}")
        else:
            print(f"  ✅ 限额耗尽: get_remaining=0")

        # 5.4 限额可用场景
        available_data = {"date": str(date.today()), "used": 0, "limit": 20, "history": []}
        Path(QUOTA_FILE).write_text(json.dumps(available_data))
        quota = DailyQuota(20, Path(QUOTA_FILE))
        remaining = quota.get_remaining()
        if remaining != 20:
            errors.append(f"限额未用时 get_remaining 应为 20, 实际 {remaining}")
        else:
            print(f"  ✅ 限额未用: get_remaining=20")

        # 5.5 跨日滚动
        yesterday_data = {"date": "2020-01-01", "used": 15, "limit": 20, "history": []}
        Path(QUOTA_FILE).write_text(json.dumps(yesterday_data))
        quota = DailyQuota(20, Path(QUOTA_FILE))
        remaining = quota.get_remaining()
        if remaining != 20:  # 跨日应重置
            errors.append(f"跨日应重置, 实际 get_remaining={remaining}")
        else:
            print(f"  ✅ 跨日滚动: 昨日 used=15 今日重置")

        success = len(errors) == 0
        if success:
            print(f"  ✅ 模式 5 通过")
        else:
            print(f"  ❌ 模式 5 失败: {errors}")
        return success, errors
    except Exception as e:
        return False, [f"模式 5 异常: {e}"]
    finally:
        # 恢复
        if Path(QUOTA_BACKUP).exists():
            shutil.move(QUOTA_BACKUP, QUOTA_FILE)
            print(f"  ✅ Quota 已恢复")


# ============================================================
# 模式 6: 早退路径 Schema 验证
# ============================================================
def pattern_6_api_schema() -> Tuple[bool, List[str]]:
    """验证 L3Advisor.chat 所有返回路径字段一致"""
    errors = []
    print("\n=== [模式 6] 早退路径 Schema 验证 ===")
    try:
        from l3_dialog_engine import L3Advisor
        advisor = L3Advisor()

        # 6.1 必需字段
        required_fields = {
            "user_id": str,
            "query": str,
            "response": str,
            "context": dict,
            "fallback_level": str,
            "decisions": list,
            "user_dialog_id": (int, type(None)),
            "assistant_dialog_id": (int, type(None)),
        }

        # 6.2 重置 quota 跑正常路径
        reset_data = {"date": str(date.today()), "used": 0, "limit": 20, "history": []}
        Path(QUOTA_FILE).write_text(json.dumps(reset_data))

        # 6.3 正常路径
        result = advisor.chat("aileo", "信维通信 300136 估值")
        for field_name, expected_type in required_fields.items():
            if field_name not in result:
                errors.append(f"正常路径缺字段: {field_name}")
            elif not isinstance(result[field_name], expected_type):
                errors.append(f"正常路径 {field_name} 类型错: 期望 {expected_type}, 实际 {type(result[field_name])}")
        if not any(f"正常路径" in e for e in errors):
            print(f"  ✅ 正常路径 schema OK: level={result['fallback_level']}")

        # 6.4 L4 早退路径 (重置 quota=20)
        exhausted_data = {"date": str(date.today()), "used": 20, "limit": 20, "history": []}
        Path(QUOTA_FILE).write_text(json.dumps(exhausted_data))
        advisor2 = L3Advisor()
        result = advisor2.chat("aileo", "test")
        for field_name, expected_type in required_fields.items():
            if field_name not in result:
                errors.append(f"L4 早退路径缺字段: {field_name}")
            elif not isinstance(result[field_name], expected_type):
                errors.append(f"L4 早退路径 {field_name} 类型错: 期望 {expected_type}, 实际 {type(result[field_name])}")
        if not any(f"L4 早退" in e for e in errors):
            print(f"  ✅ L4 早退路径 schema OK: level={result['fallback_level']}")

        success = len(errors) == 0
        if success:
            print(f"  ✅ 模式 6 通过")
        else:
            print(f"  ❌ 模式 6 失败: {errors}")
        return success, errors
    except Exception as e:
        return False, [f"模式 6 异常: {e}"]
    finally:
        # 恢复 quota
        Path(QUOTA_FILE).write_text(json.dumps({
            "date": str(date.today()), "used": 0, "limit": 20, "history": []
        }))


# ============================================================
# 模式 7 (V23 R1 扩展): SkillRollback P2-4
# ============================================================
def pattern_7_skill_rollback() -> Tuple[bool, List[str]]:
    """验证 SkillBackupManager.list_backups(skill_name=None) + list_all_backups()"""
    errors = []
    print("\n=== [模式 7] SkillRollback P2-4 (V23 R1 扩展) ===")
    try:
        from skill_rollback import SkillBackupManager, SkillBackup

        mgr = SkillBackupManager()

        # 7.1 list_backups(None) 不传参数 (PIT #12 修复后)
        result = mgr.list_backups()
        if not isinstance(result, list):
            errors.append(f"list_backups() 应返回 list, 实际 {type(result)}")
        else:
            print(f"  ✅ list_backups() 无参: {len(result)} 个备份")

        # 7.2 list_backups(skill_name) 带参数
        result = mgr.list_backups("hermes-investpilot-coordination-v2")
        if not isinstance(result, list):
            errors.append(f"list_backups(name) 应返回 list, 实际 {type(result)}")
        else:
            print(f"  ✅ list_backups('hermes-investpilot-coordination-v2'): {len(result)} 个")

        # 7.3 list_all_backups() 新方法
        all_backups = mgr.list_all_backups()
        if not isinstance(all_backups, list):
            errors.append(f"list_all_backups() 应返回 list, 实际 {type(all_backups)}")
        else:
            print(f"  ✅ list_all_backups() 含元数据: {len(all_backups)} 个")
            if all_backups:
                sample = all_backups[0]
                required = {"path", "skill_name", "date", "size_bytes"}
                missing = required - set(sample.keys())
                if missing:
                    errors.append(f"list_all_backups 项缺字段: {missing}")
                else:
                    print(f"    - 样例: {sample['skill_name']} {sample['date']} {sample['size_bytes']}B")

        # 7.4 向后兼容 - get_latest_backup
        latest = mgr.get_latest_backup("hermes-investpilot-coordination-v2")
        if latest is None or not latest.exists():
            errors.append(f"get_latest_backup 返回无效: {latest}")
        else:
            print(f"  ✅ get_latest_backup: {latest.name}")

        # 7.5 端到端: 备份 → list → 还原
        sb = SkillBackup("hermes-investpilot-coordination-v2")
        backup_path = sb.backup()
        # 列表里应能找到
        found = any(b["path"].name == backup_path.name for b in mgr.list_all_backups())
        if not found:
            errors.append(f"新备份 {backup_path.name} 在 list_all_backups 中找不到")
        else:
            print(f"  ✅ 新备份 {backup_path.name[:30]}... 在列表里")
        # 还原
        if not sb.rollback(backup_path):
            errors.append("rollback 返回 False")
        else:
            print(f"  ✅ rollback 成功")

        success = len(errors) == 0
        if success:
            print(f"  ✅ 模式 7 通过")
        else:
            print(f"  ❌ 模式 7 失败: {errors}")
        return success, errors
    except Exception as e:
        return False, [f"模式 7 异常: {e}"]


# ============================================================
# 模式 8 (V23 R1 扩展): HermesBacktestValidator 方案 8
# ============================================================
def pattern_8_hermes_backtest() -> Tuple[bool, List[str]]:
    """验证 hermes_backtest_validator.validate_hermes_strategy()"""
    errors = []
    print("\n=== [模式 8] HermesBacktest 方案 8 (V23 R1 扩展) ===")
    try:
        # 8.1 Schema-First: 探测 backtest_engine 真实返回
        import json
        import os
        os.environ['DB_PASSWORD'] = json.loads(CREDS_FILE.read_text())["DB_PASSWORD"]
        sys.path.insert(0, str(SCRIPTS_DIR))
        from backtest_engine import backtest_strategy
        # 看真实返回结构
        result = backtest_strategy(
            ts_codes=["300059.XSHE"],
            start_date="2026-05-22",
            end_date="2026-06-12",
            initial_capital=1_000_000.0,
        )
        required_keys = {"total_return", "sharpe_ratio", "max_drawdown", "final_value", "equity_curve"}
        if not required_keys.issubset(result.keys()):
            errors.append(f"backtest_strategy 缺字段: {required_keys - set(result.keys())}")
        else:
            print(f"  ✅ backtest_strategy 返回: {len(result)} 字段齐全")
            # ⚠️ PIT #14: equity_curve 是 [float], 不是 [{value, date}]
            if result["equity_curve"] and isinstance(result["equity_curve"][0], dict):
                errors.append("PIT #14: equity_curve 期望 [float] 实际 [dict]")
            else:
                print(f"    equity_curve: {len(result['equity_curve'])} 元素 (类型 {type(result['equity_curve'][0]).__name__})")

        # 8.2 import hermes_backtest_validator
        sys.path.insert(0, str(HERMES_SCRIPTS_DIR))
        from hermes_backtest_validator import (
            validate_hermes_strategy, StrategyBacktestResult,
            _get_decision_points, _normalize_ts_code,
        )
        print(f"  ✅ import OK: 4 函数 + 1 dataclass")

        # 8.3 真实依赖探测: _normalize_ts_code
        test_cases = [
            ("300059", "300059.XSHE"),  # 深交所
            ("600487", "600487.XSHG"),  # 上交所
            ("518880", "518880.XSHG"),  # 5 开头 ETF
            ("300059.XSHE", "300059.XSHE"),  # 已有后缀
            ("159819", "159819.XSHE"),  # 1 开头
            ("512880", "512880.XSHG"),
        ]
        for raw, expected in test_cases:
            actual = _normalize_ts_code(raw)
            if actual != expected:
                errors.append(f"_normalize_ts_code({raw}) → {actual}, 期望 {expected}")
            else:
                print(f"  ✅ _normalize_ts_code({raw}) → {actual}")

        # 8.4 端到端: validate_hermes_strategy
        r = validate_hermes_strategy(user_id="aileo", days=30, start_date="2026-05-22", end_date="2026-06-12")
        if not isinstance(r, StrategyBacktestResult):
            errors.append(f"validate_hermes_strategy 返回类型错: {type(r)}")
        else:
            # schema 验证 (PIT #10 修复: decision_count 必有)
            required_fields = ["strategy_name", "stock_codes", "start_date", "end_date",
                               "return_pct", "alpha_pct", "sharpe", "max_drawdown",
                               "initial_capital", "final_value", "error", "decision_count"]
            for f in required_fields:
                if not hasattr(r, f):
                    errors.append(f"StrategyBacktestResult 缺字段: {f}")
            print(f"  ✅ 端到端 validate OK: return={r.return_pct}%, codes={len(r.stock_codes)}, decisions={r.decision_count}")
            if r.error:
                print(f"    ⚠️ error: {r.error}")
            else:
                print(f"    sharpe={r.sharpe} max_dd={r.max_drawdown}%")

        # 8.5 PG 写入验证
        import psycopg2
        conn = psycopg2.connect(**get_db_config())
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM l3.strategy_backtest_results WHERE user_id='aileo'")
        cnt = cur.fetchone()[0]
        if cnt == 0:
            errors.append("l3.strategy_backtest_results 没写入 aileo 的记录")
        else:
            print(f"  ✅ PG 写入: l3.strategy_backtest_results (aileo) = {cnt} 条")
        conn.close()

        success = len(errors) == 0
        if success:
            print(f"  ✅ 模式 8 通过")
        else:
            print(f"  ❌ 模式 8 失败: {errors}")
        return success, errors
    except Exception as e:
        return False, [f"模式 8 异常: {e}"]


# ============================================================
# 入口
# ============================================================
PATTERNS = {
    1: ("Schema-First 验证", pattern_1_schema_first),
    2: ("真实依赖探测", pattern_2_inspect_module),
    3: ("PG 事务健康检查", pattern_3_pg_transaction),
    4: ("Mock LLM 真实跑通", pattern_4_mock_llm),
    5: ("限额状态隔离", pattern_5_quota_isolation),
    6: ("早退路径 Schema 验证", pattern_6_api_schema),
    7: ("SkillRollback P2-4 (V23 R1)", pattern_7_skill_rollback),
    8: ("HermesBacktest 方案 8 (V23 R1)", pattern_8_hermes_backtest),
}


@dataclass
class TestReport:
    pattern: int
    name: str
    success: bool
    errors: List[str]
    duration_s: float


def main():
    parser = argparse.ArgumentParser(description="Hermes 6 大测试模式")
    parser.add_argument("--pattern", type=int, action="append", help="跑指定模式 (可多次)")
    parser.add_argument("--all", action="store_true", help="跑全部 6 模式")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args()

    import time

    # 选模式
    if args.all:
        selected = list(PATTERNS.keys())
    elif args.pattern:
        selected = args.pattern
    else:
        print("用法: --all 或 --pattern N (可多次)")
        print("模式列表:")
        for k, (name, _) in PATTERNS.items():
            print(f"  {k}: {name}")
        return

    # 跑
    reports: List[TestReport] = []
    for p in selected:
        if p not in PATTERNS:
            print(f"⚠️ 未知模式: {p}")
            continue
        name, fn = PATTERNS[p]
        start = time.time()
        success, errors = fn()
        duration = round(time.time() - start, 2)
        reports.append(TestReport(p, name, success, errors, duration))

    # 报告
    print("\n" + "=" * 60)
    print(f"📊 {len(PATTERNS)} 模式测试报告")
    print("=" * 60)
    passed = sum(1 for r in reports if r.success)
    total = len(reports)
    for r in reports:
        icon = "✅" if r.success else "❌"
        print(f"  {icon} [模式 {r.pattern}] {r.name} ({r.duration_s}s)")
        if not r.success:
            for err in r.errors:
                print(f"      - {err}")
    print(f"\n通过: {passed}/{total}")

    if args.json:
        out_file = INVEST_ROOT / "hermes_coordination" / "test-report.json"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps([dataclasses.asdict(r) for r in reports], indent=2, ensure_ascii=False))
        print(f"JSON 报告: {out_file}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
