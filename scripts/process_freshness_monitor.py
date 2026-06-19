"""
process_freshness_monitor.py — 长生命周期进程新鲜度监控 (PIT #145, 6/19 P0-T2)

跨项目铁律 (PIT #144+#145 实战链, 6/19 V2.8.5):
- Python 进程不 reload .py 文件 (PIT #144 实战: schedule_runner PID 31736 加载老 storage_factory)
- 模块级 API 变更必须 commit 后立即 kill 重启长生命周期进程 (db-pool 铁律 ⑩)
- **PIT #145 升级**: 主动监控进程启动时间 vs 关键模块 mtime, 不一致 → CRITICAL 飞书告警
  不强制 kill (由人工决策), 但确保不会"忘重启 19h"再次发生 (PIT #144 实战教训)

设计 (PIT #141+#142 模式扩展, 6 步监控架构模板复用):
1. API: process_freshness_check() 返回 dict (PID / etime / status / stale_modules / level)
2. cron: schedule_runner job_process_freshness 每 10min 调一次 (跟 pool_health 错开避免同秒)
3. 阈值:
   🔴 CRITICAL: 任意关键模块 mtime > 进程启动时间 + 60s grace (1min) = stale
   🟡 WARNING:  进程启动时间 + 1-10min 区间 = transitional (刚启动 cron 未触发)
   🟢 HEALTHY:  进程启动时间 > 所有关键模块 mtime + 60s grace
4. 冷却: 同级别告警 cooldown 30min (跟 pool_health 一致)
5. 落库: audit.process_freshness_log (新建表, 类比 pool_health_metrics)
6. 飞书推: 仅 CRITICAL 推飞书, WARNING 写日志

检测的进程 (按 PIT #144 实战教训):
- schedule_runner (主进程, PIT #144 主角)
- streamlit (PIT #125 实战: streamlit 不热重载, dashboard 模块 mtime 变了不会生效)
- watchdog (持锁 + 自动启新, 但本身也要新鲜)

跨项目适用: 任何长生命周期 Python 进程 (Django / FastAPI / Celery Worker)
通用模式: 监控 PID etime + 关键模块 mtime + git HEAD commit time
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─── 告警级别 + 阈值常量 ────────────────────────────────────────────────

class FreshnessLevel:
    """新鲜度级别常量 (跟 PoolMonitor.AlertLevel 对齐)"""
    HEALTHY = "fresh"        # 静默 (HEALTHY 状态)
    WARNING = "stale_warn"   # 写日志 (刚启动未跑过 cron)
    CRITICAL = "stale_critical"  # 推飞书 (模块变更后未重启)


# 阈值常量
GRACE_SECONDS = 60            # 进程启动时间 + 60s 内不算 stale (cron 触发窗口)
WARNING_MAX_AGE_SECONDS = 600  # 进程启动后 10min 内 = WARNING (刚启动)


# ─── 监控的进程清单 (PIT #144 实战教训) ─────────────────────────────────
# 每个进程: (进程 cmd 关键字, 关键模块列表)
# 关键模块 mtime > 进程启动时间 → stale = 需要 kill 重启

MONITORED_PROCESSES = [
    {
        "name": "schedule_runner",
        "cmd_keyword": "schedule_runner.py",
        "critical_modules": [
            "scripts/storage_factory.py",   # PIT #143 加 get_db_conn 触发 15:40 ImportError
            "scripts/pool_manager.py",      # PIT #140 抽单例
            "scripts/pool_monitor.py",      # PIT #141 cron
            "scripts/schedule_runner.py",   # 自身 (cron 注册改 APScheduler)
        ],
    },
    {
        "name": "streamlit",
        "cmd_keyword": "streamlit",
        "critical_modules": [
            "scripts/dashboard_views/_shared.py",  # PIT #125 实战: 不热重载
        ],
    },
    {
        "name": "watchdog",
        "cmd_keyword": "watchdog_daemon.py",
        "critical_modules": [
            "scripts/watchdog_daemon.py",  # 自身
        ],
    },
]


# ─── 数据类 ─────────────────────────────────────────────────────────────

@dataclass
class ProcessFreshness:
    """单个进程的新鲜度报告"""
    name: str
    pid: Optional[int]
    etime_seconds: Optional[int]
    process_start_time: Optional[float]
    status: str  # "running" | "not_found"
    stale_modules: list = field(default_factory=list)  # [(module_path, mtime)]
    fresh_modules: list = field(default_factory=list)
    level: str = FreshnessLevel.HEALTHY  # healthy / warning / critical
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FreshnessReport:
    """整体新鲜度报告 (单个进程的)"""
    timestamp: str
    process_name: str
    level: str  # healthy / warning / critical (取所有进程中最高级)
    processes: list = field(default_factory=list)  # list of ProcessFreshness dict
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ─── 核心 API ───────────────────────────────────────────────────────────

def _get_process_etime(cmd_keyword: str) -> Optional[tuple]:
    """
    通过 ps 找第一个 cmd 包含 cmd_keyword 的 PID + etime (秒)
    返回 (pid, etime_seconds, start_time_epoch) 或 None
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,etimes,cmd"],
            capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        logger.error(f"[process_freshness] ps 异常: {e}")
        return None

    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            etimes = int(parts[1])
        except ValueError:
            continue
        cmdline = parts[2]
        if cmd_keyword in cmdline:
            start_time = time.time() - etimes
            return (pid, etimes, start_time)

    return None


