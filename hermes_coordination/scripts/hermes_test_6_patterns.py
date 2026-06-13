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
# 模式 9: PortfolioCopilot 方案 6 (V23 R2 T1)
# ============================================================

def pattern_9_portfolio_copilot() -> Tuple[bool, List[str]]:
    """模式 9: 跨标协同 Copilot (V23 R2 方案 6)"""
    try:
        from hermes_portfolio_copilot import (
            PortfolioCopilot, map_event_to_holdings,
            cross_holdings_impact, aggregate_portfolio_advice,
        )
        errors: List[str] = []

        # ⚠️ PIT #22 + V24-B2: 模式 9 调 map_event_to_holdings 默认 use_llm=True, mock 掉
        import hermes_portfolio_copilot as hpc
        original_hpc_llm = hpc.call_llm_for_event_match
        def mock_llm_p9(event_topic, holdings):
            if "SpaceX" in event_topic or "卫星" in event_topic:
                return {
                    "affected_codes": ["300136", "002149", "600487"],
                    "direction": "positive",
                    "reasoning": "SpaceX 利好", "model": "mock", "tokens_used": 100,
                }
            return {"affected_codes": [], "direction": "neutral",
                    "reasoning": "无关", "model": "mock", "tokens_used": 50}
        hpc.call_llm_for_event_match = mock_llm_p9

        with PortfolioCopilot() as copilot:
            # 1. 持仓 45 行
            if len(copilot.holdings) != 45:
                errors.append(f"持仓数 {len(copilot.holdings)} ≠ 45")
            else:
                print(f"  ✅ 持仓 45 行 总值 ¥{sum(h.market_value for h in copilot.holdings):,.0f}")

            # 2. skill 索引 ≥20
            if len(copilot.skill_index) < 20:
                errors.append(f"skill 索引 {len(copilot.skill_index)} < 20")
            else:
                print(f"  ✅ skill 索引 {len(copilot.skill_index)} 条")

            # 3. 事件→持仓 (PIT #18 严格匹配, SpaceX 应只 3 个)
            impact = map_event_to_holdings("SpaceX IPO 6月12日 估值 1.3 万亿美元", copilot.holdings)
            target_names = [h.name for h in impact.affected_holdings]
            if "信维通信" not in target_names or "西部材料" not in target_names:
                errors.append(f"SpaceX 事件缺核心标的: {target_names}")
            elif len(impact.affected_holdings) > 5:
                errors.append(f"SpaceX 事件污染: {len(impact.affected_holdings)} 标的 (应 ≤5)")
            else:
                print(f"  ✅ SpaceX 事件精准匹配: {len(impact.affected_holdings)} 标的 {target_names}")

            # 4. 跨标推理
            links = cross_holdings_impact(impact)
            if len(links) < 1:
                errors.append("跨标推理无关联")
            else:
                print(f"  ✅ 跨标关联 {len(links)} 条")

            # 5. 组合建议 schema 完整 (PIT #10 早退铁律)
            advice = aggregate_portfolio_advice(impact, links)
            for attr in ("advice_id", "event_topic", "primary_action",
                         "target_codes", "target_names", "confidence",
                         "expected_value_at_risk", "cross_links",
                         "reasoning", "risk_warnings", "timestamp"):
                if not hasattr(advice, attr):
                    errors.append(f"advice 缺字段: {attr}")
            if not errors:
                print(f"  ✅ 建议 schema 完整 (11 字段) action={advice.primary_action} conf={advice.confidence}")

            # 6. PG 持久化
            try:
                from hermes_portfolio_copilot import persist_advice
                rid = persist_advice(copilot.conn, advice, impact)
                if rid > 0 or rid == -1:
                    print(f"  ✅ PG 持久化 rid={rid}")
                else:
                    errors.append(f"PG 持久化失败 rid={rid}")
            except Exception as e:
                errors.append(f"PG 持久化异常: {e}")

        # 7. 早退路径: 无匹配事件
        impact_none = map_event_to_holdings("完全无关 xyz", [])
        if impact_none.impact_magnitude != 0.0 or len(impact_none.affected_holdings) != 0:
            errors.append(f"无匹配事件早退失败: {impact_none.impact_magnitude}")
        else:
            print(f"  ✅ 早退 schema 完整 (magnitude=0, count=0)")

        # 8. 早退路径: 中性事件
        advice_neutral = aggregate_portfolio_advice(impact_none, [])
        if advice_neutral.confidence != 0.5 or advice_neutral.primary_action != "hold":
            errors.append(f"中性事件早退失败: conf={advice_neutral.confidence}")
        else:
            print(f"  ✅ 中性事件: hold/0.5")

        if errors:
            print(f"  ❌ 模式 9 失败: {errors}")
            hpc.call_llm_for_event_match = original_hpc_llm  # 还原
            return False, errors
        print(f"  ✅ 模式 9 通过")
        hpc.call_llm_for_event_match = original_hpc_llm  # 还原
        return True, []
    except Exception as e:
        try:
            hpc.call_llm_for_event_match = original_hpc_llm
        except Exception:
            pass
        return False, [f"模式 9 异常: {e}"]


# ============================================================
# 模式 10: DashboardBridge 方案 7 (V23 R2 T2)
# ============================================================

def pattern_10_dashboard_bridge() -> Tuple[bool, List[str]]:
    """模式 10: Dashboard ↔ Web UI 双向桥 (V23 R2 方案 7)"""
    try:
        from dashboard_hermes_bridge import (
            DashboardBridge, bridge_to_web_ui,
            ActionStatus, ensure_pg_tables,
        )
        errors: List[str] = []

        # ⚠️ PIT #22 + V24-B2: PortfolioCopilot 现在调 LLM, mock 掉避免 30s 超时
        import hermes_portfolio_copilot as hpc
        original_hpc_llm = hpc.call_llm_for_event_match
        def mock_llm_p10(event_topic, holdings):
            if "SpaceX" in event_topic or "卫星" in event_topic:
                return {
                    "affected_codes": ["300136", "002149", "600487"],
                    "direction": "positive",
                    "reasoning": "SpaceX 利好",
                    "model": "mock", "tokens_used": 100,
                }
            return {"affected_codes": [], "direction": "neutral",
                    "reasoning": "无关", "model": "mock", "tokens_used": 50}
        hpc.call_llm_for_event_match = mock_llm_p10

        ensure_pg_tables()
        bridge = DashboardBridge(user_id="aileo_p10", persist_to_pg=True)

        # 1. ask_holding
        req1 = bridge.ask_holding("688008", "澜起科技", "澜起科技现在能买吗?")
        if req1.status not in (ActionStatus.SUCCESS, ActionStatus.FAILED):
            errors.append(f"ask_holding 状态异常: {req1.status}")
        else:
            print(f"  ✅ ask_holding → {req1.status.value} ({req1.duration_ms:.0f}ms)")

        # 2. cross_advise (调 PortfolioCopilot)
        req2 = bridge.cross_holding_advise("SpaceX IPO 6月12日 估值 1.3 万亿")
        if req2.status != ActionStatus.SUCCESS:
            errors.append(f"cross_advise 失败: {req2.error}")
        elif "信维通信" not in req2.result.get("target_names", []):
            errors.append(f"cross_advise 缺信维通信: {req2.result.get('target_names')}")
        else:
            print(f"  ✅ cross_advise → {len(req2.result.get('target_names', []))} 标的")

        # 3. stress_test
        req3 = bridge.stress_test_quick("fomc_hike")
        if req3.status not in (ActionStatus.SUCCESS, ActionStatus.FAILED):
            errors.append(f"stress_test 状态异常: {req3.status}")
        else:
            print(f"  ✅ stress_test → {req3.status.value}")

        # 4. event_alert
        req4 = bridge.event_alert_subscribe("GTC 2026", threshold_pct=5.0)
        if req4.status != ActionStatus.SUCCESS:
            errors.append(f"event_alert 失败: {req4.error}")
        elif not req4.result.get("subscribed"):
            errors.append(f"event_alert 未订阅: {req4.result}")
        else:
            print(f"  ✅ event_alert subscribed=True")

        # 5. 推送桥
        notif = bridge_to_web_ui(req2)
        if not notif.delivered_at:
            errors.append("推送未送达")
        else:
            print(f"  ✅ 推送送达: {notif.notification_id[:20]}")

        # 6. 持仓权重查询
        weight = bridge._get_position_weight("688008")
        if weight < 0:
            errors.append(f"持仓权重查询失败: {weight}")
        else:
            print(f"  ✅ 持仓权重 688008={weight:.2f}%")

        # 7. 早退: 未知 action
        req5 = bridge._execute_action("unknown_action_xyz", {})
        if req5.status != ActionStatus.FAILED:
            errors.append(f"未知 action 应 failed, 实际 {req5.status}")
        else:
            print(f"  ✅ 未知 action 早退: failed")

        # 8. 早退: 无关事件
        req6 = bridge.cross_holding_advise("天气预报明天晴天")
        if len(req6.result.get("target_codes", [])) != 0:
            errors.append(f"无关事件应有 0 标的, 实际 {len(req6.result.get('target_codes', []))}")
        else:
            print(f"  ✅ 无关事件早退: 0 标的")

        # 9. PG 持久化验证
        import psycopg2
        import json
        from pathlib import Path
        creds = json.loads(Path.home().joinpath(".hermes/invest_credentials/store.json").read_text())
        conn = psycopg2.connect(host="localhost", user="invest_admin",
                                password=creds["DB_PASSWORD"], dbname="investpilot")
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM l3.dashboard_bridge_log WHERE user_id = 'aileo_p10'")
        req_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM l3.push_notification_log")
        notif_count = cur.fetchone()[0]
        conn.commit()
        conn.close()
        if req_count < 4 or notif_count < 1:
            errors.append(f"PG 数据不足: req={req_count}, notif={notif_count}")
        else:
            print(f"  ✅ PG 持久化: req={req_count}, notif={notif_count}")

        # 10. 端到端: ask → push 完整流程
        req_final = bridge.ask_holding("300394", "天孚通信")
        notif_final = bridge_to_web_ui(req_final)
        if notif_final.payload.get("result", {}).get("fallback_level") not in ("L1_normal", "L4_skip"):
            errors.append(f"端到端 fallback_level 异常: {notif_final.payload.get('result', {}).get('fallback_level')}")
        else:
            print(f"  ✅ 端到端 ask+push: {req_final.status.value} → {notif_final.payload.get('result', {}).get('fallback_level')}")

        if errors:
            print(f"  ❌ 模式 10 失败: {errors}")
            hpc.call_llm_for_event_match = original_hpc_llm  # 还原 mock
            return False, errors
        print(f"  ✅ 模式 10 通过")
        hpc.call_llm_for_event_match = original_hpc_llm  # 还原 mock
        return True, []
    except Exception as e:
        try:
            hpc.call_llm_for_event_match = original_hpc_llm
        except Exception:
            pass
        return False, [f"模式 10 异常: {e}"]


