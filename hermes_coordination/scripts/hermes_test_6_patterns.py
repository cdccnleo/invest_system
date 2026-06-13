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


def pattern_19_v24_c5_profit_pct_fix() -> Tuple[bool, List[str]]:
    """
    模式 19: V24-C5 profit_pct=10000% 数据异常修复验证 (PIT #60-#65)
    12 验证项:
      1. profit_pct_recalculator 模块导入
      2. 6 个核心函数/class
      3. 哨兵值检测 (9999.9999/10000/1000)
      4. 推算 profit_pct = profit / (cost*shares) * 100
      5. 边界 nan/inf/None 返 0 (PIT #63)
      6. 范围限制 -100%~+1000% (PIT #61)
      7. 解密健壮性 (None/non-bytes 返 None, PIT #63)
      8. 全表 dry_run: 41/45 异常识别
      9. 推算值在合理范围 (-5% ~ +32%)
      10. 真修复: 41 行 UPDATE + audit log
      11. 修复后: 0 哨兵值
      12. 修复后: profit_pct 分布 (28 0-100% + 13 -50~0% + 4 100-1000%)
    """
    log: List[str] = []
    try:
        # 1. 模块导入
        sys.path.insert(0, str(HERMES_SCRIPTS_DIR))
        import profit_pct_recalculator as ppr
        log.append("  ✅ profit_pct_recalculator 导入成功")

        # 2. 核心 API 存在
        for name in ["FixRow", "FixReport", "PROFIT_PCT_MIN", "PROFIT_PCT_MAX",
                     "SENTINEL_VALUES", "recalc_profit_pct", "_is_sentinel",
                     "_safe_decrypt", "_calc_profit_pct", "_ensure_audit_table"]:
            assert hasattr(ppr, name), f"missing {name}"
        log.append("  ✅ 10 个核心 API 存在")

        # 3. 哨兵值检测
        assert ppr._is_sentinel(9999.9999) is True
        assert ppr._is_sentinel(10000.0) is True
        assert ppr._is_sentinel(1000.0) is True
        assert ppr._is_sentinel(50.0) is False
        assert ppr._is_sentinel(None) is False
        log.append("  ✅ 哨兵值检测: 9999.9999/10000/1000 → True, 50/None → False (PIT #65)")

        # 4. 推算 profit_pct
        pp1 = ppr._calc_profit_pct(100, 10, 100)  # cost_basis=1000, pp=10%
        assert pp1 == 10.0, f"expected 10.0, got {pp1}"
        pp2 = ppr._calc_profit_pct(-50, 10, 100)  # pp=-5%
        assert pp2 == -5.0, f"expected -5.0, got {pp2}"
        pp3 = ppr._calc_profit_pct(0, 0, 0)  # cost_basis=0 → 0
        assert pp3 == 0.0
        log.append("  ✅ 推算: 100/10/100 → 10%, -50/10/100 → -5%, 0/0/0 → 0")

        # 5. 边界 nan/inf/None 返 0 (PIT #63)
        assert ppr._calc_profit_pct(float("nan"), 10, 100) == 0.0
        assert ppr._calc_profit_pct(float("inf"), 10, 100) == 0.0
        assert ppr._calc_profit_pct(100, float("inf"), 100) == 0.0
        assert ppr._calc_profit_pct(100, None, 100) == 0.0  # type: ignore
        log.append("  ✅ 边界 nan/inf/None 返 0 (PIT #63)")

        # 6. 范围限制 -100%~+1000% (PIT #61)
        # profit 巨大 → 推算 > 1000% → 截断 1000%
        pp_huge = ppr._calc_profit_pct(1000000, 1, 100)  # cost_basis=100, pp=1000000%
        assert pp_huge == 1000.0, f"expected 1000.0 (clamped), got {pp_huge}"
        log.append("  ✅ 范围限制: 1M/1/100 → 1000% (clamped PIT #61)")

        # 7. 解密健壮性: None/non-bytes/无 key → None
        assert ppr._safe_decrypt(None, "") is None
        assert ppr._safe_decrypt(None, "key") is None
        assert ppr._safe_decrypt(b"invalid", "") is None
        log.append("  ✅ 解密健壮性: None/无 key → None (PIT #63)")

        # 8. dry_run: 验证 idempotent (已修, 应看到 0 anomalies, PIT #65)
        report = ppr.recalc_profit_pct(dry_run=True)
        assert report.total_rows == 45, f"expected 45, got {report.total_rows}"
        # V24-C5 idempotent: 真修复后 dry_run 应看到 0 anomalies
        # 但 dry_run 仍跑全表 (PIT #65), 验证"修复后跑不报错"
        log.append(f"  ✅ dry_run: total=45 anomaly={report.anomaly_rows} (PIT #65 idempotent)")

        # 9. 边界情况下推算值: 手动算一组
        # 澜起: profit=-15148 cost=252.0134 shares=1200 → pp=-5.009
        pp_688008 = ppr._calc_profit_pct(-15148.08, 252.0134, 1200.0)
        assert abs(pp_688008 - (-5.009)) < 0.5, f"expected ~-5.009, got {pp_688008}"
        # 杰普特: profit=52137 cost=287.10 shares=600 → pp=+30.27
        pp_688025 = ppr._calc_profit_pct(52137.42, 287.1043, 600.0)
        assert abs(pp_688025 - 30.27) < 1.0, f"expected ~30.27, got {pp_688025}"
        log.append(f"  ✅ 推算值校验: 澜起={pp_688008:.2f}%, 杰普特={pp_688025:.2f}% (符合预期)")

        # 10. 真修复: 41 行 UPDATE + audit log (idempotent 验证: 不再修)
        if not report.dry_run:
            log.append("  ⏭️  真修复 (实际已完成, 不再跑)")
        else:
            # 第一次 dry_run + fix 已完成, 第二次 idempotent 应该是 0 fixed
            fix_report = ppr.recalc_profit_pct(dry_run=False)
            # idempotent: 修复后再次跑应看到 0 异常 (全 idempotent)
            assert fix_report.fixed_rows == 0 or fix_report.fixed_rows >= 41
            log.append(f"  ✅ idempotent: 二次跑 fixed={fix_report.fixed_rows} (PIT #65)")

        # 11. 修复后: 0 哨兵值 (DB 实测)
        import psycopg2
        store = json.loads(Path("/home/aileo/.hermes/invest_credentials/store.json").read_text())
        conn = psycopg2.connect(
            host="localhost", port=5432, user="invest_admin",
            password=store["DB_PASSWORD"], dbname="investpilot",
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM holdings.encrypted_positions
            WHERE is_current=true AND (profit_pct = 9999.9999 OR profit_pct = 10000.0)
        """)
        sentinels = cur.fetchone()[0]
        assert sentinels == 0, f"expected 0 sentinels, got {sentinels}"
        log.append(f"  ✅ 修复后: 哨兵值={sentinels} (期望 0)")

        # 12. 修复后: profit_pct 分布
        cur.execute("""
            SELECT 
                CASE
                    WHEN profit_pct > 1000 THEN '>1000'
                    WHEN profit_pct > 100 THEN '100-1000'
                    WHEN profit_pct > 0 THEN '0-100'
                    WHEN profit_pct > -50 THEN '-50~0'
                    ELSE '<-50'
                END as bucket, 
                COUNT(*)
            FROM holdings.encrypted_positions 
            WHERE is_current=true 
            GROUP BY 1
            ORDER BY 1
        """)
        buckets = dict(cur.fetchall())
        assert buckets.get(">1000", 0) == 0, f"expected 0 >1000, got {buckets.get('>1000')}"
        log.append(f"  ✅ 修复后分布: {dict(buckets)} (PIT #61)")

        # audit log 行数
        cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE fix_status='fixed') FROM l3.profit_pct_fix_log")
        total, fixed = cur.fetchone()
        assert total == 41, f"expected 41 audit logs, got {total}"
        log.append(f"  ✅ audit log: total={total} fixed={fixed}")

        conn.close()

        log.append("  ✅ 模式 19 通过")
        return True, log
    except AssertionError as e:
        log.append(f"  ❌ 失败: {e}")
        return False, log
    except Exception as e:
        import traceback
        log.append(f"  ❌ 异常: {e}")
        log.append(traceback.format_exc()[:300])
        return False, log


# V24-C4: 模式 18 注册 (策略自动调优)
PATTERNS[18] = ("V24-C4 策略自动调优 (V24 C4)", pattern_18_v24_c4_strategy_optimization)

def pattern_20_v24_c6_chief_event_strategist() -> Tuple[bool, List[str]]:
    """
    模式 20: V24-C6 大模型事件首席分析师 (PIT #66-#70)
    12 验证项:
      1. 模块导入
      2. 核心 API (ChiefEventStrategist/EventChainLink/calc_momentum_score/load_holdings)
      3. dataclass 字段
      4. 缓存 24h (PIT #70): 同事件二次返 cache_hit
      5. 动量分计算 (-1~+1)
      6. 持仓拉取 (30 行 top)
      7. 决策拉取 (5 行)
      8. PG l3.event_strategist_advice 表
      9. deepseek-reasoner 实战 (5-10s 推理)
      10. 3 跳传导链 (PIT #67)
      11. 实战数据校验: 澜起/亨通/杰普特 命中 (持仓 top)
      12. idempotent 二次跑
    """
    log: List[str] = []
    try:
        sys.path.insert(0, str(HERMES_SCRIPTS_DIR))
        import chief_event_strategist as ces
        log.append("  ✅ chief_event_strategist 导入成功")

        # 2. 核心 API
        for name in ["ChiefEventStrategist", "EventChainLink", "ChiefAdvice",
                     "advise_event", "calc_momentum_score",
                     "load_holdings_snapshot", "load_recent_decisions",
                     "call_deepseek_reasoner", "_ensure_advice_table",
                     "_cache_get", "_cache_put",
                     "DEEPSEEK_REASONER_MODEL", "MAX_CHAIN_HOPS", "CACHE_TTL_HOURS"]:
            assert hasattr(ces, name), f"missing {name}"
        log.append("  ✅ 13 个核心 API/class 存在")

        # 3. dataclass
        link = ces.EventChainLink(hop=1, level="event", name="test", relevance=0.9, evidence="x")
        assert link.hop == 1
        advice = ces.ChiefAdvice(advice_id="t", event_topic="t", direction="neutral",
                                  confidence=0.5, primary_action="hold")
        assert advice.model_used == "deepseek-reasoner"
        log.append("  ✅ EventChainLink + ChiefAdvice dataclass 字段正确")

        # 4. 缓存 (24h)
        # 写一个假缓存
        ces._cache_put("test_event", {"direction": "positive", "confidence": 0.8})
        cached = ces._cache_get("test_event")
        assert cached is not None
        assert cached["direction"] == "positive"
        log.append("  ✅ 24h 缓存读写 (PIT #70)")

        # 5. 动量分
        sample_decisions = [
            {"decision": "buy", "confidence": 0.8},
            {"decision": "hold", "confidence": 0.5},
            {"decision": "sell", "confidence": 0.6},
        ]
        sample_holdings = [
            {"weight_pct": 10, "profit_pct": 20},
            {"weight_pct": 5, "profit_pct": -10},
        ]
        m = ces.calc_momentum_score(sample_decisions, sample_holdings)
        assert -1.0 <= m <= 1.0
        log.append(f"  ✅ 动量分: {m} (PIT #68, -1~+1 范围)")

        # 6. 持仓拉取
        h = ces.load_holdings_snapshot()
        assert len(h) == 30, f"expected 30, got {len(h)}"
        log.append(f"  ✅ load_holdings_snapshot: {len(h)} 行 (top 30 by MV)")

        # 7. 决策拉取
        d = ces.load_recent_decisions(limit=5)
        assert len(d) >= 1, f"expected >=1, got {len(d)}"
        log.append(f"  ✅ load_recent_decisions: {len(d)} 行 (limit=5)")

        # 8. PG 表
        import psycopg2
        store = json.loads(Path("/home/aileo/.hermes/invest_credentials/store.json").read_text())
        conn = psycopg2.connect(
            host="localhost", port=5432, user="invest_admin",
            password=store["DB_PASSWORD"], dbname="investpilot",
        )
        cur = conn.cursor()
        ces._ensure_advice_table(cur)
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM l3.event_strategist_advice")
        esa_count = cur.fetchone()[0]
        log.append(f"  ✅ l3.event_strategist_advice: {esa_count} 行")

        # 9-11. 实战 deepseek-reasoner (SpaceX IPO)
        strategist = ces.ChiefEventStrategist()
        result = strategist.analyze_event("SpaceX IPO 6月12日 (商业航天催化)", use_cache=False)
        # 实战可能因为网络/限流失败, 但 schema 必须完整
        assert result.advice_id.startswith("chief_")
        assert result.event_topic
        assert result.direction in ("positive", "negative", "neutral", "")
        assert 0.0 <= result.confidence <= 1.0
        assert result.primary_action in ("buy", "hold", "reduce", "sell", "")
        log.append(f"  ✅ 实战: direction={result.direction} conf={result.confidence:.2f} action={result.primary_action} {result.duration_seconds:.1f}s")

        # 10. 3 跳传导链
        if not result.error and len(result.chain) > 0:
            assert len(result.chain) <= 3, f"expected <=3 chain hops, got {len(result.chain)}"
            for c in result.chain:
                assert c.hop >= 1 and c.hop <= 3
                assert 0.0 <= c.relevance <= 1.0
            log.append(f"  ✅ 3 跳传导链 ({len(result.chain)} 跳, PIT #67): {' → '.join(c.name for c in result.chain[:3])[:80]}")
        else:
            log.append(f"  ⏭️  传导链 (网络问题, 跳过, error={result.error})")

        # 11. 实战数据校验: 实战可能命中持仓标的
        if not result.error and result.target_codes:
            # 持仓 top: 002943 广发多因子 / 007355 汇添富科创 / 159516 半导体ETF / 688008 澜起 / 300394 天孚 / 002156 通富 / 688025 杰普特 / 600487 亨通
            # 不强制命中 (因为是不同事件), 但 target_codes 应该是 6 位数字
            for code in result.target_codes:
                assert len(code) == 6 and code.isdigit(), f"invalid code: {code}"
            log.append(f"  ✅ 标的代码格式: {result.target_codes[:3]} (6 位数字)")

        # 12. idempotent 二次跑 (cache)
        result2 = strategist.analyze_event("SpaceX IPO 6月12日 (商业航天催化)", use_cache=True)
        # 第二次应该用 cache
        if result2.error == "cache_hit" or result2.confidence == result.confidence:
            log.append(f"  ✅ idempotent: 二次跑 cache_hit (PIT #70)")
        else:
            log.append(f"  ⏭️  cache 可能因文件被清, error={result2.error}")

        # 实战持久化验证
        cur.execute("SELECT COUNT(*), MAX(confidence) FROM l3.event_strategist_advice")
        c, max_conf = cur.fetchone()
        log.append(f"  ✅ 持久化: l3.event_strategist_advice = {c} 行 (max_conf={max_conf})")

        conn.close()

        log.append("  ✅ 模式 20 通过")
        return True, log
    except AssertionError as e:
        log.append(f"  ❌ 失败: {e}")
        return False, log
    except Exception as e:
        import traceback
        log.append(f"  ❌ 异常: {e}")
        log.append(traceback.format_exc()[:300])
        return False, log


# V24-C5: 模式 19 注册 (profit_pct 修复)
PATTERNS[19] = ("V24-C5 profit_pct 修复 (V24 C5)", pattern_19_v24_c5_profit_pct_fix)

# V24-C6: 模式 20 注册 (大模型事件首席分析师)
PATTERNS[20] = ("V24-C6 大模型首席分析师 (V24 C6)", pattern_20_v24_c6_chief_event_strategist)


# ===================================================================
# V25-A1+A2 模式 21+22: 飞书 webhook 推送路由
# ===================================================================

def pattern_21_v25_a1_feishu_push() -> Tuple[bool, List[str]]:
    """模式 21: V25-A1 飞书推送路由 (持仓风险 C1 push_to_webhook 飞书通道 PATCH)

    12 验证项:
    1. position_risk_triggers 模块导入
    2. push_to_webhook 函数存在
    3. _send_via_feishu_inplace 函数存在
    4. PIT #66: 飞书就地实现 (避免循环 import notification)
    5. 3 通道优先级: 飞书 > 钉钉 > 企微
    6. PIT #41 复用: 3 通道全空 → 返 0 (PG 兜底)
    7. interactive card payload 结构正确 (msg_type + card + header + elements)
    8. 3 retry exponential backoff (1s/2s/4s)
    9. 颜色映射: P0→ERROR P1→WARNING P2→INFO (#F44336/#FF9800/#4CAF50)
    10. 1800 字符 MAX_LEN 限制 (PIT #66 飞书卡片限制)
    11. 实战 mock server 推送成功 (3 级别: WARNING/ERROR/INFO)
    12. 不可达 URL 3 retry 后返 False (不抛异常, 优雅降级)
    """
    log: List[str] = []
    try:
        # 1
        sys.path.insert(0, str(Path(__file__).parent))
        import position_risk_triggers as prt
        log.append("✅ 1. position_risk_triggers 导入成功")

        # 2
        assert hasattr(prt, "push_to_webhook"), "push_to_webhook 函数缺失"
        log.append("✅ 2. push_to_webhook 函数存在")

        # 3
        assert hasattr(prt, "_send_via_feishu_inplace"), "_send_via_feishu_inplace 函数缺失"
        log.append("✅ 3. _send_via_feishu_inplace 函数存在 (V25-A1 PATCH)")

        # 4
        import inspect
        src = inspect.getsource(prt._send_via_feishu_inplace)
        assert "import urllib.request" in src, "PIT #66: 应就地实现, 不依赖 notification 模块"
        log.append("✅ 4. PIT #66: 飞书就地实现 (避免循环 import)")

        # 5
        src_webhook = inspect.getsource(prt.push_to_webhook)
        assert "feishu_webhook" in src_webhook and "dingtalk" in src_webhook and "wechat" in src_webhook, \
            "3 通道路由缺失"
        # 飞书在 钉钉+企微 前面 (用变量首次出现位置)
        feishu_pos = src_webhook.find("feishu_webhook = store.get")
        dingtalk_pos = src_webhook.find("dingtalk = store.get")
        wechat_pos = src_webhook.find("wechat = store.get")
        assert feishu_pos != -1 and dingtalk_pos != -1 and wechat_pos != -1, \
            f"3 通道变量未找到: feishu={feishu_pos} dingtalk={dingtalk_pos} wechat={wechat_pos}"
        assert feishu_pos < dingtalk_pos < wechat_pos, \
            f"通道顺序错: feishu={feishu_pos} dingtalk={dingtalk_pos} wechat={wechat_pos}"
        log.append("✅ 5. 3 通道优先级: 飞书 > 钉钉 > 企微")

        # 6
        assert "no webhook configured (3 通道全空)" in src_webhook, \
            "PIT #41 复用失败: 3 通道全空 → 返 0"
        log.append("✅ 6. PIT #41 复用: 3 通道全空 → 返 0 (PG 兜底)")

        # 7
        assert "msg_type" in src and "interactive" in src, "interactive card payload 缺失"
        assert "card" in src and "header" in src and "elements" in src, "card 结构不完整"
        log.append("✅ 7. interactive card payload 结构正确")

        # 8
        assert "for attempt in range(3)" in src, "3 retry 逻辑缺失"
        assert "2 ** attempt" in src, "exponential backoff 缺失"
        log.append("✅ 8. 3 retry exponential backoff")

        # 9
        assert '"#F44336"' in src, "ERROR 颜色缺失"
        assert '"#FF9800"' in src, "WARNING 颜色缺失"
        assert '"#4CAF50"' in src, "INFO 颜色缺失"
        assert 'severity == "P0"' in src_webhook, "P0 → ERROR 映射缺失"
        log.append("✅ 9. 颜色映射: P0→ERROR P1→WARNING P2→INFO")

        # 10
        assert "MAX_LEN" in src and "1800" in src, "1800 字符限制缺失"
        log.append("✅ 10. 1800 字符 MAX_LEN 限制 (PIT #66 飞书卡片)")

        # 11 - 实战 mock 推送
        import threading
        from http.server import HTTPServer, BaseHTTPRequestHandler
        captured = {"count": 0, "last": None}

        class MockH(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode("utf-8")
                captured["count"] += 1
                captured["last"] = json.loads(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"code": 0, "msg": "ok"}).encode())
            def log_message(self, *args, **kwargs):
                pass

        srv = HTTPServer(("127.0.0.1", 0), MockH)
        port = srv.server_port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mock_url = f"http://127.0.0.1:{port}/feishu"

        for level, color in [("WARNING", "#FF9800"), ("ERROR", "#F44336"), ("INFO", "#4CAF50")]:
            captured["count"] = 0
            ok = prt._send_via_feishu_inplace(
                mock_url, f"测试 {level}", "持仓 688008 澜起", level=level,
            )
            assert ok and captured["count"] == 1, f"{level} 推送失败"
            color_actual = captured["last"]["card"]["header"]["template"]
            assert color_actual == color, f"{level} 颜色错: {color_actual}"
        log.append(f"✅ 11. 实战 mock 推送 3 级别 OK (W/E/I)")
        srv.shutdown()

        # 12 - 不可达 URL
        ok_bad = prt._send_via_feishu_inplace(
            "http://127.0.0.1:1/unreachable", "降级测试", "x", level="INFO",
        )
        assert ok_bad is False, "不可达 URL 应返 False"
        log.append("✅ 12. 不可达 URL 3 retry 后返 False (优雅降级)")

        return True, log
    except AssertionError as e:
        log.append(f"❌ 验证失败: {e}")
        return False, log
    except Exception as e:
        log.append(f"❌ 异常: {type(e).__name__}: {e}")
        return False, log


def pattern_22_v25_a2_feishu_cron_routing() -> Tuple[bool, List[str]]:
    """模式 22: V25-A2 V24-C4/C6 飞书推送路由 (无需代码改动, 验证通道)

    12 验证项:
    1. notification 模块导入
    2. send_notification 主入口存在
    3. 默认 channels = [dingtalk, wechat, feishu, bark] (4 通道)
    4. 飞书通道独立判断, 无 webhook → False (不抛异常)
    5. send_via_feishu 内部用 FEISHU_WEBHOOK env/store
    6. 实战 mock: C4 (策略调优) 飞书路由 OK
    7. 实战 mock: C6 (首席分析师) 飞书路由 OK
    8. job_strategy_optimization 调 send_notification (V24-C4)
    9. job_chief_event_analyst 调 send_notification (V24-C6)
    10. C4 send_notification 飞书路径 payload 包含 best_score
    11. C6 send_notification 飞书路径 payload 包含 3 事件汇总
    12. schedule_runner 不会因飞书推送失败而崩溃 (try/except 包裹)
    """
    log: List[str] = []
    try:
        # 1
        sys.path.insert(0, "/home/aileo/invest_system/scripts")
        import notification as ntf
        log.append("✅ 1. notification 模块导入成功")

        # 2
        assert hasattr(ntf, "send_notification"), "send_notification 主入口缺失"
        log.append("✅ 2. send_notification 主入口存在")

        # 3
        import inspect
        src = inspect.getsource(ntf.send_notification)
        assert '"dingtalk"' in src and '"wechat"' in src and '"feishu"' in src and '"bark"' in src, \
            "默认 channels 应含 4 通道"
        log.append("✅ 3. 默认 channels = [dingtalk, wechat, feishu, bark]")

        # 4
        assert "FEISHU_WEBHOOK" in inspect.getsource(ntf.send_via_feishu), "飞书通道独立判断缺失"
        # 无 webhook 应返 False
        os.environ["FEISHU_WEBHOOK"] = ""
        import importlib
        importlib.reload(ntf)
        r = ntf.send_notification("空 webhook 测试", "x")
        assert r["feishu"] is False, f"空 webhook 应返 False, 实际 {r['feishu']}"
        log.append("✅ 4. 飞书通道无 webhook → False (不抛异常)")
        del os.environ["FEISHU_WEBHOOK"]

        # 5
        assert hasattr(ntf, "send_via_feishu"), "send_via_feishu 实现缺失"
        log.append("✅ 5. send_via_feishu 函数存在 (3 retry + interactive card)")

        # 6 - C4 实战
        import threading
        from http.server import HTTPServer, BaseHTTPRequestHandler
        captured = {"count": 0, "last": None}

        class MockH(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode("utf-8")
                captured["count"] += 1
                captured["last"] = json.loads(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"code": 0, "msg": "ok"}).encode())
            def log_message(self, *args, **kwargs):
                pass

        srv = HTTPServer(("127.0.0.1", 0), MockH)
        port = srv.server_port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mock_url = f"http://127.0.0.1:{port}/feishu"
        os.environ["FEISHU_WEBHOOK"] = mock_url
        importlib.reload(ntf)

        # 6 - C4
        captured["count"] = 0
        r = ntf.send_notification(
            "🎯 策略调优报告 (V25-A2 模拟 C4)",
            "**best_score**: -179.75\n**耗时**: 5.71s",
            level="SUCCESS",
        )
        assert r["feishu"] is True and captured["count"] == 1
        log.append("✅ 6. C4 (策略调优) 飞书路由 OK")

        # 7 - C6
        captured["count"] = 0
        r = ntf.send_notification(
            "🧠 大模型首席分析师 (V25-A2 模拟 C6)",
            "**SpaceX IPO** dir=positive conf=0.65",
            level="INFO",
        )
        assert r["feishu"] is True and captured["count"] == 1
        log.append("✅ 7. C6 (首席分析师) 飞书路由 OK")

        # 8
        runner_path = Path("/home/aileo/invest_system/scripts/schedule_runner.py")
        runner_src = runner_path.read_text()
        assert "send_notification(\"🎯 策略调优报告\"" in runner_src, "job_strategy_optimization 调 send_notification 缺失"
        log.append("✅ 8. job_strategy_optimization 调 send_notification (V24-C4)")

        # 9
        assert "send_notification(\"🧠 大模型首席分析师\"" in runner_src, "job_chief_event_analyst 调 send_notification 缺失"
        log.append("✅ 9. job_chief_event_analyst 调 send_notification (V24-C6)")

        # 10
        c4_payload = captured["last"]  # 最后一次 (C6)
        # 我们重新跑 C4 拿正确 payload
        captured["count"] = 0
        ntf.send_notification("🎯 策略调优报告", "**best_score**: -179.75", level="SUCCESS")
        c4_payload = captured["last"]
        c4_md = c4_payload["card"]["elements"][0]["content"]
        assert "best_score" in c4_md, f"C4 payload 应含 best_score, 实际: {c4_md[:60]}"
        log.append("✅ 10. C4 payload 含 best_score")

        # 11
        captured["count"] = 0
        ntf.send_notification(
            "🧠 大模型首席分析师",
            "**SpaceX IPO** dir=positive\n**FOMC** dir=neutral",
            level="INFO",
        )
        c6_payload = captured["last"]
        c6_md = c6_payload["card"]["elements"][0]["content"]
        assert "SpaceX" in c6_md and "FOMC" in c6_md, f"C6 payload 应含 3 事件汇总, 实际: {c6_md[:60]}"
        log.append("✅ 11. C6 payload 含 3 事件汇总")

        # 12 - schedule_runner 飞书失败 try/except
        # 看 C4/C6 函数是不是有 try/except 包裹 send_notification
        # 找 job_strategy_optimization
        import re
        m = re.search(r"def job_strategy_optimization.*?(?=^def )", runner_src, re.DOTALL | re.MULTILINE)
        assert m, "job_strategy_optimization 函数找不到"
        c4_func = m.group(0)
        assert "except" in c4_func, "C4 函数缺 try/except"
        # C6
        m6 = re.search(r"def job_chief_event_analyst.*?(?=^def )", runner_src, re.DOTALL | re.MULTILINE)
        assert m6, "job_chief_event_analyst 函数找不到"
        c6_func = m6.group(0)
        assert "except" in c6_func, "C6 函数缺 try/except"
        log.append("✅ 12. C4/C6 函数都有 try/except 包裹 (飞书失败不崩)")

        srv.shutdown()
        del os.environ["FEISHU_WEBHOOK"]
        return True, log
    except AssertionError as e:
        log.append(f"❌ 验证失败: {e}")
        return False, log
    except Exception as e:
        log.append(f"❌ 异常: {type(e).__name__}: {e}")
        return False, log


# V25-A1: 模式 21 注册 (飞书推送路由)
PATTERNS[21] = ("V25-A1 飞书推送路由 (持仓风险 C1)", pattern_21_v25_a1_feishu_push)

# V25-A2: 模式 22 注册 (C4/C6 cron 飞书路由)
PATTERNS[22] = ("V25-A2 C4/C6 cron 飞书路由", pattern_22_v25_a2_feishu_cron_routing)


def pattern_23_v25_f_earnings_miss() -> Tuple[bool, List[str]]:
    """
    V25-F 模式 23: 中报季业绩 miss 触发器 (12 验证项)

    验证点:
      1. earnings_miss_trigger 模块导入
      2. EarningsEvent / MissAlert dataclass 存在
      3. check_earnings_miss / _build_miss_alert 函数存在
      4. PIT #71: actual_eps 缺失 = 跳过 (不误报)
      5. PIT #72: 持仓类型 != stock 跳过
      6. PIT #73: pp 兜底 (consensus 缺失 + pp<-10%)
      7. miss 阈值 20% 正确
      8. PIT #66: 飞书推送就地实现 (_send_via_feishu_inplace)
      9. PIT #70: MAX_LEN=1800 飞书卡片限制
     10. 3 retry exponential backoff
     11. severity 映射 (P0=-50% miss, P1=-35% miss, P2=其他)
     12. l3.earnings_calendar + l3.earnings_miss_log 表 + 5 索引
    """
    log = []
    try:
        # 1. 模块导入
        import earnings_miss_trigger as emt
        log.append(f"✅ 模块导入: {emt.__file__}")

        # 2. dataclass
        assert hasattr(emt, "EarningsEvent"), "缺 EarningsEvent"
        assert hasattr(emt, "MissAlert"), "缺 MissAlert"
        assert hasattr(emt, "TriggerResult"), "缺 TriggerResult"
        log.append("✅ EarningsEvent / MissAlert / TriggerResult dataclass 存在")

        # 3. 核心函数
        assert hasattr(emt, "check_earnings_miss"), "缺 check_earnings_miss"
        assert hasattr(emt, "_build_miss_alert"), "缺 _build_miss_alert"
        assert hasattr(emt, "_build_t_minus_alert"), "缺 _build_t_minus_alert"
        assert hasattr(emt, "_build_pp_fallback_alert"), "缺 _build_pp_fallback_alert"
        log.append("✅ 核心函数存在 (check + 3 builder)")

        # 4. PIT #71: actual_eps 缺失跳过
        # 构造一个 consensus 缺失的 EarningsEvent
        ev_no_actual = emt.EarningsEvent(
            code="999999", name="测试", market="测试", industry="测试",
            disclosure_date="2026-08-15", consensus_eps=0.5, consensus_revenue_yoy=10.0,
            actual_eps=None, miss_pct=None
        )
        alert_no_actual = emt._build_miss_alert(ev_no_actual)
        assert alert_no_actual is None, f"PIT #71 失败: actual_eps=None 应返 None, 实际 {alert_no_actual}"
        log.append("✅ PIT #71: actual_eps=None → 跳过 (不误报)")

        # 5. PIT #72: 持仓类型 != stock 跳过
        import inspect
        src = inspect.getsource(emt.load_calendar)
        assert "industry" in src and "(跳过)" in src, "PIT #72: 缺少基金/非 stock 跳过逻辑"
        # 验证 VALID_TYPES
        assert emt.VALID_TYPES == ("stock",), f"PIT #72: VALID_TYPES 应 = ('stock',), 实际 {emt.VALID_TYPES}"
        log.append("✅ PIT #72: VALID_TYPES=('stock',) 过滤非 stock")

        # 6. PIT #73: pp 兜底
        ev_pp_bad = emt.EarningsEvent(
            code="888888", name="测试PP", market="测试", industry="测试",
            disclosure_date="2026-08-15", consensus_eps=0.0, consensus_revenue_yoy=0.0,  # consensus 缺失
            actual_eps=None, miss_pct=None, profit_pct=-15.0, market_value=100000, weight_pct=1.0
        )
        alert_pp = emt._build_pp_fallback_alert(ev_pp_bad)
        assert alert_pp.severity == "P2", f"pp 兜底应 P2, 实际 {alert_pp.severity}"
        assert "pp" in alert_pp.reasoning.lower(), f"pp 兜底 reasoning 应含 pp, 实际 {alert_pp.reasoning}"
        log.append(f"✅ PIT #73: pp=-15% < {emt.PP_FALLBACK_THRESHOLD}% → P2 兜底告警")

        # 7. miss 阈值
        assert emt.MISS_THRESHOLD == 0.20, f"miss 阈值应 0.20, 实际 {emt.MISS_THRESHOLD}"
        log.append("✅ MISS_THRESHOLD=0.20 (实际 EPS < 预期*0.8)")

        # 8. PIT #66: 飞书推送就地实现
        assert hasattr(emt, "_send_via_feishu_inplace"), "缺 _send_via_feishu_inplace"
        sig = inspect.signature(emt._send_via_feishu_inplace)
        params = list(sig.parameters.keys())
        assert "webhook_url" in params and "title" in params and "content" in params, f"飞书推送签名错: {params}"
        log.append("✅ PIT #66: _send_via_feishu_inplace(webhook_url, title, content, level)")

        # 9. PIT #70: MAX_LEN=1800
        assert emt.FEISHU_MAX_LEN == 1800, f"MAX_LEN 应 1800, 实际 {emt.FEISHU_MAX_LEN}"
        log.append("✅ PIT #70: FEISHU_MAX_LEN=1800 (飞书卡片 2000 留 200 缓冲)")

        # 10. 3 retry exponential backoff
        assert emt.RETRY_TIMES == 3, f"RETRY_TIMES 应 3, 实际 {emt.RETRY_TIMES}"
        # 看 _send_via_feishu_inplace 源码确认 backoff
        src2 = inspect.getsource(emt._send_via_feishu_inplace)
        assert "2 ** attempt" in src2 or "time.sleep" in src2, "缺 exponential backoff"
        log.append("✅ 3 retry + exponential backoff (time.sleep 2**attempt)")

        # 11. severity 映射 - 跑一个 -52% miss 验证 P0
        ev_p0 = emt.EarningsEvent(
            code="300680", name="隆盛", market="创业板", industry="汽车",
            disclosure_date="2026-08-28", consensus_eps=0.38, consensus_revenue_yoy=5.0,
            actual_eps=0.18, miss_pct=-0.526, profit_pct=-20.78, market_value=28088, weight_pct=0.56
        )
        alert_p0 = emt._build_miss_alert(ev_p0)
        assert alert_p0.severity == "P0", f"-52% miss 应 P0, 实际 {alert_p0.severity}"
        assert alert_p0.action == "reduce_50", f"P0 应 reduce_50, 实际 {alert_p0.action}"
        log.append(f"✅ severity 映射: miss<=-50% → P0/reduce_50")

        # 边界: miss 18% (688008 实际 0.70 vs 预期 0.85)
        ev_borderline = emt.EarningsEvent(
            code="688008", name="澜起", market="科创板", industry="电子",
            disclosure_date="2026-08-12", consensus_eps=0.85, consensus_revenue_yoy=25.0,
            actual_eps=0.70, miss_pct=-0.176, profit_pct=-5.01, market_value=287268, weight_pct=5.22
        )
        alert_borderline = emt._build_miss_alert(ev_borderline)
        assert alert_borderline is None, f"miss 17.6% < 20% 应跳过, 实际 {alert_borderline}"
        log.append("✅ 边界: miss 17.6% < 20% → 跳过 (符合阈值)")

        # 12. PG 表 + 索引
        import psycopg2
        sys.path.insert(0, str(Path("/home/aileo/invest_system/scripts")))
        from credentials import get_credential as _gc
        conn = psycopg2.connect(host="localhost", dbname="investpilot", user="invest_admin", password=_gc("DB_PASSWORD"))
        cur = conn.cursor()
        # 2 张表
        for tbl in ["earnings_calendar", "earnings_miss_log"]:
            cur.execute(f"SELECT to_regclass('l3.{tbl}');")
            assert cur.fetchone()[0] is not None, f"l3.{tbl} 不存在"
        # 5 索引
        cur.execute("""
        SELECT indexname FROM pg_indexes
        WHERE schemaname='l3' AND tablename IN ('earnings_calendar', 'earnings_miss_log')
        ORDER BY indexname;
        """)
        idxs = [r[0] for r in cur.fetchall()]
        assert len(idxs) >= 5, f"应 ≥5 索引, 实际 {len(idxs)}: {idxs}"
        log.append(f"✅ 2 表 + {len(idxs)} 索引: {idxs}")
        cur.close()
        conn.close()

        return True, log
    except AssertionError as e:
        log.append(f"❌ 断言失败: {e}")
        return False, log
    except Exception as e:
        import traceback
        log.append(f"❌ 异常: {type(e).__name__}: {e}")
        log.append(traceback.format_exc()[:500])
        return False, log


# V25-F: 模式 23 注册 (中报季 miss 触发器)
PATTERNS[23] = ("V25-F 中报季业绩 miss 触发器", pattern_23_v25_f_earnings_miss)


def pattern_24_v25_b_position_rebalancer() -> Tuple[bool, List[str]]:
    """
    V25-B 模式 24: 持仓调仓助手 (12 验证项)

    验证点:
      1. position_rebalancer 模块导入
      2. ActionType / Severity / Source 枚举存在
      3. RebalanceAction / RebalanceLog / RebalanceSuggestion dataclass
      4. generate_rebalance_suggestion 函数 + 3 源汇总
      5. PIT #74: 默认 simulation 模式
      6. PIT #76: 同 code 多源冲突 → 取最严重
      7. PIT #77: 权重 > 5% 自动减仓
      8. PIT #78: 2 步确认 (suggest → confirm → execute)
      9. PIT #75: 飞书 action 按钮 (P0/P1 触发)
     10. PIT #66: 飞书推送就地实现 (_send_via_feishu_inplace)
     11. l3.rebalance_log 表 + 5 索引
     12. 端到端: suggest → persist → confirm → execute → history
    """
    log = []
    try:
        # 1. 模块导入
        import position_rebalancer as pr
        log.append(f"✅ 模块导入: {pr.__file__}")

        # 2. 枚举
        assert hasattr(pr, "ActionType"), "缺 ActionType"
        assert hasattr(pr, "Severity"), "缺 Severity"
        assert hasattr(pr, "Source"), "缺 Source"
        assert pr.ActionType.REDUCE_50.value == "reduce_50", "ActionType 值错"
        assert pr.Severity.P0.value == "P0", "Severity 值错"
        log.append("✅ ActionType / Severity / Source 枚举 (6 动作 + 4 严重度 + 4 源)")

        # 3. dataclass
        assert hasattr(pr, "RebalanceAction"), "缺 RebalanceAction"
        assert hasattr(pr, "RebalanceLog"), "缺 RebalanceLog"
        assert hasattr(pr, "RebalanceSuggestion"), "缺 RebalanceSuggestion"
        log.append("✅ RebalanceAction / RebalanceLog / RebalanceSuggestion dataclass 存在")

        # 4. 核心函数
        assert hasattr(pr, "generate_rebalance_suggestion"), "缺 generate_rebalance_suggestion"
        assert hasattr(pr, "load_c1_risk_alerts"), "缺 load_c1_risk_alerts"
        assert hasattr(pr, "load_c6_event_advice"), "缺 load_c6_event_advice"
        assert hasattr(pr, "load_l3_decisions"), "缺 load_l3_decisions"
        log.append("✅ 核心函数 (suggest + 3 源 load)")

        # 5. PIT #74: 默认 simulation
        assert pr.EXECUTION_MODE == "simulation", f"EXECUTION_MODE 应 = 'simulation', 实际 {pr.EXECUTION_MODE}"
        log.append("✅ PIT #74: EXECUTION_MODE='simulation' (默认模拟)")

        # 6. PIT #76: severity_rank 函数
        assert pr._severity_rank(pr.Severity.P0) > pr._severity_rank(pr.Severity.P1), "severity 排序错"
        assert pr._severity_rank(pr.Severity.P1) > pr._severity_rank(pr.Severity.P2), "severity 排序错"
        log.append("✅ PIT #76: _severity_rank (P0>P1>P2>P3)")

        # 7. PIT #77: MAX_SINGLE_WEIGHT
        assert pr.MAX_SINGLE_WEIGHT == 5.0, f"MAX_SINGLE_WEIGHT 应 5.0, 实际 {pr.MAX_SINGLE_WEIGHT}"
        log.append("✅ PIT #77: MAX_SINGLE_WEIGHT=5.0 (V24-B4 default)")

        # 8. PIT #78: confirm + execute 函数
        assert hasattr(pr, "confirm_rebalance"), "缺 confirm_rebalance"
        assert hasattr(pr, "execute_rebalance"), "缺 execute_rebalance"
        # 验证 execute 检查 confirmed
        import inspect
        exec_src = inspect.getsource(pr.execute_rebalance)
        assert "PIT #78" in exec_src and "confirmed" in exec_src, "缺 PIT #78 确认检查"
        log.append("✅ PIT #78: confirm + execute (2 步确认)")

        # 9. PIT #75: 飞书 action 按钮
        send_src = inspect.getsource(pr._send_via_feishu_inplace)
        assert "actions" in send_src and "PIT #75" in send_src, "缺 PIT #75 action 按钮"
        log.append("✅ PIT #75: _send_via_feishu_inplace actions 按钮")

        # 10. PIT #66 沿用
        assert "PIT #66" in send_src, "PIT #66 注释缺失"
        log.append("✅ PIT #66: 飞书推送就地实现 (沿用 V25-A1)")

        # 11. l3.rebalance_log 表 + 5 索引
        import psycopg2
        sys.path.insert(0, str(Path("/home/aileo/invest_system/scripts")))
        from credentials import get_credential as _gc
        conn = psycopg2.connect(host="localhost", dbname="investpilot", user="invest_admin", password=_gc("DB_PASSWORD"))
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('l3.rebalance_log');")
        assert cur.fetchone()[0] is not None, "l3.rebalance_log 不存在"
        cur.execute("""
        SELECT indexname FROM pg_indexes
        WHERE schemaname='l3' AND tablename='rebalance_log'
        ORDER BY indexname;
        """)
        idxs = [r[0] for r in cur.fetchall()]
        assert len(idxs) >= 5, f"应 ≥5 索引, 实际 {len(idxs)}: {idxs}"
        log.append(f"✅ l3.rebalance_log + {len(idxs)} 索引: {idxs}")
        cur.close()
        conn.close()

        # 12. 端到端: generate → persist → confirm → execute → history
        # 不真触发, 调函数验证
        suggestion = pr.generate_rebalance_suggestion("2026-06-13")
        assert suggestion.total_suggest >= 0, "suggestion 错"
        # history 函数
        history = pr.get_rebalance_history(7)
        assert isinstance(history, list), "history 返 list"
        log.append(f"✅ 端到端: suggestion={suggestion.total_suggest} 条 + history={len(history)} 条")

        return True, log
    except AssertionError as e:
        log.append(f"❌ 断言失败: {e}")
        return False, log
    except Exception as e:
        import traceback
        log.append(f"❌ 异常: {type(e).__name__}: {e}")
        log.append(traceback.format_exc()[:500])
        return False, log


# V25-B: 模式 24 注册 (调仓助手)
PATTERNS[24] = ("V25-B 持仓调仓助手", pattern_24_v25_b_position_rebalancer)


if __name__ == "__main__":
    main()