def _check_module_freshness(module_path: str, process_start_time: float, grace_seconds: int = GRACE_SECONDS) -> tuple:
    """
    检查单个模块的 mtime vs 进程启动时间
    返回 (module_path, mtime, is_stale)
    is_stale = mtime > (process_start_time + grace_seconds) 即模块变更后未重启
    """
    full_path = Path("/home/aileo/invest_system") / module_path
    if not full_path.exists():
        return (module_path, None, True)  # 模块不存在 = stale

    mtime = full_path.stat().st_mtime
    cutoff = process_start_time + grace_seconds
    is_stale = mtime > cutoff
    return (module_path, mtime, is_stale)


def check_process_freshness(process_config: dict) -> ProcessFreshness:
    """
    检查单个进程的新鲜度

    Args:
        process_config: MONITORED_PROCESSES 中的单个 dict
            {
                "name": "schedule_runner",
                "cmd_keyword": "schedule_runner.py",
                "critical_modules": ["scripts/storage_factory.py", ...]
            }

    Returns:
        ProcessFreshness dataclass
    """
    name = process_config["name"]
    cmd_keyword = process_config["cmd_keyword"]
    critical_modules = process_config["critical_modules"]

    # 1. 找进程
    proc_info = _get_process_etime(cmd_keyword)
    if proc_info is None:
        return ProcessFreshness(
            name=name,
            pid=None,
            etime_seconds=None,
            process_start_time=None,
            status="not_found",
            level=FreshnessLevel.WARNING,
            reason=f"进程 {name} 未运行 (cmd_keyword={cmd_keyword})"
        )

    pid, etime_seconds, process_start_time = proc_info

    # 2. 检查每个关键模块
    stale_modules = []
    fresh_modules = []
    for mod in critical_modules:
        mod_path, mtime, is_stale = _check_module_freshness(mod, process_start_time)
        if is_stale:
            stale_modules.append((mod_path, mtime))
        else:
            fresh_modules.append((mod_path, mtime))

    # 3. 评估 level
    if stale_modules:
        # 至少一个关键模块 mtime > 进程启动时间 + grace = 🔴 CRITICAL
        level = FreshnessLevel.CRITICAL
        reason = f"进程 {name} (PID={pid}) 未加载最新模块: {[m[0] for m in stale_modules]}"
    elif etime_seconds < WARNING_MAX_AGE_SECONDS:
        # 进程刚启动 (10min 内) = 🟡 WARNING (cron 还没触发)
        level = FreshnessLevel.WARNING
        reason = f"进程 {name} (PID={pid}) 启动 {etime_seconds}s 内, cron 未充分验证"
    else:
        # 所有模块 fresh + 进程运行足够久 = 🟢 HEALTHY
        level = FreshnessLevel.HEALTHY
        reason = f"进程 {name} (PID={pid}) 运行 {etime_seconds}s ({etime_seconds/60:.1f}min), 所有关键模块 fresh"

    return ProcessFreshness(
        name=name,
        pid=pid,
        etime_seconds=etime_seconds,
        process_start_time=process_start_time,
        status="running",
        stale_modules=stale_modules,
        fresh_modules=fresh_modules,
        level=level,
        reason=reason,
    )