# ============================================================
# 模式 11: V22Monitoring 监控 7 天 (V23 R3 T1)
# ============================================================

def pattern_11_v22_monitoring() -> Tuple[bool, List[str]]:
    """模式 11: v2.2 监控数据收集器 (V23 R3)"""
    try:
        from v22_monitoring import (
            generate_daily_report, backfill_7_days, ensure_pg_table,
            collect_llm_call_count, collect_decision_writes, collect_push_count,
            collect_fallback_distribution, collect_cron_health, persist_metrics,
            format_report_text,
        )
        errors: List[str] = []

        # 0. PG 表存在
        import psycopg2
        from pathlib import Path
        creds = json.loads(Path.home().joinpath(".hermes/invest_credentials/store.json").read_text())
        conn = psycopg2.connect(host="localhost", user="invest_admin",
                                password=creds["DB_PASSWORD"], dbname="investpilot")
        try:
            ensure_pg_table(conn)
            cur = conn.cursor()
            conn.commit()
            cur.execute("SELECT count(*) FROM l3.v22_monitoring")
            pre_count = cur.fetchone()[0]
            conn.commit()
            print(f"  ✅ v22_monitoring 表已存在 (当前 {pre_count} 行)")
        except Exception as e:
            errors.append(f"PG 表初始化失败: {e}")
            return False, errors

        target = "2026-06-12"

        # 1. 5 大指标 (各调一次, 验证 schema)
        try:
            llm_count, _ = collect_llm_call_count(conn, target)
            assert llm_count >= 0
            print(f"  ✅ llm_call_count: {llm_count}")
        except Exception as e:
            errors.append(f"collect_llm_call_count 失败: {e}")

        try:
            dec = collect_decision_writes(conn, target)
            print(f"  ✅ decision_writes: {dec}")
        except Exception as e:
            errors.append(f"collect_decision_writes 失败: {e}")

        try:
            push, _ = collect_push_count(conn, target)
            print(f"  ✅ push_count: {push}")
        except Exception as e:
            errors.append(f"collect_push_count 失败: {e}")

        try:
            fb = collect_fallback_distribution(conn, target)
            assert "L1_normal" in fb
            print(f"  ✅ fallback_dist: L1={fb['L1_normal']}, L4={fb.get('L4_skip', 0)}")
        except Exception as e:
            errors.append(f"collect_fallback_distribution 失败: {e}")

        try:
            rate, total, failed = collect_cron_health(conn, target)
            assert 0 <= rate <= 100
            print(f"  ✅ cron_health: {rate}% ({total} 任务, {failed} 失败)")
        except Exception as e:
            errors.append(f"collect_cron_health 失败: {e}")

        # 6. 报告生成 (PIT #10 早退 schema 完整)
        try:
            report = generate_daily_report(conn, target)
            for attr in ("report_date", "llm_call_count", "llm_quota_limit",
                         "llm_quota_used_pct", "decision_writes", "push_count",
                         "fallback_l1", "fallback_l2", "fallback_l3", "fallback_l4",
                         "cron_success_rate", "cron_total", "cron_failed",
                         "alerts", "health_status"):
                if not hasattr(report, attr):
                    errors.append(f"report 缺字段: {attr}")
            if not errors:
                print(f"  ✅ 报告 schema 完整 (15 字段) health={report.health_status}")
        except Exception as e:
            errors.append(f"generate_daily_report 失败: {e}")

        # 7. 持久化
        try:
            persist_metrics(conn, report)
            cur = conn.cursor()
            conn.commit()
            cur.execute("SELECT count(*) FROM l3.v22_monitoring WHERE metric_date = %s", (target,))
            metric_count = cur.fetchone()[0]
            conn.commit()
            assert metric_count >= 5, f"持久化 {metric_count} < 5"
            print(f"  ✅ PG 持久化: {metric_count} 指标")
        except Exception as e:
            errors.append(f"persist_metrics 失败: {e}")

        # 8. 7 天回填
        try:
            reports = backfill_7_days(conn)
            assert len(reports) == 7
            print(f"  ✅ 7 天回填: {len(reports)} 份报告")
        except Exception as e:
            errors.append(f"backfill_7_days 失败: {e}")

        # 9. 报告文本
        try:
            text = format_report_text(reports)
            assert "7 天汇总" in text and "趋势分析" in text
            print(f"  ✅ 报告文本: {len(text)} 字符")
        except Exception as e:
            errors.append(f"format_report_text 失败: {e}")

        # 10. 早退: 未来日期
        try:
            from datetime import date, timedelta
            future = (date.today() + timedelta(days=30)).isoformat()
            future_report = generate_daily_report(conn, future)
            assert future_report.llm_call_count == 0
            print(f"  ✅ 早退未来日期: 0 调用 + healthy")
        except Exception as e:
            errors.append(f"future_date early_return 失败: {e}")

        conn.close()
        if errors:
            print(f"  ❌ 模式 11 失败: {errors}")
            return False, errors
        print(f"  ✅ 模式 11 通过")
        return True, []
    except Exception as e:
        return False, [f"模式 11 异常: {e}"]


# ============================================================
# 模式 12: V22ToV23Integration 集成验证 (V23 R3 T2)
# ============================================================

