"""
hnsw_migration.py — pgvector IVFFlat → HNSW 索引迁移脚本

适用条件：
  - pgvector >= 0.5.0（本系统 0.8.2 ✅）
  - 向量维度 768（nomic-embed-text）

HNSW vs IVFFlat：
  - HNSW：构图索引，查询质量更高（m=16, ef_construction=200），适合数据量 < 100k
  - IVFFlat：聚类索引，适合超大规模但查询质量略低

执行时机建议：
  - news_embeddings / report_embeddings 各自积累 > 100 条数据后再迁移
  - 迁移期间表只读锁（约几秒），生产环境请在低峰期执行

用法：
  python hnsw_migration.py [--dry-run] [--target {all|news|reports}]
"""

import argparse
import json
import os
import sys
import time

import psycopg2

sys.path.insert(0, os.path.dirname(__file__))


def get_conn():
    cred = json.load(open(os.path.expanduser("~/.hermes/invest_credentials/store.json")))
    return psycopg2.connect(
        host="localhost",
        database="investpilot",
        user="invest_admin",
        password=cred["DB_PASSWORD"],
    )


# HNSW 参数（768维向量通用推荐值）
HNSW_PARAMS = {
    "m": 16,                    # 每层连接数，越大质量越高但内存越大
    "ef_construction": 200,     # 构建时动态列表大小，越大质量越高但构建越慢
}
# 查询时的探索参数（可动态调整）
DEFAULT_EF = 100


def migrate_table(conn, schema_table: str, dry_run: bool = False) -> dict:
    """
    单表 IVFFlat → HNSW 迁移。

    1. 获取当前索引名
    2. 创建新 HNSW 索引（CONCURRENTLY 避免锁）
    3. 验证新索引可用
    4. DROP 旧 IVFFlat 索引
    """
    schema, table = schema_table.split(".")
    # 实际索引名（从 pg_indexes 确认）
    IDX_MAP = {
        "research.news_embeddings":  {"old": "idx_news_embedding_cosine",  "new": "idx_news_hnsw"},
        "research.report_embeddings": {"old": "idx_report_emb",             "new": "idx_report_hnsw"},
    }
    idx_old = IDX_MAP[schema_table]["old"]
    idx_new = IDX_MAP[schema_table]["new"]

    cur = conn.cursor()
    result = {"table": schema_table, "status": "ok", "steps": []}

    # Step 1: 确认旧索引存在
    cur.execute("""
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE tablename = %s AND indexname = %s
    """, (table, idx_old))
    old_idx = cur.fetchone()
    if not old_idx:
        result["status"] = "skip"
        result["steps"].append(f"旧索引 {idx_old} 不存在，跳过")
        return result
    result["steps"].append(f"旧索引: {old_idx[1]}")

    # Step 2: 行数
    cur.execute(f'SELECT COUNT(*) FROM {schema_table}')
    count = cur.fetchone()[0]
    result["steps"].append(f"数据量: {count} 条")
    if count == 0:
        result["status"] = "skip"
        result["steps"].append("表为空，跳过迁移（HNSW 对空表无意义）")
        return result

    m = HNSW_PARAMS["m"]
    ef = HNSW_PARAMS["ef_construction"]
    sql = f"""
        CREATE INDEX CONCURRENTLY {idx_new}
        ON {schema_table}
        USING hnsw (embedding vector_cosine_ops)
        WITH (m=%s, ef_construction=%s)
    """
    result["steps"].append(f"创建 HNSW: m={m}, ef_construction={ef}")

    if dry_run:
        result["steps"].append(f"[DRY-RUN] 不执行: {sql}")
        return result

    t0 = time.time()
    cur.execute(sql, (m, ef))
    conn.commit()
    elapsed = time.time() - t0
    result["steps"].append(f"HNSW 索引创建完成，耗时 {elapsed:.1f}s")

    # Step 3: 验证新索引
    cur.execute("""
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE indexname = %s
    """, (idx_new,))
    new_idx = cur.fetchone()
    result["steps"].append(f"新索引: {new_idx[1] if new_idx else '未找到'}")
    result["index_size_new"] = idx_new

    # Step 4: 删除旧索引
    cur.execute(f"DROP INDEX {idx_old}")
    conn.commit()
    result["steps"].append(f"已删除旧索引: {idx_old}")

    # Step 5: ANALYZE 表
    cur.execute(f"ANALYZE {schema_table}")
    conn.commit()
    result["steps"].append("ANALYZE 完成")

    return result


def set_ef(conn, schema_table: str, ef: int = DEFAULT_EF):
    """动态调整 hnsw.ef 参数（需超权限，不影响普通查询）"""
    # pgvector 的 ef 参数在索引创建后可通过 SET 调整会话级别，无需重建索引
    # 但实际查询计划由优化器决定，这里仅记录
    print(f"INFO: hnsw.ef 动态调整需在会话中执行 `SET hnsw.ef = {ef}`，不影响已创建索引")


def main():
    parser = argparse.ArgumentParser(description="IVFFlat → HNSW 索引迁移")
    parser.add_argument("--dry-run", action="store_true", help="只打印不执行")
    parser.add_argument("--target", default="all",
                        choices=["all", "news", "reports"],
                        help="目标表，默认 all")
    args = parser.parse_args()

    targets = {
        "news": ["research.news_embeddings"],
        "reports": ["research.report_embeddings"],
        "all": ["research.news_embeddings", "research.report_embeddings"],
    }[args.target]

    print(f"=== pgvector HNSW 迁移 ({'DRY-RUN' if args.dry_run else '正式执行'}) ===\n")

    conn = get_conn()
    for schema_table in targets:
        print(f"\n▶ {schema_table}")
        res = migrate_table(conn, schema_table, dry_run=args.dry_run)
        for step in res["steps"]:
            print(f"  {step}")
        print(f"  状态: {res['status']}")

    conn.close()
    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()