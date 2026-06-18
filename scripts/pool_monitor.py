"""
pool_monitor.py — PG 连接池健康监控 + 主动告警 (PIT #141, 6/17 P0-T2)

跨项目铁律 (PIT #138+#139+#140+#141 实战链, 6/17 V2.8):
- 多个池入口分散 + 无 health_check = 等到 PoolError 才知道泄漏 (被动)
- 抽 PoolManager 单例 + health_check + cron 监控 = 主动告警, 不等 PoolError
- PIT #141 升级: 加阈值告警 + 冷却防 spam + cron 落库 + 飞书推送

设计:
- 三级告警: 🔴 CRITICAL (in_use > 80% maxconn 或 invariant != OK)
              🟡 WARNING  (in_use > 50% maxconn 或 peak > 90% maxconn)
              🟢 HEALTHY  (其他)
- 冷却防 spam: 同级别告警 cooldown 30min (避免 5min 间隔连发)
- 数据库落库: 写 audit.pool_health_metrics (新建表) 便于 dashboard 趋势
- 飞书推送: 🔴 CRITICAL 推飞书, 🟡 WARNING 写日志, 🟢 HEALTHY 静默
- 单点集成: schedule_runner job_pool_health cron 5min 调一次

用法 (新代码推荐):
    from pool_monitor import PoolMonitor
    from pool_manager import get_pool
    monitor = PoolMonitor()
    report = monitor.check_and_alert(get_pool())
    print(report)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─── 告警级别 + 阈值常量 ────────────────────────────────────────────────

class AlertLevel:
    """告警级别常量 (跟 notification.py level_map 对齐)"""
    HEALTHY = "healthy"    # 静默
    WARNING = "warning"    # 写日志
    CRITICAL = "critical"  # 推飞书


# 阈值 (基于 maxconn 比例, 默认 maxconn=10)
THRESHOLD_CRITICAL_USAGE = 0.80   # in_use > 80% maxconn → 🔴 CRITICAL
THRESHOLD_WARNING_USAGE = 0.50    # in_use > 50% maxconn → 🟡 WARNING
THRESHOLD_CRITICAL_PEAK = 0.95    # peak > 95% maxconn → 🔴 CRITICAL (历史峰值过高预警)
THRESHOLD_WARNING_PEAK = 0.90     # peak > 90% maxconn → 🟡 WARNING

# 冷却时间 (秒): 同级别告警 cooldown, 防 spam
COOLDOWN_SECONDS = 30 * 60  # 30 min


# ─── 报告数据结构 ──────────────────────────────────────────────────────

@dataclass
class PoolHealthReport:
    """池健康检查报告 (一次 check 的快照)"""
    timestamp: str                              # ISO 格式时间戳
    level: str                                  # HEALTHY/WARNING/CRITICAL
    message: str                                # 摘要 (推飞书用)
    detail: dict = field(default_factory=dict)  # 完整 health_check dict + 上下文

    def to_dict(self) -> dict:
        return asdict(self)


# ─── PoolMonitor 主类 ──────────────────────────────────────────────────

class PoolMonitor:
    """PG 连接池监控器 (PIT #141, 6/17 P0-T2)

    职责:
    1. 调 PoolManager.health_check() 拿到原始数据
    2. 按阈值判断告警级别 (HEALTHY/WARNING/CRITICAL)
    3. 冷却防 spam (同一级别 cooldown 30min)
    4. 落库 audit.pool_health_metrics
    5. CRITICAL 级别推飞书 (PIT #112 兼容)
    """

    def __init__(self, cooldown_seconds: int = COOLDOWN_SECONDS):
        self.cooldown_seconds = cooldown_seconds
        self._last_alert_level: Optional[str] = None
        self._last_alert_ts: float = 0.0
        # 累计统计 (告警频率参考)
        self._total_checks = 0
        self._total_alerts = 0

    # ─── 主入口 ─────────────────────────────────────────────────────────

    def check_and_alert(self, pool) -> PoolHealthReport:
        """核心: 检查 + 告警 + 落库, 返回报告对象

        用法:
            from pool_manager import get_pool
            from pool_monitor import PoolMonitor
            monitor = PoolMonitor()
            report = monitor.check_and_alert(get_pool())
        """
        self._total_checks += 1

        # 1. 调 PoolManager.health_check()
        try:
            health = pool.health_check()
        except Exception as e:
            logger.error(f"[PoolMonitor] health_check 异常: {e}")
            return PoolHealthReport(
                timestamp=datetime.now().isoformat(),
                level=AlertLevel.CRITICAL,
                message=f"pool.health_check() 异常: {e}",
                detail={"error": str(e)},
            )

        # 2. 判断告警级别
        level, message = self._evaluate(health)

        # 3. 检查冷却 (防 spam)
        if level != AlertLevel.HEALTHY:
            in_cooldown = self._is_in_cooldown(level)
            if in_cooldown:
                logger.debug(
                    f"[PoolMonitor] {level} 告警在冷却期 (距上次 "
                    f"{time.time() - self._last_alert_ts:.0f}s < {self.cooldown_seconds}s), 静默"
                )
                # 不推飞书, 不计入告警数, 但仍落库 (trend 数据完整)
                report = PoolHealthReport(
                    timestamp=datetime.now().isoformat(),
                    level=level,
                    message=message,
                    detail={**health, "cooldown": True},
                )
                self._persist(report)
                return report

            # 4. 推飞书 (仅 CRITICAL, WARNING 写日志)
            self._send_alert(level, message, health)
            self._last_alert_level = level
            self._last_alert_ts = time.time()
            self._total_alerts += 1

        # 5. 落库
        report = PoolHealthReport(
            timestamp=datetime.now().isoformat(),
            level=level,
            message=message,
            detail=health,
        )
        self._persist(report)
        return report

    # ─── 阈值评估 ────────────────────────────────────────────────────────

    def _evaluate(self, health: dict) -> tuple[str, str]:
        """根据 health_check 结果判断告警级别 + 摘要

        返回: (level, message)
        """
        in_use = health.get("in_use", 0)
        maxconn = health.get("maxconn", 10)
        peak = health.get("peak_in_use", 0)
        invariant = health.get("invariant", "OK")
        healthy = health.get("healthy", True)

        # 紧急: invariant 违反 (泄漏或绕过) 或 in_use 超 80%
        if invariant != "OK" or in_use > maxconn * THRESHOLD_CRITICAL_USAGE:
            return AlertLevel.CRITICAL, (
                f"🔴 PG 池紧急: invariant={invariant}, "
                f"in_use={in_use}/{maxconn} ({in_use/maxconn:.0%}), "
                f"peak={peak}/{maxconn}"
            )

        # PIT #142 (6/17 P1-T2): 内部 invariant hook 检测
        # _invariant_violations > 0 = getconn/putconn 路径已记录不变量违反 (重复put/泄漏超限/绕过池)
        inv_violations = health.get("invariant_violations", 0)
        if inv_violations > 0:
            last_msg = health.get("last_violation_msg", "unknown")
            return AlertLevel.CRITICAL, (
                f"🔴 PG 池不变量违反 #{inv_violations}: {last_msg} "
                f"(get_count={health.get('get_count')}, put_count={health.get('put_count')})"
            )

        # 紧急: 历史峰值超 95% (容量预警, 即使当前 in_use=0)
        if peak > maxconn * THRESHOLD_CRITICAL_PEAK:
            return AlertLevel.CRITICAL, (
                f"🔴 PG 池容量预警: peak_in_use={peak}/{maxconn} ({peak/maxconn:.0%}) 超 95%, "
                f"建议 maxconn 调高到 {int(peak * 1.5)}"
            )

        # 警告: in_use 超 50% 或 peak 超 90%
        if in_use > maxconn * THRESHOLD_WARNING_USAGE or peak > maxconn * THRESHOLD_WARNING_PEAK:
            return AlertLevel.WARNING, (
                f"🟡 PG 池警告: in_use={in_use}/{maxconn} ({in_use/maxconn:.0%}), "
                f"peak={peak}/{maxconn} ({peak/maxconn:.0%})"
            )

        # 健康
        return AlertLevel.HEALTHY, (
            f"🟢 PG 池健康: in_use={in_use}/{maxconn}, peak={peak}/{maxconn}, "
            f"invariant={invariant}"
        )

    # ─── 冷却检查 ────────────────────────────────────────────────────────

    def _is_in_cooldown(self, level: str) -> bool:
        """检查是否在冷却期 (同级别告警 30min 内不重复推飞书)"""
        if self._last_alert_level != level:
            return False  # 不同级别不冷却
        elapsed = time.time() - self._last_alert_ts
        return elapsed < self.cooldown_seconds

    # ─── 飞书告警 ────────────────────────────────────────────────────────

    def _send_alert(self, level: str, message: str, health: dict):
        """推飞书告警 (仅 CRITICAL 级别推, WARNING 只写日志)

        PIT #112 兼容: 异常路径自动捕获, 不影响 cron 继续调度
        """
        if level == AlertLevel.WARNING:
            logger.warning(message)
            return

        if level == AlertLevel.CRITICAL:
            logger.error(message)
            # 推飞书 (复用 notification.py 已有推送逻辑)
            try:
                # PIT #112 兼容: from notification import send_error_alert
                from notification import send_error_alert
                detail = (
                    f"**{message}**\n\n"
                    f"**健康快照**:\n"
                    f"• get_count: {health.get('get_count')}\n"
                    f"• put_count: {health.get('put_count')}\n"
                    f"• in_use: {health.get('in_use')}/{health.get('maxconn')}\n"
                    f"• free: {health.get('free')}\n"
                    f"• peak_in_use: {health.get('peak_in_use')}\n"
                    f"• invariant: {health.get('invariant')}\n"
                    f"• time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"**PIT #141 主动告警** (threshold: critical={THRESHOLD_CRITICAL_USAGE:.0%}, "
                    f"peak_critical={THRESHOLD_CRITICAL_PEAK:.0%})"
                )
                send_error_alert(f"PG 池健康告警 ({level})", detail)
            except Exception as e:
                # PIT #112 兼容: 推送失败不递归
                logger.error(f"[PoolMonitor] 飞书推送失败 (不递归): {e}")

    # ─── 数据库落库 ──────────────────────────────────────────────────────

    def _persist(self, report: PoolHealthReport):
        """写 audit.pool_health_metrics 表 (新建)

        PIT #12 铁律: 写 SQL 前必查 information_schema.columns 确认真实列名
        表结构 (PIT #141 实战设计, 6/17 23:00 创建):
            CREATE TABLE IF NOT EXISTS audit.pool_health_metrics (
                id BIGSERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                detail JSONB NOT NULL,
                in_use INT,
                peak_in_use INT,
                maxconn INT,
                invariant TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pool_health_timestamp
                ON audit.pool_health_metrics (timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_pool_health_level
                ON audit.pool_health_metrics (level);

        落库失败不致命 (PIT #119 兼容), 失败静默写日志
        """
        try:
            from storage_factory import pg_cursor  # PIT #140 内部转 PoolManager
            with pg_cursor() as (conn, cur):
                if conn is None:
                    logger.debug("[PoolMonitor] conn=None, 跳过落库")
                    return
                # PIT #117: _safe_num 防御 jsonb None
                detail_json = json.dumps(report.detail, default=str)
                cur.execute(
                    """
                    INSERT INTO audit.pool_health_metrics
                      (level, message, detail, in_use, peak_in_use, maxconn, invariant)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        report.level,
                        report.message,
                        detail_json,
                        report.detail.get("in_use"),
                        report.detail.get("peak_in_use"),
                        report.detail.get("maxconn"),
                        report.detail.get("invariant"),
                    ),
                )
                conn.commit()
        except Exception as e:
            # 落库失败不致命 (PIT #119 兼容), 写 debug 日志
            logger.debug(f"[PoolMonitor] 落库失败 (非致命): {e}")

    # ─── 统计 ────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """监控器自身统计 (调试用)"""
        return {
            "total_checks": self._total_checks,
            "total_alerts": self._total_alerts,
            "alert_rate": f"{(self._total_alerts / self._total_checks * 100) if self._total_checks > 0 else 0:.1f}%",
            "last_alert_level": self._last_alert_level,
            "last_alert_age_sec": (time.time() - self._last_alert_ts) if self._last_alert_ts > 0 else None,
        }


# ─── 快捷函数 (cron 调用) ──────────────────────────────────────────────

def run_pool_health_check() -> dict:
    """schedule_runner cron 调用入口: 返回 dict 供 track_cron_task 解析

    Usage (schedule_runner.py):
        @track_cron_task("PG 池健康监控")
        def job_pool_health():
            from pool_monitor import run_pool_health_check
            return run_pool_health_check()
    """
    from pool_manager import get_pool

    monitor = PoolMonitor()
    report = monitor.check_and_alert(get_pool())

    return {
        "processed": 1,
        "failed": 0 if report.level != AlertLevel.CRITICAL else 1,
        "level": report.level,
        "message": report.message,
        "in_use": report.detail.get("in_use"),
        "peak_in_use": report.detail.get("peak_in_use"),
        "maxconn": report.detail.get("maxconn"),
        "invariant": report.detail.get("invariant"),
    }


# ─── 模块自测 (PIT #136+#137+#140 自检模式) ────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=== PoolMonitor 自检 ===\n")

    from pool_manager import get_pool

    monitor = PoolMonitor()
    pool = get_pool()

    # 测试 1: 单次 check_and_alert
    print("--- 测试 1: 单次 check_and_alert ---")
    report = monitor.check_and_alert(pool)
    print(f"  level: {report.level}")
    print(f"  message: {report.message}")
    print(f"  detail: {report.detail}")

    # 测试 2: 强制 CRITICAL (篡改 in_use 模拟泄漏)
    print("\n--- 测试 2: 模拟泄漏 (强制 in_use=9, maxconn=10) ---")
    original_get = pool._get_count
    pool._get_count += 9  # 模拟泄漏 9 个 conn (但只 +9 不 put, 触发 invariant 违反)
    report2 = monitor.check_and_alert(pool)
    print(f"  level: {report2.level}")
    print(f"  message: {report2.message}")
    pool._get_count = original_get  # 还原

    # 测试 3: 冷却检查 (5min 内连续 3 次 CRITICAL 应该只告警 1 次)
    print("\n--- 测试 3: 冷却防 spam ---")
    monitor2 = PoolMonitor(cooldown_seconds=10)  # 缩到 10s 测试
    pool._get_count += 9  # 再次触发泄漏
    for i in range(3):
        r = monitor2.check_and_alert(pool)
        print(f"  call #{i+1}: level={r.level}, cooldown={r.detail.get('cooldown', False)}")
        time.sleep(1)
    pool._get_count = original_get

    # 测试 4: 统计
    print("\n--- 测试 4: 监控器统计 ---")
    print(f"  stats: {monitor.stats()}")

    print("\n=== 自检完成 ===")
    sys.exit(0)