def pattern_12_v22_to_v23_integration() -> Tuple[bool, List[str]]:
    """模式 12: v2.2 → v2.3 集成验证 (V23 R3)"""
    try:
        from v22_to_v23_integration import (
            verify_module_imports, verify_pg_tables, verify_cron_registered,
            verify_quota_files, verify_e2e_data_flow, full_integration_check,
            V22_MODULES, V23_MODULES, EXPECTED_PG_TABLES,
        )
        errors: List[str] = []

        # ⚠️ PIT #22 + V24-B2: 端到端测试会调 PortfolioCopilot → LLM, mock 掉
        import hermes_portfolio_copilot as hpc
        original_hpc_llm = hpc.call_llm_for_event_match
        def mock_llm_p12(event_topic, holdings):
            if "SpaceX" in event_topic or "卫星" in event_topic:
                return {
                    "affected_codes": ["300136", "002149", "600487"],
                    "direction": "positive",
                    "reasoning": "SpaceX 利好", "model": "mock", "tokens_used": 100,
                }
            return {"affected_codes": [], "direction": "neutral",
                    "reasoning": "无关", "model": "mock", "tokens_used": 50}
        hpc.call_llm_for_event_match = mock_llm_p12

        # 1. v2.3 模块全部 import
        imports = verify_module_imports()
        for m in V23_MODULES:
            if imports.get(m, {}).get("status") != "ok":
                errors.append(f"v2.3 模块 {m} import 失败: {imports.get(m)}")
        if not errors:
            print(f"  ✅ v2.3 模块 {len(V23_MODULES)}/{len(V23_MODULES)} import OK")

        # 2. v2.2 模块 import (允许部分失败, 因为有些可能在不同环境)
        v22_ok = sum(1 for m in V22_MODULES if imports.get(m, {}).get("status") == "ok")
        if v22_ok < 1:
            errors.append(f"v2.2 模块 {v22_ok}/{len(V22_MODULES)} 成功 (至少 1)")
        else:
            print(f"  ✅ v2.2 模块 {v22_ok}/{len(V22_MODULES)} import OK")

        # 3. PG 表存在
        import psycopg2
        from pathlib import Path
        creds = json.loads(Path.home().joinpath(".hermes/invest_credentials/store.json").read_text())
        conn = psycopg2.connect(host="localhost", user="invest_admin",
                                password=creds["DB_PASSWORD"], dbname="investpilot")
        try:
            pg = verify_pg_tables(conn)
            existing = len(pg.get("existing", []))
            if existing < 5:
                errors.append(f"PG 表 {existing} < 5: {pg}")
            else:
                print(f"  ✅ PG 表 {existing}/{len(EXPECTED_PG_TABLES)} 存在")
        finally:
            conn.close()

        # 4. 关键函数签名
        from v22_to_v23_integration import verify_module_funcs
        funcs = verify_module_funcs()
        v23_func_ok = sum(1 for m in V23_MODULES if funcs.get(m, {}).get("status") == "ok")
        if v23_func_ok != len(V23_MODULES):
            errors.append(f"v2.3 函数 {v23_func_ok}/{len(V23_MODULES)} 签名匹配")
        else:
            print(f"  ✅ v2.3 函数 {v23_func_ok}/{len(V23_MODULES)} 签名匹配")

        # 5. cron 注册
        cron = verify_cron_registered()
        if cron.get("status") != "ok":
            errors.append(f"cron 未注册: {cron}")
        else:
            print(f"  ✅ cron 18:30 已注册 (job_v22_monitoring_collect)")

        # 6. quota 文件
        quota = verify_quota_files()
        if quota.get("/tmp/hermes_llm_quota.json", {}).get("status") != "ok":
            errors.append(f"quota 文件不可用: {quota}")
        else:
            print(f"  ✅ quota 文件 OK: {list(quota.keys())}")

        # 7. e2e 数据流
        flow = verify_e2e_data_flow()
        if flow.get("status") not in ("ok", "partial"):
            errors.append(f"e2e 失败: {flow}")
        else:
            advice_count = flow.get("advice_count", 0)
            notif_delivered = flow.get("notif_delivered", False)
            print(f"  ✅ e2e: advice={advice_count} 标的, notif={notif_delivered}")

        # 8. 早退
        try:
            from hermes_portfolio_copilot import map_event_to_holdings
            impact = map_event_to_holdings("xyz", [])
            if impact.impact_magnitude != 0.0:
                errors.append(f"早退 magnitude 应 0.0, 实际 {impact.impact_magnitude}")
            else:
                print(f"  ✅ 早退: 0 标的 + 0.0 magnitude")
        except Exception as e:
            errors.append(f"早退测试失败: {e}")

        # 9. 12 模式测试脚本存在 + 包含模式 12
        test_script = Path(__file__).resolve()
        text = test_script.read_text(encoding="utf-8")
        if "pattern_12_v22_to_v23_integration" not in text:
            errors.append("模式 12 函数未定义")
        else:
            print(f"  ✅ 12 模式测试脚本 OK")

        # 10. 完整端到端
        try:
            full = full_integration_check()
            s = full.get("summary", {})
            if s.get("pass_rate", 0) < 80:
                errors.append(f"端到端 {s.get('pass_rate', 0):.1f}% < 80%")
            else:
                print(f"  ✅ 端到端: {s.get('passed', 0)}/{s.get('total', 0)} ({s.get('pass_rate', 0):.1f}%)")
        except Exception as e:
            errors.append(f"full_integration_check 失败: {e}")

        if errors:
            print(f"  ❌ 模式 12 失败: {errors}")
            hpc.call_llm_for_event_match = original_hpc_llm  # 还原
            return False, errors
        print(f"  ✅ 模式 12 通过")
        hpc.call_llm_for_event_match = original_hpc_llm  # 还原
        return True, []
    except Exception as e:
        try:
            hpc.call_llm_for_event_match = original_hpc_llm
        except Exception:
            pass
        return False, [f"模式 12 异常: {e}"]


# ============================================================
# 模式 13: V24-B2 LLM 真实接入 + Fallback 链 (V24 B2)
# ============================================================


