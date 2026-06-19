"""PoolManager — 统一 PG 连接池管理器 (PIT #140, 6/17 V2.8)

跨项目 class-level 教训 (PIT #138+#139 实战, 6/17 21:00 晚间工作流🔴):
- 连接池 = 银行窗口, 借了必须还, 覆盖变量 ≠ 归还连接 (PIT #138)
- cron 循环反复调脚本必走连接池, 直连 = 池监控盲区 (PIT #139)
- 长生命周期对象的内部 conn 应复用, 不是每次借

本模块:
1. 统一 _get_pool/_get_pg_conn/pg_cursor 3 个入口为单例 PoolManager
2. 加 health_check / get_count / put_count 不变量, 泄漏可观测
3. 向后兼容: `from storage_factory import _get_pool, _get_pg_conn, pg_cursor` 仍可用

用法 (新代码推荐):
    from pool_manager import get_pool
    with get_pool().get_cursor() as (conn, cur):
        cur.execute("SELECT 1")

用法 (老代码兼容):
    from storage_factory import pg_cursor  # 内部已转 PoolManager
    with pg_cursor() as (conn, cur):
        cur.execute("SELECT 1")

监控:
    from pool_manager import get_pool
    print(get_pool().health_check())
    # {'get_count': 100, 'put_count': 98, 'in_use': 2, 'free': 8, 'maxconn': 10, 'peak_in_use': 5, 'healthy': True}
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2 import pool as pg_pool
from dotenv import load_dotenv

# 加载 .env (跟 storage_factory 一致, PIT #138 沿用)
load_dotenv(Path(__file__).parent.parent / ".env")

# 优先使用 credentials 模块 (支持 WCM / 本地文件 / 环境变量)
try:
    from credentials import get_credential  # type: ignore
    _HAS_CREDENTIALS = True
except ImportError:
    _HAS_CREDENTIALS = False

logger = logging.getLogger(__name__)

# ─── 全局单例 ────────────────────────────────────────────────────────────────

_PM_SINGLETON: "PoolManager | None" = None
_PM_LOCK = threading.Lock()


# ─── PoolManager 类 ──────────────────────────────────────────────────────────


class PoolManager:
    """PG 连接池统一管理器 (单例)

    核心约束:
    - get_count >= put_count (借了必须还)
    - in_use <= maxconn (池满拒绝)
    - in_use = get_count - put_count (不变量)
    """

    def __init__(self, minconn: int = 2, maxconn: int = 20, host: str = "localhost",
                 user: str = "invest_admin", database: str = "investpilot"):
        self.minconn = minconn
        self.maxconn = maxconn
        self.host = host
        self.user = user
        self.database = database
        self._pool: Optional[pg_pool.ThreadedConnectionPool] = None
        self._lock = threading.Lock()

        # 不变量计数器 (PIT #138+#140 实战防御)
        self._get_count = 0  # 累计 getconn 次数
        self._put_count = 0  # 累计 putconn 次数
        self._peak_in_use = 0  # 历史峰值并发 (健康检查参考)

        # PIT #142 (6/17 P1-T2): 内部 invariant hook, 累加违反次数
        # 不 raise (不破坏调用方), 仅 logger.critical + 累计 _invariant_violations
        # PIT #141 cron 通过 health_check()['invariant_violations'] 检测 → 飞书推
        self._invariant_violations = 0
        self._last_violation_msg: Optional[str] = None

        # PIT #148 (6/19 17:39 实战升级): 池满持续时长跟踪
        # 实战教训: 17:34-17:39 in_use 从 0 涨到 10/10 (5min), 5min 间隔观察确认是临时并发占满
        # 真正泄漏标志: in_use == maxconn 持续 > 10min (e.g. 异常 conn 永远不还)
        # 临时并发占满: in_use == maxconn 但 < 5min 就恢复 (cron 跑完归还)
        # 用 maxconn_saturated_since 字段暴露给 PIT #141 cron: 持续 > 10min 才推 CRITICAL
        self._maxconn_saturated_since: Optional[float] = None  # time.time() when first hit maxconn

    def _ensure_pool(self):
        """懒初始化连接池 (线程安全)"""
        if self._pool is not None:
            return
        with self._lock:
            if self._pool is not None:
                return
            # PIT #138 修复: 导入 credentials 模块, 失败兜底到环境变量
            if _HAS_CREDENTIALS:
                pwd = get_credential("DB_PASSWORD")
            else:
                pwd = os.environ.get("DB_PASSWORD", "")
            self._pool = pg_pool.ThreadedConnectionPool(
                self.minconn, self.maxconn,
                host=self.host,
                user=self.user,
                database=self.database,
                password=pwd,
            )
            logger.info(f"[PoolManager] 池初始化: minconn={self.minconn} maxconn={self.maxconn}")

    def getconn(self):
        """从池借一个连接 (手动归还, 用 get_cursor 更安全)

        PIT #142 (6/17 P1-T2): 内部 invariant hook
        - 借后 _check_invariant() 验证 in_use = get - put (泄漏检测)
        - 违反时不 raise (不破坏调用方), 仅 logger.critical + 记 _invariant_violations
        - PIT #141 cron 通过 health_check()['invariant_violations'] 检测 → 飞书推
        """
        self._ensure_pool()
        try:
            conn = self._pool.getconn()
        except pg_pool.PoolError as e:
            logger.error(f"[PoolManager] 池满拒绝 getconn: {e}")
            raise
        self._get_count += 1
        in_use = self._get_count - self._put_count
        if in_use > self._peak_in_use:
            self._peak_in_use = in_use
        # PIT #148: 池满持续时长跟踪 (区分临时并发 vs 真泄漏)
        if in_use >= self.maxconn and self._maxconn_saturated_since is None:
            self._maxconn_saturated_since = time.time()
        elif in_use < self.maxconn and self._maxconn_saturated_since is not None:
            duration_s = time.time() - self._maxconn_saturated_since
            logger.info(f"[PoolManager] 池满解除, 持续 {duration_s:.1f}s (PIT #148)")
            self._maxconn_saturated_since = None
        # PIT #142: in-line invariant check (借后立刻验证, 不等 cron 5min)
        self._check_invariant()
        return conn

    def putconn(self, conn):
        """归还连接到池 (手动模式)

        PIT #142 (6/17 P1-T2): 内部 invariant hook
        - 还后 _check_invariant() 验证 put <= get (重复 put 检测)
        - 违反时不 raise (不破坏调用方), 仅 logger.critical + 记 _invariant_violations
        """
        if self._pool is None:
            return
        # 检查 conn 是否还活着 (PIT #138 修复: PG idle timeout 兜底)
        try:
            alive = not conn.closed
        except Exception:
            alive = False
        try:
            self._pool.putconn(conn)
            self._put_count += 1
            if not alive:
                logger.warning(f"[PoolManager] 归还了已关闭的 conn (PG idle timeout/异常断开), 池会清掉")
        except Exception as e:
            logger.error(f"[PoolManager] putconn 异常: {e}")
        # PIT #142: in-line invariant check (还后立刻验证)
        self._check_invariant()

    @contextmanager
    def get_cursor(self):
        """主入口: 借/还自动平衡 (上下文管理器)

        用法:
            with get_pool().get_cursor() as (conn, cur):
                cur.execute("SELECT 1")
        """
        conn = self.getconn()
        cur = None
        try:
            cur = conn.cursor()
            yield conn, cur
        except Exception:
            # 异常路径: rollback 后再 putconn, 避免半事务状态
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            self.putconn(conn)

    def _in_use_count(self) -> int:
        """当前在用 (不变量: in_use = get_count - put_count)"""
        return self._get_count - self._put_count

    def _check_invariant(self):
        """PIT #142 (6/17 P1-T2): 内部 invariant check hook
        PIT #148 (6/19 17:39 实战升级): 增加池满持续时长跟踪 (区分临时并发 vs 真泄漏)

        检查项:
          1. in_use >= 0 (put_count > get_count = 重复 put, 异常)
          2. in_use <= maxconn (借了不还 = 泄漏, 异常; 但池满时 raise, 这里不会发生)
          3. psycopg2 池内部 _used 跟我们的 in_use 一致 (PIT #140 不变量)

        不 raise (不破坏调用方), 仅 logger.critical + 累加 _invariant_violations
        PIT #141 cron 通过 health_check()['invariant_violations'] 检测 → 飞书推

        PIT #148 实战教训 (17:39 in_use=10/10 实战):
        - 原本以为 conn.close() 泄漏, 实际验证: get=18 put=8 diff=10, 但 in_use=10 diff=10 一致, 池满状态
        - 真正根因: 多个 cron 并发跑 (17:34 盘中异动 + 盘中异动关联扫描 同时触发) 临时占满
        - PIT #142 检查 2 "in_use > maxconn" 漏报 (in_use==maxconn 永远 false)
        - 修复: health_check() 新增 maxconn_saturated_since 字段, PIT #141 cron 检测"持续池满"
        """
        in_use = self._in_use_count()
        violations = []

        # 检查 1: put_count > get_count (重复 put)
        if self._put_count > self._get_count:
            violations.append(
                f"put_count({self._put_count}) > get_count({self._get_count}) 重复 put"
            )

        # 检查 2: in_use 超 maxconn (逻辑上池满会 raise, 但保险起见)
        if in_use > self.maxconn:
            violations.append(
                f"in_use({in_use}) > maxconn({self.maxconn}) 泄漏超限"
            )

        # 检查 3: psycopg2 池内部 _used 跟我们的 in_use 一致 (PIT #140 不变量)
        if self._pool is not None:
            try:
                pool_used = len(self._pool._used)  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pool_used = -1
            if pool_used >= 0 and in_use != pool_used:
                violations.append(
                    f"in_use({in_use}) != pool._used({pool_used}) 绕过或泄漏"
                )

        if violations:
            self._invariant_violations += 1
            msg = " | ".join(violations)
            self._last_violation_msg = msg
            logger.critical(f"[PoolManager] 不变量违反 #{self._invariant_violations}: {msg}")

    def get_count(self) -> int:
        return self._get_count

    def put_count(self) -> int:
        return self._put_count

    def health_check(self) -> dict:
        """池健康度报告 (PIT #138+#140 实战新增, 飞书告警 + 健康监控用)

        返回 dict:
            get_count:  累计 getconn 次数
            put_count:  累计 putconn 次数
            in_use:     当前在用 (get - put)
            free:       当前池空闲
            maxconn:    池上限
            peak_in_use:历史峰值并发
            healthy:    True (in_use 一致 + 不超 maxconn)
            invariant:  'OK' 或具体错误描述
        """
        in_use = self._in_use_count()
        # psycopg2 ThreadedConnectionPool 内部用 _pool (free list) + _used (借用 dict)
        # LSP 警告: 这些是私有属性, 但 psycopg2 2.9+ 一直稳定, 跨项目代码已实战
        free = 0
        pool_used = 0
        if self._pool is not None:
            try:
                free = len(self._pool._pool)  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                free = -1  # 标记不可观测
            try:
                pool_used = len(self._pool._used)  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pool_used = -1

        # 不变量检查: 我们的 in_use 应等于池的 _used (理论上)
        if pool_used < 0:
            invariant = "psycopg2 池内部不可观测 (跳过不变量)"
        elif in_use == pool_used:
            invariant = "OK"
        else:
            invariant = f"我们的 in_use={in_use} != 池 _used={pool_used} (泄漏或绕过)"

        healthy = (pool_used < 0) or ((in_use == pool_used) and (in_use <= self.maxconn))

        # PIT #148 (6/19 17:39 实战升级): 池满持续时长暴露
        # 区分临时并发占满 (e.g. 17:34-17:39 5min 临时并发) vs 真泄漏 (持续 > 10min)
        # PIT #141 cron 检测 maxconn_saturated_duration_s > 600 (10min) 才推 CRITICAL
        maxconn_saturated_duration_s = None
        if self._maxconn_saturated_since is not None:
            maxconn_saturated_duration_s = time.time() - self._maxconn_saturated_since

        return {
            "get_count": self._get_count,
            "put_count": self._put_count,
            "in_use": in_use,
            "free": free,
            "maxconn": self.maxconn,
            "peak_in_use": self._peak_in_use,
            "pool_internal_used": pool_used,
            "healthy": healthy,
            "invariant": invariant,
            # PIT #142 (6/17 P1-T2): 暴露 invariant hook 状态
            "invariant_violations": self._invariant_violations,
            "last_violation_msg": self._last_violation_msg,
            # PIT #148 (6/19 17:39 实战): 池满持续时长
            "maxconn_saturated_since": self._maxconn_saturated_since,
            "maxconn_saturated_duration_s": maxconn_saturated_duration_s,
        }

    def close(self):
        """关闭池 (测试用, 生产调用少)"""
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None


# ─── 单例工厂 ────────────────────────────────────────────────────────────────


def get_pool() -> PoolManager:
    """获取 PoolManager 单例 (线程安全)"""
    global _PM_SINGLETON
    if _PM_SINGLETON is not None:
        return _PM_SINGLETON
    with _PM_LOCK:
        if _PM_SINGLETON is None:
            _PM_SINGLETON = PoolManager()
        return _PM_SINGLETON


def reset_pool_for_testing():
    """测试用: 重置单例 (生产代码不要调)"""
    global _PM_SINGLETON
    with _PM_LOCK:
        if _PM_SINGLETON is not None:
            try:
                _PM_SINGLETON.close()
            except Exception:
                pass
        _PM_SINGLETON = None