def run_process_freshness_check() -> dict:
    """
    主入口 (PIT #145 cron 调, 跟 PIT #141 run_pool_health_check 同款)

    Returns:
        {
            "timestamp": "2026-06-19T15:50:00+08:00",
            "level": "fresh" | "stale_warn" | "stale_critical",
            "processes": [
                {"name": "schedule_runner", "pid": 100791, "level": "fresh", "stale_modules": [], ...},
                ...
            ],
            "summary": "...",
        }
    """
    timestamp = datetime.now().isoformat()

    freshness_list = []
    max_level = FreshnessLevel.HEALTHY

    for process_config in MONITORED_PROCESSES:
        pf = check_process_freshness(process_config)
        freshness_list.append(pf.to_dict())

        # 升级 max_level
        if pf.level == FreshnessLevel.CRITICAL:
            max_level = FreshnessLevel.CRITICAL
        elif pf.level == FreshnessLevel.WARNING and max_level != FreshnessLevel.CRITICAL:
            max_level = FreshnessLevel.WARNING

    # 摘要
    critical_count = sum(1 for p in freshness_list if p["level"] == FreshnessLevel.CRITICAL)
    warning_count = sum(1 for p in freshness_list if p["level"] == FreshnessLevel.WARNING)
    healthy_count = sum(1 for p in freshness_list if p["level"] == FreshnessLevel.HEALTHY)

    summary_parts = []
    if critical_count > 0:
        summary_parts.append(f"🔴 {critical_count} 进程 stale (模块变更后未重启)")
    if warning_count > 0:
        summary_parts.append(f"🟡 {warning_count} 进程刚启动未验证")
    if healthy_count > 0:
        summary_parts.append(f"🟢 {healthy_count} 进程 fresh")

    summary = ", ".join(summary_parts) if summary_parts else "无进程监控"

    return {
        "timestamp": timestamp,
        "level": max_level,
        "processes": freshness_list,
        "summary": summary,
    }


# ─── 自我测试模式 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    """Self-test: 跑一次新鲜度检查 + 落库 (手动验证)"""
    import sys
    sys.path.insert(0, "/home/aileo/invest_system/scripts")

    print("=" * 60)
    print("PIT #145 process_freshness_monitor.py 自检")
    print("=" * 60)

    report = run_process_freshness_check()

    print(f"\n⏰ Timestamp: {report['timestamp']}")
    print(f"📊 Level: {report['level']}")
    print(f"📝 Summary: {report['summary']}")

    print(f"\n=== 进程详情 ({len(report['processes'])} 个) ===")
    for proc in report["processes"]:
        icon = {"fresh": "🟢", "stale_warn": "🟡", "stale_critical": "🔴"}.get(proc["level"], "❓")
        print(f"\n{icon} [{proc['level']}] {proc['name']}")
        print(f"   状态: {proc['status']}")
        if proc["pid"]:
            print(f"   PID: {proc['pid']}, etime: {proc['etime_seconds']}s ({proc['etime_seconds']/60:.1f}min)")
        print(f"   原因: {proc['reason']}")
        if proc["stale_modules"]:
            print(f"   ⚠️ Stale 模块: {[m[0] for m in proc['stale_modules']]}")
        if proc["fresh_modules"]:
            print(f"   ✅ Fresh 模块: {len(proc['fresh_modules'])} 个")