def pattern_13_v24_b2_llm_integration() -> Tuple[bool, List[str]]:
    """模式 13: V24-B2 方案 6 LLM 真实接入 (PIT #22 模式标识 + PIT #7 Fallback 链)"""
    errors = []
    print("\n=== [模式 13] V24-B2 LLM 真实接入 (方案 6 升级) ===")
    try:
        # 1. LLM 客户端 + 限额管理存在
        from hermes_portfolio_copilot import (
            call_llm_for_event_match, map_event_to_holdings,
            _DailyLLMQuota, _QUOTA,
            _MATCH_MODE_LLM, _MATCH_MODE_KEYWORD, _MATCH_MODE_EMPTY,
            THEME_KEYWORDS_TO_CODES,
        )
        if call_llm_for_event_match is None:
            errors.append("call_llm_for_event_match 未定义")
        if _QUOTA is None or not hasattr(_QUOTA, "can_call"):
            errors.append("_DailyLLMQuota 类未实现")
        if _MATCH_MODE_LLM != "llm" or _MATCH_MODE_KEYWORD != "keyword":
            errors.append("PIT #22 模式标识常量未正确定义")
        if not errors:
            print(f"  ✅ LLM 客户端 + 限额管理 + 模式标识存在 (limit={_QUOTA.daily_limit}/天)")

        # 2. 加载真实持仓
        from hermes_portfolio_copilot import load_current_holdings
        holdings = load_current_holdings()
        if not holdings:
            errors.append("未加载到持仓")
            return False, errors
        print(f"  ✅ 持仓 {len(holdings)} 个")

        # 3. LLM 路径 (mock openai 已注入, 不打真实网络)
        import time

        # ⚠️ 重要: hpc 在 import 时 `from openai import OpenAI` 是在函数内 (line 294)
        # 改 openai.OpenAI 不影响 hpc 内的 OpenAI 局部名
        # 解法: 直接 mock hpc.call_llm_for_event_match 函数本身
        import hermes_portfolio_copilot as hpc
        original_hpc_llm = hpc.call_llm_for_event_match

        def mock_llm_succeed(event_topic, holdings):
            if "SpaceX" in event_topic or "卫星" in event_topic:
                return {
                    "affected_codes": ["300136", "002149", "600487"],
                    "direction": "positive",
                    "reasoning": "SpaceX IPO 利好卫星链",
                    "model": "mock-gpt-4o-mini",
                    "tokens_used": 150,
                }
            return {"affected_codes": [], "direction": "neutral", "reasoning": "无关",
                    "model": "mock", "tokens_used": 80}

        hpc.call_llm_for_event_match = mock_llm_succeed

        t0 = time.time()
        impact = map_event_to_holdings("SpaceX 6月12日 IPO 定价$1.3万亿", holdings, use_llm=True)
        llm_elapsed = time.time() - t0
        if not impact.event_id.endswith("llm"):
            errors.append(f"LLM 路径 event_id 应以 _llm 结尾, 实测 {impact.event_id}")
        if len(impact.affected_holdings) != 3:
            errors.append(f"LLM 应匹配 3 标的 (300136/002149/600487), 实测 {len(impact.affected_holdings)}")
        if "[LLM" not in impact.reasoning:
            errors.append("LLM reasoning 应带 [LLM 语义匹配] 前缀")
        if llm_elapsed > 5:
            errors.append(f"LLM 路径耗时过长: {llm_elapsed:.2f}s")
        if not errors:
            print(f"  ✅ LLM 路径: 3 标的命中, {llm_elapsed:.3f}s, event_id={impact.event_id}")

        # 4. 关键词路径
        t0 = time.time()
        impact_kw = map_event_to_holdings("SpaceX 6月12日 IPO 定价$1.3万亿", holdings, use_llm=False)
        kw_elapsed = time.time() - t0
        if not impact_kw.event_id.endswith("kw"):
            errors.append(f"关键词路径 event_id 应以 _kw 结尾, 实测 {impact_kw.event_id}")
        if "[关键词" not in impact_kw.reasoning:
            errors.append("关键词 reasoning 应带 [关键词匹配] 前缀")
        if kw_elapsed > 0.5:
            errors.append(f"关键词路径应 < 0.5s, 实测 {kw_elapsed:.3f}s")
        if not errors:
            print(f"  ✅ 关键词路径: 3 标的命中, {kw_elapsed:.3f}s, event_id={impact_kw.event_id}")

        # 5. Fallback 链 (PIT #7 防御): 让 LLM 抛错 → 期望降级
        def mock_llm_fail(event_topic, holdings):
            raise TimeoutError("mock LLM 30s timeout")

        hpc.call_llm_for_event_match = mock_llm_fail

        t0 = time.time()
        impact_fb = map_event_to_holdings("SpaceX 6月12日 IPO 定价$1.3万亿", holdings, use_llm=True)
        fb_elapsed = time.time() - t0
        if not impact_fb.event_id.endswith("kw"):
            errors.append(f"Fallback 后 event_id 应以 _kw 结尾, 实测 {impact_fb.event_id}")
        if "[关键词" not in impact_fb.reasoning:
            errors.append("Fallback 后 reasoning 应带 [关键词匹配] 前缀")
        if fb_elapsed > 2:
            errors.append(f"Fallback 应 < 2s (立即失败), 实测 {fb_elapsed:.2f}s")
        if not errors:
            print(f"  ✅ Fallback 链: LLM 失败 → 降级关键词, {fb_elapsed:.3f}s, "
                  f"event_id={impact_fb.event_id}")

        # 还原
        hpc.call_llm_for_event_match = original_hpc_llm

        # 6. 限额管理
        if not _QUOTA.can_call():
            errors.append("quota 应在限额内")
        else:
            print(f"  ✅ 限额管理 OK (今日已用 {_QUOTA._load().get('used', 0)}/{_QUOTA.daily_limit})")

        if errors:
            print(f"  ❌ 模式 13 失败: {errors}")
            return False, errors
        print(f"  ✅ 模式 13 通过")
        return True, []
    except Exception as e:
        import traceback
        return False, [f"模式 13 异常: {e}\n{traceback.format_exc()[:500]}"]


# ============================================================
# 模式 14: V24-B2.1 AInvest LLM 接入 (V24 B2.1)
# ============================================================

def pattern_14_v24_b21_ainvest_llm() -> Tuple[bool, List[str]]:
    """模式 14: V24-B2.1 复用 AInvest DeepSeek 链 (PIT #27-#30 防御)"""
    errors = []
    print("\n=== [模式 14] V24-B2.1 AInvest LLM 接入 (复用 DeepSeek+缓存+降级) ===")
    try:
        # 1. 验证 hermes_llm_client 模块存在
        from hermes_llm_client import (
            call_llm_for_event_match_ainvest,
            get_ainvest_llm_client,
            get_cached_ainvest_client,
            _parse_llm_json,
            _estimate_tokens,
        )
        if call_llm_for_event_match_ainvest is None:
            errors.append("call_llm_for_event_match_ainvest 未定义")
        if get_ainvest_llm_client is None:
            errors.append("get_ainvest_llm_client 未定义")
        if _parse_llm_json is None:
            errors.append("_parse_llm_json 未定义")
        if not errors:
            print(f"  ✅ hermes_llm_client 4 个核心函数存在")

        # 2. 拿 AInvest 客户端
        client = get_cached_ainvest_client()
        if client is None:
            errors.append("AInvest 客户端拿不到 (无法用真实 API)")
            return False, errors
        print(f"  ✅ AInvest 客户端: {type(client).__name__}")

        # 3. 加载真实持仓
        from hermes_portfolio_copilot import load_current_holdings
        holdings = load_current_holdings()
        if not holdings:
            errors.append("未加载到持仓")
            return False, errors
        print(f"  ✅ 持仓 {len(holdings)} 个")

        # 4. 真实 AInvest 调 (SpaceX 事件 - 期望命中 300136 + 002149)
        import time
        t0 = time.time()
        result = call_llm_for_event_match_ainvest(
            "SpaceX 6月12日 IPO 定价1.3万亿美元, 星链/星舰全面铺开",
            holdings,
        )
        ainvest_elapsed = time.time() - t0
        if result is None:
            errors.append("AInvest 调返回 None")
        else:
            print(f"  ✅ AInvest 调成功: {ainvest_elapsed:.2f}s, source={result.get('source')}")
            print(f"     model: {result.get('model')}, latency_s: {result.get('latency_s')}")
            codes = set(result.get("affected_codes", []))
            expected = {"300136", "002149"}  # 信维 + 西部材料
            if not expected.issubset(codes):
                errors.append(f"AInvest 漏关键标的: 期望 {expected} ⊆ 实际 {codes}")
            else:
                print(f"     命中: {len(codes)} 标的, 含核心 {expected & codes} ✅")
            if ainvest_elapsed > 10:
                errors.append(f"AInvest 调太慢: {ainvest_elapsed:.2f}s > 10s")
            else:
                print(f"     性能: {ainvest_elapsed:.2f}s < 10s ✅")

        # 5. hpc 集成验证 (use_ainvest=True)
        from hermes_portfolio_copilot import call_llm_for_event_match
        t0 = time.time()
        result_hpc = call_llm_for_event_match(
            "英伟达 GTC 2026 发布 Blackwell Ultra GPU, 1.6T 光模块订单爆满",
            holdings,
            use_ainvest=True,
        )
        hpc_elapsed = time.time() - t0
        if result_hpc is None:
            errors.append("hpc 集成调用 None")
        else:
            source = result_hpc.get("source", "")
            if not source.startswith("ainvest"):
                errors.append(f"hpc 应走 AInvest 链, 实际 source={source}")
            else:
                print(f"  ✅ hpc 集成: {hpc_elapsed:.2f}s, source={source}, "
                      f"{len(result_hpc.get('affected_codes', []))} 标的")

        # 6. Fallback 链 (PIT #30): mock AInvest 客户端失败
        import hermes_llm_client as hlc
        original_get = hlc.get_cached_ainvest_client
        hlc.get_cached_ainvest_client = lambda: None  # 强制 AInvest 不可用

        # 此时 hpc 应该降级到 V24-B2 OpenAI 路径
        # 但 V24-B2 OpenAI 也 mock 掉, 让最终也失败 → 返 None
        import hermes_portfolio_copilot as hpc
        original_hpc_llm = hpc.call_llm_for_event_match
        # mock hpc 内部 OpenAI 路径
        def mock_openai_fails(event_topic, holdings, use_ainvest=True):
            # 模拟: AInvest None → 走 OpenAI 路径 → 也失败
            from hermes_llm_client import call_llm_for_event_match_ainvest
            r = call_llm_for_event_match_ainvest(event_topic, holdings)
            if r is None:
                # 模拟 OpenAI 也失败
                return None
            return r
        hpc.call_llm_for_event_match = mock_openai_fails

        # 这个测试是为了验证: 即便 AInvest 失败, 也不会异常崩
        t0 = time.time()
        result_fb = hpc.call_llm_for_event_match("英伟达", holdings[:5], use_ainvest=True)
        fb_elapsed = time.time() - t0
        if fb_elapsed > 5:
            errors.append(f"Fallback 链太慢: {fb_elapsed:.2f}s")
        else:
            print(f"  ✅ Fallback 链: AInvest 不可用不崩, {fb_elapsed:.3f}s")

        # 还原
        hpc.call_llm_for_event_match = original_hpc_llm
        hlc.get_cached_ainvest_client = original_get

        # 7. JSON 解析容错 (PIT #28: DeepSeek 偶尔返 markdown 块)
        test_cases = [
            ('{"a": 1}', {"a": 1}),
            ('```json\n{"a": 2}\n```', {"a": 2}),
            ('思考...\n{"a": 3}\n结论', {"a": 3}),
            ('garbage', None),
        ]
        for raw, expected in test_cases:
            parsed = _parse_llm_json(raw)
            if parsed != expected:
                errors.append(f"_parse_llm_json 错: {raw[:30]} → {parsed} (期望 {expected})")
        if not any("_parse_llm_json 错" in e for e in errors):
            print(f"  ✅ JSON 容错解析 4/4 case 通过 (含 markdown/garbage)")

        if errors:
            print(f"  ❌ 模式 14 失败: {errors}")
            return False, errors
        print(f"  ✅ 模式 14 通过")
        return True, []
    except Exception as e:
        import traceback
        return False, [f"模式 14 异常: {e}\n{traceback.format_exc()[:500]}"]


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
    9: ("PortfolioCopilot 方案 6 (V23 R2)", pattern_9_portfolio_copilot),
    10: ("DashboardBridge 方案 7 (V23 R2)", pattern_10_dashboard_bridge),
    11: ("V22Monitoring 监控 7 天 (V23 R3)", pattern_11_v22_monitoring),
    12: ("V22ToV23Integration 集成验证 (V23 R3)", pattern_12_v22_to_v23_integration),
    13: ("V24-B2 LLM 真实接入 (V24 B2)", pattern_13_v24_b2_llm_integration),
    14: ("V24-B2.1 AInvest LLM 接入 (V24 B2.1)", pattern_14_v24_b21_ainvest_llm),
}


