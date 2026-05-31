"""
db_optimizer.py — 数据库查询优化模块
提供索引管理、查询分析、连接池优化
"""

import logging
import time
from typing import Optional

import psycopg2
import psycopg2.pool

logger = logging.getLogger("invest_system.db_optimizer")

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",
    "minconn": 2,
    "maxconn": 10,
}

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

# 推荐索引定义
RECOMMENDED_INDEXES = [
    ("market.daily_quotes", "idx_dq_ts_code_date", "ts_code, trade_date DESC"),
    ("market.daily_quotes", "idx_dq_trade_date", "trade_date DESC"),
    ("market.financial_indicators", "idx_fi_ts_code_date", "ts_code, report_date DESC"),
    ("market.financial_indicators", "idx_fi_report_date", "report_date DESC"),
    ("research.research_reports", "idx_rr_report_date", "report_date DESC"),
    ("research.research_reports", "idx_rr_info_code", "info_code"),
    ("research.announcements", "idx_ann_notice_date", "notice_date DESC"),
    ("research.announcements", "idx_ann_ts_code", "ts_code"),
    ("audit.audit_log", "idx_audit_event_time", "event_time DESC"),
    ("audit.audit_log", "idx_audit_event_type", "event_type"),
]


def _get_db_conn():
    """获取数据库连接"""
    from pgcrypto_migration import get_credential
    cfg = dict(DB_CONFIG)
    cfg["password"] = get_credential("DB_PASSWORD")
    return psycopg2.connect(**{k: v for k, v in cfg.items() if k in ("host", "user", "database", "password")})


def get_pool():
    """
    获取数据库连接池（线程安全）

    Returns:
        ThreadedConnectionPool 实例
    """
    global _pool
    if _pool is not None:
        return _pool

    from pgcrypto_migration import get_credential
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=DB_CONFIG["minconn"],
        maxconn=DB_CONFIG["maxconn"],
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        database=DB_CONFIG["database"],
        password=get_credential("DB_PASSWORD"),
    )
    logger.info(f"连接池已创建: min={DB_CONFIG['minconn']}, max={DB_CONFIG['maxconn']}")
    return _pool


def get_pool_conn():
    """从连接池获取连接"""
    return get_pool().getconn()


def put_pool_conn(conn):
    """归还连接到连接池"""
    get_pool().putconn(conn)


def analyze_slow_queries() -> list[dict]:
    """
    分析慢查询（PostgreSQL pg_stat_statements）

    Returns:
        慢查询列表，含 query/calls/mean_time/max_time
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT query,
                   calls,
                   mean_exec_time::numeric(10,2) as mean_ms,
                   max_exec_time::numeric(10,2) as max_ms,
                   total_exec_time::numeric(10,2) as total_ms
            FROM pg_stat_statements
            WHERE query NOT LIKE '%pg_stat%'
            ORDER BY mean_exec_time DESC
            LIMIT 20
        """)
        rows = cur.fetchall()
        return [
            {
                "query": (r[0][:100] + "..." if len(r[0] or "") > 100 else r[0] or ""),
                "calls": r[1],
                "mean_ms": float(r[2] or 0),
                "max_ms": float(r[3] or 0),
                "total_ms": float(r[4] or 0),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"慢查询分析失败（可能需要启用 pg_stat_statements 扩展）: {e}")
        return []
    finally:
        conn.close()


def create_recommended_indexes(dry_run: bool = False) -> dict:
    """
    创建推荐索引

    Args:
        dry_run: 仅检查不执行

    Returns:
        {"created": [...], "existing": [...], "failed": [...]}
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    created = []
    existing = []
    failed = []

    try:
        for schema_table, idx_name, columns in RECOMMENDED_INDEXES:
            schema, table = schema_table.split(".")

            # 检查索引是否存在
            cur.execute("""
                SELECT 1 FROM pg_indexes
                WHERE schemaname = %s AND tablename = %s AND indexname = %s
            """, (schema, table, idx_name))
            if cur.fetchone():
                existing.append(idx_name)
                continue

            if dry_run:
                created.append(idx_name)
                continue

            # 创建索引
            try:
                cur.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{idx_name}" ON {schema_table} ({columns})')
                conn.commit()
                created.append(idx_name)
                logger.info(f"索引已创建: {idx_name}")
            except Exception as e:
                conn.rollback()
                failed.append({"index": idx_name, "error": str(e)})
                logger.warning(f"索引创建失败 {idx_name}: {e}")

        return {"created": created, "existing": existing, "failed": failed}
    finally:
        conn.close()


def get_table_stats() -> list[dict]:
    """获取各表行数和大小统计"""
    conn = _get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT schemaname || '.' || tablename as table_name,
                   n_live_tup::bigint as row_count,
                   pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as total_size
            FROM pg_stat_user_tables
            ORDER BY n_live_tup DESC
        """)
        rows = cur.fetchall()
        return [
            {"table": r[0], "rows": r[1], "size": r[2]}
            for r in rows
        ]
    finally:
        conn.close()


def optimize_pg_config() -> dict:
    """
    检查并建议 PostgreSQL 配置优化

    Returns:
        优化建议列表
    """
    conn = _get_db_conn()
    cur = conn.cursor()
    suggestions = []
    try:
        # 检查 shared_buffers
        cur.execute("SHOW shared_buffers")
        sb = cur.fetchone()[0]
        suggestions.append({"param": "shared_buffers", "current": sb, "recommended": "256MB+"})

        # 检查 work_mem
        cur.execute("SHOW work_mem")
        wm = cur.fetchone()[0]
        suggestions.append({"param": "work_mem", "current": wm, "recommended": "16MB+"})

        # 检查 effective_cache_size
        cur.execute("SHOW effective_cache_size")
        ec = cur.fetchone()[0]
        suggestions.append({"param": "effective_cache_size", "current": ec, "recommended": "1GB+"})

        # 检查 maintenance_work_mem
        cur.execute("SHOW maintenance_work_mem")
        mw = cur.fetchone()[0]
        suggestions.append({"param": "maintenance_work_mem", "current": mw, "recommended": "64MB+"})
    finally:
        conn.close()

    return suggestions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== 表统计 ===")
    for s in get_table_stats():
        print(f"  {s['table']:40s} {s['rows']:>8,} rows  {s['size']}")

    print("\n=== 索引检查 ===")
    result = create_recommended_indexes(dry_run=True)
    print(f"  需创建: {len(result['created'])}")
    print(f"  已存在: {len(result['existing'])}")

    print("\n=== 慢查询 ===")
    for q in analyze_slow_queries():
        print(f"  {q['mean_ms']:8.2f}ms x{q['calls']:>5}  {q['query'][:80]}")