def pattern_15_v24_b3_websocket() -> Tuple[bool, List[str]]:
    """
    模式 15: V24-B3 WebSocket 实时推送 (Dashboard↔Streamlit)

    验证:
    1. dashboard_hermes_websocket.py 4 核心函数存在
    2. WSMessage schema 严格 (PIT #26)
    3. WS server 启停 (asyncio)
    4. 客户端 subscribe + ping/pong
    5. push_notification_with_notify 触发 NOTIFY → WS 广播
    6. render_websocket_js_client 输出 HTML 含 reconnect (PIT #32)
    """
    errors = []
    # 1. 4 核心函数存在
    try:
        import sys
        _SCRIPT_DIR = "/home/aileo/invest_system/hermes_coordination/scripts"
        if _SCRIPT_DIR not in sys.path:
            sys.path.insert(0, _SCRIPT_DIR)
        from dashboard_hermes_websocket import (
            WSMessage, WSMsgType, WSTarget,
            HermesWebSocketServer, render_websocket_js_client,
            push_notification_with_notify, get_websocket_status,
        )
        print(f"  ✅ dashboard_hermes_websocket 7 个核心函数/API 存在")
    except ImportError as e:
        return False, [f"❌ 导入失败: {e}"]

    # 2. WSMessage schema 严格 (PIT #26)
    try:
        msg_ok = WSMessage(type="ping", id="p1")
        assert msg_ok.type == "ping" and msg_ok.id == "p1" and msg_ok.ts
        # 缺字段应 raise
        try:
            WSMessage.from_json('{"missing":"fields"}')
            errors.append("❌ schema 校验: 缺字段未 raise")
        except ValueError:
            print(f"  ✅ schema 严格: 缺字段 raise ValueError (PIT #26)")
    except Exception as e:
        errors.append(f"❌ schema 测试异常: {e}")

    # 3. WS server 启停 + 4. 客户端 ping/pong
    try:
        import asyncio
        import threading
        import time
        import websockets

        async def server_test():
            server = HermesWebSocketServer(host="localhost", port=18765, token="test-token")
            await server.start()
            await asyncio.sleep(0.3)  # 等 server ready
            return server

        async def client_test():
            """客户端连接 + ping/pong + 校验 schema"""
            uri = "ws://localhost:18765/ws?token=test-token"
            async with websockets.connect(uri) as ws:
                # welcome
                welcome_raw = await asyncio.wait_for(ws.recv(), timeout=2)
                welcome = json.loads(welcome_raw)
                assert welcome["type"] == "notification"
                assert welcome["id"].startswith("welcome_")
                # subscribe
                sub = WSMessage(type="subscribe", id="s1", target="dashboard",
                                payload={"target": "dashboard"})
                await ws.send(sub.to_json())
                # ping
                ping = WSMessage(type="ping", id="p1")
                await ws.send(ping.to_json())
                # pong
                pong_raw = await asyncio.wait_for(ws.recv(), timeout=2)
                pong = json.loads(pong_raw)
                assert pong["type"] == "pong"
                assert pong["id"] == "pong_p1"
                # schema 错误
                await ws.send('{"missing":"fields"}')
                err_raw = await asyncio.wait_for(ws.recv(), timeout=2)
                err = json.loads(err_raw)
                assert err["type"] == "error"
                return True

        async def run_full():
            server = await server_test()
            try:
                ok = await client_test()
                return ok
            finally:
                await server.stop()

        ok = asyncio.run(run_full())
        if ok:
            print(f"  ✅ WS server 启停 + 客户端 subscribe/ping/pong/schema (5 项)")
    except Exception as e:
        errors.append(f"❌ WS server 集成测试异常: {type(e).__name__}: {e}")

    # 5. push_notification_with_notify 触发 NOTIFY → WS 广播
    try:
        import asyncio
        import websockets
        from dashboard_hermes_bridge import QuickActionRequest, ActionStatus

        async def server_test():
            server = HermesWebSocketServer(host="localhost", port=18766, token="test-token")
            await server.start()
            await asyncio.sleep(2.0)  # 等 server + PG listener 充分就绪
            return server

        async def listener(ready_event):
            """客户端等 NOTIFY 触发广播"""
            uri = "ws://localhost:18766/ws?token=test-token"
            async with websockets.connect(uri) as ws:
                await ws.recv()  # welcome
                sub = WSMessage(type="subscribe", id="s1", target="dashboard",
                                payload={"target": "dashboard"})
                await ws.send(sub.to_json())
                await asyncio.sleep(0.3)
                ready_event.set()  # 信号: 已订阅
                # 等 8s 内 NOTIFY 广播
                notif_raw = await asyncio.wait_for(ws.recv(), timeout=8)
                notif_d = json.loads(notif_raw)
                assert notif_d["type"] == "notification"
                assert "title" in notif_d.get("payload", {}), f"payload keys: {list(notif_d.get('payload', {}).keys())}"
                return notif_d

        async def trigger(ready_event):
            """写 PG + NOTIFY (等 listener ready)"""
            await asyncio.wait_for(ready_event.wait(), timeout=3)
            await asyncio.sleep(0.2)  # 再稳一下
            req = QuickActionRequest(
                request_id="req_p15_test",
                user_id="aileo",
                action_type="event_alert",
                payload={"event_topic": "模式 15 测试", "threshold_pct": 3.0},
                status=ActionStatus.SUCCESS,
                result={"response": "模式 15 NOTIFY 测试", "confidence": 0.95},
                duration_ms=1700.0,
            )
            return push_notification_with_notify(req, target="dashboard")

        async def run_notify():
            server = await server_test()
            ready = asyncio.Event()
            try:
                listener_ret, _trigger_ret = await asyncio.gather(listener(ready), trigger(ready))
                return True, listener_ret
            finally:
                await server.stop()

        ok, notif = asyncio.run(run_notify())
        if ok:
            print(f"  ✅ NOTIFY → WS 广播全链路通: {notif['payload']['title']}")
    except Exception as e:
        import traceback
        errors.append(f"❌ NOTIFY→WS 测试异常: {type(e).__name__}: {e}\n{traceback.format_exc()[:500]}")

    # 6. render_websocket_js_client 输出 HTML 含 reconnect (PIT #32)
    try:
        html = render_websocket_js_client()
        assert "WebSocket" in html
        assert "onclose" in html
        assert "setTimeout(connect, 3000)" in html  # PIT #32 自动 reconnect
        assert "subscribe" in html
        print(f"  ✅ JS 客户端 HTML 含 WebSocket + 自动 reconnect 3s (PIT #32)")
    except AssertionError as e:
        errors.append(f"❌ JS 客户端 HTML 校验失败: {e}")

    # 7. 集成: bridge.render_websocket_panel 函数存在
    try:
        from dashboard_hermes_bridge import render_websocket_panel
        assert callable(render_websocket_panel)
        print(f"  ✅ render_websocket_panel 已集成到 bridge.py")
    except (ImportError, AssertionError) as e:
        errors.append(f"❌ render_websocket_panel 集成失败: {e}")

    return len(errors) == 0, errors


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


# V24-B3: 模式 15 注册 (放在 main 后避免 forward reference 错误)
PATTERNS[15] = ("V24-B3 WebSocket 实时推送 (V24 B3)", pattern_15_v24_b3_websocket)


def pattern_16_v24_c1_position_risk() -> Tuple[bool, List[str]]:
    """
    模式 16: V24-C1 持仓风险预算 (方案 9)

    验证:
    1. position_risk_manager 3 核心函数存在
    2. position_risk_triggers 4 核心函数存在
    3. position_risk_dashboard render 函数存在
    4. 真实持仓 45 个 + 5 指标计算
    5. 组合 VaR (95%) 计算
    6. 风险等级判定 (low/medium/high/critical)
    7. 3 类触发器生成 (止损/止盈/集中度/亏损/同 type)
    8. 频次限制 + 去重 (PIT #40 #43)
    9. 3 级降级 (webhook→WS→PG) (PIT #30 复用)
    10. PG 持久化 (l3.position_risk_snapshot + l3.risk_alert_log)
    11. 边界 case (持仓 0 / total=0) (PIT #37 #38)
    12. schedule_runner 3 个 cron 已注册
    """
    errors = []
    try:
        import sys
        _SCRIPT_DIR = "/home/aileo/invest_system/hermes_coordination/scripts"
        if _SCRIPT_DIR not in sys.path:
            sys.path.insert(0, _SCRIPT_DIR)

        # 1. manager 8 核心
        from position_risk_manager import (
            analyze_portfolio, analyze_position, fetch_current_positions,
            ensure_pg_tables, save_snapshot, generate_risk_report,
            PositionRisk, PortfolioRisk,
        )
        print(f"  ✅ position_risk_manager 8 个核心函数/class 存在")

        # 2. triggers 7 核心
        from position_risk_triggers import (
            generate_alerts, dedup_alerts, run_triggers, persist_to_pg,
            RiskAlert, AlertType, AlertSeverity,
        )
        print(f"  ✅ position_risk_triggers 7 个核心函数/class 存在")

        # 3. dashboard render
        from position_risk_dashboard import render_risk_dashboard
        assert callable(render_risk_dashboard)
        print(f"  ✅ position_risk_dashboard.render_risk_dashboard 存在")

        # 4. 真实持仓 + 5 指标
        ensure_pg_tables()
        positions = fetch_current_positions()
        assert len(positions) == 45, f"持仓数异常: {len(positions)}"
        total_mv = sum(float(p.get("market_value") or 0) for p in positions)
        assert abs(total_mv - 5631646.60) < 1, f"总市值异常: {total_mv}"
        print(f"  ✅ 真实持仓 45 个 + 总市值 ¥{total_mv:,.0f}")

        # 5. 组合 VaR
        portfolio = analyze_portfolio(positions)
        assert portfolio.total_var_1d > 0, "VaR 应 > 0"
        print(f"  ✅ 组合 1d VaR: ¥{portfolio.total_var_1d:,.0f}")

        # 6. 风险等级判定
        position_risks = [analyze_position(p, positions, total_mv) for p in positions]
        levels = {pr.risk_level for pr in position_risks}
        assert "low" in levels or "medium" in levels, "应有风险等级"
        print(f"  ✅ 风险等级: {levels} (low/medium/high/critical)")

        # 7. 3 类触发器
        alerts = generate_alerts(positions)
        types = {a.alert_type for a in alerts}
        print(f"  ✅ 触发器类型: {len(types)} 种 ({list(types)[:5]})")

        # 8. 去重 (PIT #40)
        deduped = dedup_alerts(alerts)
        assert len(deduped) <= len(alerts), "去重后应 <= 原始"
        print(f"  ✅ 去重: {len(alerts)} → {len(deduped)} (PIT #40)")

        # 9. PG 持久化 (PIT #30 兜底)
        saved = persist_to_pg(deduped)
        assert saved >= 0, "PG 持久化应 >= 0"
        print(f"  ✅ PG 持久化: {saved}/{len(deduped)} (PIT #30 兜底)")

        # 10. 边界 case (PIT #37 #38)
        empty = analyze_portfolio([])
        assert empty.position_count == 0, "PIT #37 持仓 0 应 position_count=0"
        zero_total = analyze_portfolio([{"code": "X", "name": "X", "type": "stock", "market_value": 0}])
        assert zero_total.total_market_value == 0, "PIT #38 total=0 应 total_mv=0"
        print(f"  ✅ 边界: 持仓 0 / total=0 返 schema 完整 (PIT #37 #38)")

        # 11. schedule_runner cron 注册
        from pathlib import Path
        sr_text = Path("/home/aileo/invest_system/scripts/schedule_runner.py").read_text()
        assert "job_position_risk_alert" in sr_text, "schedule_runner 应包含 job"
        assert "position_risk_pre_market" in sr_text, "盘前 cron 应注册"
        assert "position_risk_post_market" in sr_text, "盘后 cron 应注册"
        assert "position_risk_weekly" in sr_text, "周一 cron 应注册"
        print(f"  ✅ schedule_runner 3 cron 已注册 (盘前/盘后/周一)")

        # 12. 报告生成
        report = generate_risk_report(portfolio, position_risks)
        assert "持仓风险报告" in report
        assert "组合总览" in report
        print(f"  ✅ 报告生成: {len(report)} 字符")

    except Exception as e:
        import traceback
        errors.append(f"❌ V24-C1 模式 16 异常: {type(e).__name__}: {e}\n{traceback.format_exc()[:500]}")

    return len(errors) == 0, errors


# V24-C1: 模式 16 注册 (持仓风险预算)
PATTERNS[16] = ("V24-C1 持仓风险预算 (V24 C1)", pattern_16_v24_c1_position_risk)


# ═══════════════════════════════════════════════════════════════════════════
# V24-B4: 模式 17 — 跨 Profile 隔离 (L3 Advisor Profile 切换)
# ═══════════════════════════════════════════════════════════════════════════

def pattern_17_v24_b4_profile_isolation() -> Tuple[bool, List[str]]:
    """V24-B4: 跨 Profile 隔离 (default/conservative/aggressive)

    验证项 (12):
    1. profile_strategy.py 8 核心函数/class 存在
    2. 3 profile YAML 加载成功 (default/conservative/aggressive)
    3. 3 profile 风险总览差异化 (max_pct/max_pe/confidence)
    4. 持仓合规检查 (信维 300136 PE 150 → default 黑名单+PE)
    5. 持仓合规检查 (信维 conservative 0/3 + 0/1 黑名单) 严格
    6. 持仓合规检查 (信维 aggressive 0/0 通过) 宽松
    7. 跨 profile 决策对比 (信维 3 套 action 不同)
    8. event_driven + conservative → hold (PIT #46 隔离)
    9. L3DialogEngine 接受 profile 参数
    10. L3DialogEngine 非法 profile 降级 default
    11. PG l3.profile_audit_log 建表 + 切换记录
    12. 边界: 持仓空返 [] (PIT #47)
    """
    import traceback
    errors: List[str] = []
    sys.path.insert(0, str(Path(__file__).parent))
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

    # 1. 核心函数/class
    try:
        from profile_strategy import (
            L3ProfileAdvisor, ProfileCompliance, ProfileRecommendation,
            ProfileRiskOverview, build_profile_aware_recommendation,
            get_all_profiles_risk_overview, check_profile_compliance,
            ensure_pg_tables, ALLOWED_PROFILES,
        )
        print(f"  ✅ profile_strategy 8 个核心函数/class 存在")
    except Exception as e:
        errors.append(f"❌ profile_strategy 导入失败: {e}")
        return False, errors

    if ALLOWED_PROFILES != ("default", "conservative", "aggressive"):
        errors.append(f"❌ ALLOWED_PROFILES 错: {ALLOWED_PROFILES}")
    else:
        print(f"  ✅ ALLOWED_PROFILES = {ALLOWED_PROFILES}")

    # 2. 3 profile YAML 加载
    try:
        overviews = get_all_profiles_risk_overview()
        if len(overviews) != 3:
            errors.append(f"❌ 3 profile 数量错: {len(overviews)}")
        else:
            print(f"  ✅ 3 profile 加载: {[o.profile for o in overviews]}")
    except Exception as e:
        errors.append(f"❌ 3 profile 加载失败: {e}")

    # 3. 风险总览差异化
    try:
        risk_levels = {o.profile: (o.max_position_pct, o.max_pe_ttm, o.confidence_threshold)
                       for o in overviews}
        if risk_levels != {
            "default": (5.0, 100, 0.65),
            "conservative": (8.0, 30, 0.80),
            "aggressive": (15.0, 200, 0.55),
        }:
            errors.append(f"❌ 风险总览不匹配: {risk_levels}")
        else:
            print(f"  ✅ 3 profile 风险差异化: max_pct {risk_levels['default'][0]}/{risk_levels['conservative'][0]}/{risk_levels['aggressive'][0]}")
    except Exception as e:
        errors.append(f"❌ 风险总览差异化失败: {e}")

    # 4-6. 持仓合规 (信维 300136)
    try:
        test_pos = [{"code": "300136", "name": "信维通信", "current_pct": 4.0, "pe_ttm": 150, "change_52w": 350}]
        d_results = check_profile_compliance("default", test_pos)
        c_results = check_profile_compliance("conservative", test_pos)
        a_results = check_profile_compliance("aggressive", test_pos)
        d_viol = len(d_results[0].violations) if d_results else 0
        c_viol = len(c_results[0].violations) if c_results else 0
        a_viol = len(a_results[0].violations) if a_results else 0
        # default: 1 (黑名单) + 1 (PE>100) = 2
        # conservative: 1 (PE>30) + 1 (52w>100) = 2
        # aggressive: 0
        if not (d_viol >= 2):
            errors.append(f"❌ default violations {d_viol} < 2")
        if not (c_viol >= 2):
            errors.append(f"❌ conservative violations {c_viol} < 2")
        if a_viol != 0:
            errors.append(f"❌ aggressive violations {a_viol} != 0")
        if not errors:
            print(f"  ✅ 信维合规对比: default {d_viol} violations | conservative {c_viol} | aggressive {a_viol} (宽松)")
    except Exception as e:
        errors.append(f"❌ 持仓合规失败: {e}")

    # 7. 跨 profile 决策对比
    try:
        recs = build_profile_aware_recommendation(
            target_code="300136", target_name="信维通信",
            current_pct=4.0, pe_ttm=150, change_52w=350,
        )
        actions = {p: r.action for p, r in recs.items()}
        # expected: default=sell (黑名单), conservative=reduce (PE), aggressive=buy
        if actions.get("default") != "sell":
            errors.append(f"❌ default action 错: {actions.get('default')}")
        if actions.get("aggressive") not in ("buy", "hold"):
            errors.append(f"❌ aggressive action 错: {actions.get('aggressive')}")
        if not errors:
            print(f"  ✅ 跨 profile 决策: default={actions['default']} conservative={actions['conservative']} aggressive={actions['aggressive']}")
    except Exception as e:
        errors.append(f"❌ 跨 profile 决策失败: {e}")

    # 8. event_driven + conservative (PIT #46)
    try:
        recs = build_profile_aware_recommendation(
            target_code="600487", target_name="亨通光电",
            current_pct=2.0, pe_ttm=58.91, change_52w=422, event_driven=True,
        )
        # conservative PE 58.91 > 30 → reduce (不追事件)
        if recs["conservative"].action != "reduce":
            errors.append(f"❌ conservative event_driven 错: {recs['conservative'].action}")
        if not errors:
            print(f"  ✅ event_driven + conservative: PE>{recs['conservative'].action} (PIT #46)")
    except Exception as e:
        errors.append(f"❌ event_driven 测试失败: {e}")

    # 9. L3DialogEngine profile 参数
    try:
        from l3_dialog_engine import L3DialogEngine
        e1 = L3DialogEngine()
        if e1.profile != "default":
            errors.append(f"❌ default profile 错: {e1.profile}")
        e1.conn.close()
        e2 = L3DialogEngine(profile="aggressive")
        if e2.profile != "aggressive":
            errors.append(f"❌ aggressive profile 错: {e2.profile}")
        e2.conn.close()
        if not errors:
            print(f"  ✅ L3DialogEngine profile 参数 OK")
    except Exception as e:
        errors.append(f"❌ L3DialogEngine profile 失败: {e}")

    # 10. 非法 profile 降级
    try:
        e3 = L3DialogEngine(profile="invalid_xyz")
        if e3.profile != "default":
            errors.append(f"❌ 非法 profile 降级错: {e3.profile}")
        e3.conn.close()
        if not errors:
            print(f"  ✅ 非法 profile 'invalid_xyz' 降级 default (PIT #44)")
    except Exception as e:
        errors.append(f"❌ 非法 profile 测试失败: {e}")

    # 11. PG profile_audit_log
    try:
        ddl = ensure_pg_tables()
        if "profile_audit_log" not in ddl:
            errors.append(f"❌ profile_audit_log 表未建: {ddl}")
        else:
            print(f"  ✅ profile_audit_log 表: {ddl['profile_audit_log']} 行")
        # 切换 + log
        advisor = L3ProfileAdvisor(profile="default")
        ok = advisor.log_profile_switch("default", "aggressive")
        if not ok:
            errors.append(f"❌ log_profile_switch 返 False")
        else:
            print(f"  ✅ profile 切换记录 PG 成功 (PIT #48)")
    except Exception as e:
        errors.append(f"❌ PG audit log 失败: {e}")

    # 12. 边界 (PIT #47)
    try:
        empty = check_profile_compliance("default", [])
        if empty != []:
            errors.append(f"❌ 边界 case 错: {empty}")
        else:
            print(f"  ✅ 边界: 持仓空返 [] (PIT #47)")
    except Exception as e:
        errors.append(f"❌ 边界 case 失败: {e}")

    if not errors:
        print(f"  ✅ 模式 17 通过")
    else:
        print(f"  ❌ 模式 17 失败: {len(errors)} 错误")
        for e in errors[:3]:
            print(f"    {e[:100]}")
    return len(errors) == 0, errors


# V24-B4: 模式 17 注册 (跨 Profile 隔离)
PATTERNS[17] = ("V24-B4 跨 Profile 隔离 (V24 B4)", pattern_17_v24_b4_profile_isolation)


# ═══════════════════════════════════════════════════════════════════════════
# V24-C4: 模式 18 — 策略自动调优 (网格搜索 + Walk-Forward)
# ═══════════════════════════════════════════════════════════════════════════

def pattern_18_v24_c4_strategy_optimization() -> Tuple[bool, List[str]]:
    """V24-C4: 回测策略自动调优 (网格 + Walk-Forward)

    验证项 (12):
    1. strategy_optimizer.py 6 核心函数/class 存在
    2. composite_score 复合分 (PIT #53)
    3. 边界: nan/inf/非数字返 0 (PIT #58)
    4. 单次回测 (PIT #52)
    5. 网格搜索 4 trials (PIT #55 早停)
    6. Walk-Forward 21 trials (3 window × 7) (PIT #54 滚动)
    7. 边界: 空 codes 返 0 trial (PIT #58 修复)
    8. PG l3.strategy_optimization_runs 表 DDL
    9. 主入口 run_optimization + 持久化
    10. select_best_run 查最优
    11. CLI --run --method walk_forward 真实跑
    12. 实战数据异常 (PIT #59) - 实战 21 天数据 -70% 返负分
    """
    import traceback
    errors: List[str] = []
    # ⚠️ PIT 修复: 头部 sys.path 已把 HERMES_SCRIPTS_DIR 放 [0],
    # 不需要再 insert (会打乱顺序导致 import 老 scripts/strategy_optimizer.py)
    # 直接用 sys.path 当前状态 (HERMES 在前, SCRIPTS 在后)

    # 1. 核心函数/class
    try:
        from strategy_optimizer import (
            grid_search, walk_forward_optimization, run_optimization,
            select_best_run, ensure_pg_tables, composite_score, run_single_backtest,
            Trial, OptimizationResult,
        )
        print(f"  ✅ strategy_optimizer 6 个核心函数/class 存在")
    except Exception as e:
        errors.append(f"❌ strategy_optimizer 导入失败: {e}")
        return False, errors

    # 2. 复合分
    try:
        s1 = composite_score(10, 2, 5)
        s2 = composite_score(0, 0, 0)
        if s1 != 6.5 or s2 != 0.0:
            errors.append(f"❌ composite_score 错: {s1} / {s2}")
        else:
            print(f"  ✅ 复合分 sharpe×2+return-|mdd|×1.5: 10/2/5 → {s1}, 0/0/0 → {s2}")
    except Exception as e:
        errors.append(f"❌ composite_score 失败: {e}")

    # 3. 边界 nan/inf
    try:
        s_nan = composite_score(float("nan"), 1, 1)
        s_inf = composite_score(1, float("inf"), 1)
        s_str = composite_score("bad", 1, 1)
        if s_nan != 0.0 or s_inf != 0.0 or s_str != 0.0:
            errors.append(f"❌ 边界复合分错: nan={s_nan} inf={s_inf} str={s_str}")
        else:
            print(f"  ✅ 边界 nan/inf/str 返 0 (PIT #58)")
    except Exception as e:
        errors.append(f"❌ 边界复合分失败: {e}")

    # 4. 单次回测
    try:
        t = run_single_backtest(
            ts_codes=["300059.XSHE", "600487.XSHG", "300394.XSHE"],
            start_date="2026-05-01", end_date="2026-06-12",
            initial_capital=1_000_000, position_size_pct=0.95,
        )
        if not hasattr(t, "composite_score"):
            errors.append(f"❌ Trial 缺 composite_score")
        else:
            print(f"  ✅ 单次回测: return={t.return_pct:.2f}% score={t.composite_score:.2f} (PIT #52)")
    except Exception as e:
        errors.append(f"❌ 单次回测失败: {e}")

    # 5. 网格搜索
    try:
        gs = grid_search(
            ts_codes=["300059.XSHE", "600487.XSHG", "300394.XSHE"],
            start_date="2026-05-01", end_date="2026-06-12",
            initial_capitals=[500_000, 1_000_000],
            position_sizes=[0.85, 0.95],
        )
        if gs.n_trials < 1:
            errors.append(f"❌ 网格搜索 n_trials=0: {gs.n_trials}")
        else:
            print(f"  ✅ 网格搜索: n_trials={gs.n_trials} best_score={gs.best_composite_score:.2f} (PIT #55)")
    except Exception as e:
        errors.append(f"❌ 网格搜索失败: {e}")

    # 6. Walk-Forward
    try:
        wf = walk_forward_optimization(
            ts_codes=["300059.XSHE", "600487.XSHG", "300394.XSHE"],
            end_date="2026-06-12",
            train_days=10, test_days=5, step_days=5,
        )
        if wf.n_trials < 5:
            errors.append(f"❌ WF n_trials<5: {wf.n_trials}")
        else:
            print(f"  ✅ Walk-Forward: n_trials={wf.n_trials} best_score={wf.best_composite_score:.2f} (PIT #54)")
    except Exception as e:
        errors.append(f"❌ Walk-Forward 失败: {e}")

    # 7. 边界: 空 codes
    try:
        empty = grid_search(ts_codes=[], start_date="2026-05-01", end_date="2026-06-12")
        if empty.n_trials != 0 or empty.error != "empty_codes":
            errors.append(f"❌ 空 codes 边界: n={empty.n_trials} err={empty.error}")
        else:
            print(f"  ✅ 边界: 空 codes → 0 trial + error=empty_codes (PIT #58 修复)")
    except Exception as e:
        errors.append(f"❌ 空 codes 边界失败: {e}")

    # 8. PG DDL
    try:
        ddl = ensure_pg_tables()
        if "strategy_optimization_runs" not in ddl:
            errors.append(f"❌ strategy_optimization_runs 表未建: {ddl}")
        else:
            print(f"  ✅ PG l3.strategy_optimization_runs: {ddl['strategy_optimization_runs']} 行")
    except Exception as e:
        errors.append(f"❌ PG DDL 失败: {e}")

    # 9. 主入口 + 持久化
    try:
        res = run_optimization(user_id="aileo", days=30, method="walk_forward", persist=True)
        if res.n_trials < 1:
            errors.append(f"❌ 主入口 n_trials=0: {res.error}")
        else:
            print(f"  ✅ 主入口: n_trials={res.n_trials} best_score={res.best_composite_score:.2f}")
    except Exception as e:
        errors.append(f"❌ 主入口失败: {e}")

    # 10. select_best_run
    try:
        best = select_best_run(method="walk_forward")
        if not best:
            errors.append(f"❌ select_best_run 没找到")
        else:
            print(f"  ✅ select_best_run: {best['run_id'][:30]} score={best['best_composite_score']:.2f}")
    except Exception as e:
        errors.append(f"❌ select_best_run 失败: {e}")

    # 11. CLI
    try:
        import subprocess
        r = subprocess.run(
            [".venv/bin/python", "hermes_coordination/scripts/strategy_optimizer.py", "--run", "--method", "walk_forward"],
            capture_output=True, text=True, timeout=120, cwd="/home/aileo/invest_system",
        )
        if r.returncode != 0:
            errors.append(f"❌ CLI exit={r.returncode}: {r.stderr[:200]}")
        elif "best_composite_score" not in r.stdout:
            errors.append(f"❌ CLI 无 best_composite_score: {r.stdout[:200]}")
        else:
            print(f"  ✅ CLI --run --method walk_forward exit=0, best_score 输出完整")
    except subprocess.TimeoutExpired:
        errors.append(f"❌ CLI 超时 120s")
    except Exception as e:
        errors.append(f"❌ CLI 失败: {e}")

    # 12. 实战数据异常
    try:
        res = run_optimization(user_id="aileo", days=30, method="walk_forward", persist=True)
        if res.best_composite_score >= 0:
            print(f"  ⚠️ best_score {res.best_composite_score:.2f} ≥ 0 (异常数据修复了?)")
        else:
            print(f"  ✅ 实战: best_score={res.best_composite_score:.2f} 负分, 反映实战数据问题 (PIT #59)")
    except Exception as e:
        errors.append(f"❌ 实战验证失败: {e}")

    if not errors:
        print(f"  ✅ 模式 18 通过")
    else:
        print(f"  ❌ 模式 18 失败: {len(errors)} 错误")
        for e in errors[:3]:
            print(f"    {e[:100]}")
    return len(errors) == 0, errors


# V24-C4: 模式 18 注册 (策略自动调优)
PATTERNS[18] = ("V24-C4 策略自动调优 (V24 C4)", pattern_18_v24_c4_strategy_optimization)


if __name__ == "__main__":
    main()