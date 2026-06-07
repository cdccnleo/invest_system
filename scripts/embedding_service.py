"""
embedding_service.py — 向量记忆系统
基于 pgvector + Ollama nomic-embed-text，实现新闻/研报的语义检索
"""

import os
import json
import time
import logging
import urllib.request
import psycopg2
from typing import Optional

from scripts.utils import chunk_text

logger = logging.getLogger("invest_system.embedding")

OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = "nomic-embed-text:latest"
EMBED_DIM = 768

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",  # 运行时由 _get_password() 注入
}

def _get_password():
    from pgcrypto_migration import get_credential
    return get_credential("DB_PASSWORD")

def get_db_conn():
    cfg = dict(DB_CONFIG)
    cfg["password"] = _get_password()
    return psycopg2.connect(**cfg)


def get_embedding(text: str, model: str = EMBED_MODEL) -> Optional[list[float]]:
    """调用 Ollama 生成文本向量"""
    payload = {"model": model, "prompt": text}
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/embeddings",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get("embedding")
    except Exception as e:
        logger.warning(f"Embedding 生成失败: {e}")
        return None


def init_vector_tables():
    """初始化向量存储表（仅运行一次）"""
    conn = get_db_conn()
    cur = conn.cursor()

    # news_embeddings 表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS research.news_embeddings (
            id BIGSERIAL PRIMARY KEY,
            article_id BIGINT NOT NULL REFERENCES research.news_articles(id) ON DELETE CASCADE,
            content_chunk TEXT NOT NULL,
            embedding vector(768) NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # 向量索引（cosine 相似度）
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_embedding_cosine
        ON research.news_embeddings USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)
    conn.commit()
    logger.info("向量表 research.news_embeddings 初始化完成")
    conn.close()


def embed_news_articles(article_ids: list[int] = None, limit: int = 50):
    """
    将新闻文章向量化并写入 research.news_embeddings
    - 自动分块（每块 ~500 字符）
    - 仅处理尚未嵌入的文章
    """
    conn = get_db_conn()
    cur = conn.cursor()

    # 查询待处理文章
    if article_ids:
        cur.execute("""
            SELECT na.id, na.title, LEFT(na.content, 2000)
            FROM research.news_articles na
            WHERE na.id = ANY(%s)
              AND na.id NOT IN (SELECT DISTINCT article_id FROM research.news_embeddings)
            LIMIT %s
        """, (article_ids, limit))
    else:
        cur.execute("""
            SELECT na.id, na.title, LEFT(na.content, 2000)
            FROM research.news_articles na
            WHERE na.id NOT IN (SELECT DISTINCT article_id FROM research.news_embeddings)
            ORDER BY na.published_at DESC
            LIMIT %s
        """, (limit,))

    articles = cur.fetchall()
    logger.info(f"待嵌入文章: {len(articles)} 篇")

    embedded = 0
    for art_id, title, content in articles:
        text = f"{title}。{content or ''}"
        chunks = chunk_text(text, max_chars=500)

        for chunk_idx, chunk in enumerate(chunks):
            emb = get_embedding(chunk)
            if emb is None:
                continue
            try:
                cur.execute("""
                    INSERT INTO research.news_embeddings
                        (article_id, content_chunk, embedding)
                    VALUES (%s, %s, %s)
                """, (art_id, chunk, emb))
                embedded += 1
            except Exception as e:
                logger.warning(f"写入 embedding 失败 (article_id={art_id}): {e}")

        time.sleep(0.5)  # 避免 Ollama 过载

    conn.commit()
    logger.info(f"嵌入完成: {embedded} 条 chunks")
    conn.close()
    return embedded


def search_similar_news(query: str, top_k: int = 5, min_score: float = 0.5) -> list[dict]:
        """语义搜索相似新闻
        返回: [{article_id, title, content, similarity, published_at}, ...]
        """
        query_emb = get_embedding(query)
        if query_emb is None:
            logger.warning("Query embedding 生成失败")
            return []

        conn = get_db_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT
                    ne.article_id,
                    na.title,
                    LEFT(ne.content_chunk, 300),
                    1 - (ne.embedding <=> %s::vector) AS similarity,
                    na.published_at
                FROM research.news_embeddings ne
                JOIN research.news_articles na ON na.id = ne.article_id
                WHERE 1 - (ne.embedding <=> %s::vector) > %s
                ORDER BY ne.embedding <=> %s::vector
                LIMIT %s
            """, (query_emb, query_emb, min_score, query_emb, top_k))

            results = []
            for row in cur.fetchall():
                results.append({
                    "article_id": row[0],
                    "title": row[1],
                    "content_chunk": row[2],
                    "similarity": round(float(row[3]), 4),
                    "published_at": row[4].isoformat() if row[4] else None,
                })
            return results
        except Exception as e:
            logger.warning(f"向量搜索失败: {e}")
            return []
        finally:
            conn.close()


def get_news_context_for_prompt(query: str, top_k: int = 8) -> str:
    """
    生成用于 LLM Prompt 的新闻上下文摘要
    """
    results = search_similar_news(query, top_k=top_k, min_score=0.3)
    if not results:
        return "近期无高度相关新闻。"

    lines = []
    for r in results:
        date_str = r["published_at"][:10] if r["published_at"] else "未知日期"
        lines.append(f"- [{date_str}] {r['title']} (相似度 {r['similarity']:.2f})")
        lines.append(f"  {r['content_chunk'][:150]}...")

    return "\n".join(lines)


# ─── 研报向量化 ────────────────────────────────────────────────────────────

def embed_research_report(report_id: int, text: str = None) -> bool:
    """
    将单条研报向量化并写入 research.report_embeddings

    Returns:
        True if embedding was generated and stored, False otherwise
    """
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        if text is None:
            cur.execute("""
                SELECT title, summary, source
                FROM research.research_reports
                WHERE id = %s
            """, (report_id,))
            row = cur.fetchone()
            if not row:
                return False
            title, summary, source = row
            text = f"{source}研报：{title}。{summary or ''}"

        chunks = chunk_text(text, max_chars=500)
        stored = 0
        for idx, chunk in enumerate(chunks):
            emb = get_embedding(chunk)
            if emb is None:
                continue
            try:
                cur.execute("""
                    INSERT INTO research.report_embeddings
                        (report_id, chunk_index, content, embedding)
                    VALUES (%s, %s, %s, %s)
                """, (report_id, idx, chunk, emb))
                stored += 1
            except Exception as e:
                logger.warning(f"研报 embedding 写入失败 (report_id={report_id}, idx={idx}): {e}")
            time.sleep(0.5)

        conn.commit()
        logger.info(f"研报 {report_id} 向量化完成: {stored}/{len(chunks)} chunks")
        return stored > 0
    except Exception as e:
        conn.rollback()
        logger.warning(f"研报向量化异常 (report_id={report_id}): {e}")
        return False
    finally:
        cur.close()
        conn.close()


def embed_reports_batch(limit: int = 50) -> int:
    """
    批量对未向量化的研报生成 embedding

    Returns:
        成功嵌入的研报数量
    """
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT rr.id, rr.title, rr.summary, rr.source
            FROM research.research_reports rr
            LEFT JOIN research.report_embeddings re ON re.report_id = rr.id
            WHERE re.id IS NULL
            ORDER BY rr.report_date DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        logger.info(f"待嵌入研报: {len(rows)} 条")
        success = 0
        for rid, title, summary, source in rows:
            text = f"{source}研报：{title}。{summary or ''}"
            if embed_research_report(rid, text):
                success += 1
        return success
    finally:
        cur.close()
        conn.close()


def search_similar_reports(query: str, top_k: int = 5, min_score: float = 0.5) -> list[dict]:
    """
    语义搜索相似研报
    """
    query_emb = get_embedding(query)
    if query_emb is None:
        return []

    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                re.report_id,
                rr.title,
                LEFT(re.content, 300),
                1 - (re.embedding <=> %s::vector) AS similarity,
                rr.report_date,
                rr.source,
                rr.rating
            FROM research.report_embeddings re
            JOIN research.research_reports rr ON rr.id = re.report_id
            WHERE 1 - (re.embedding <=> %s::vector) > %s
            ORDER BY re.embedding <=> %s::vector
            LIMIT %s
        """, (query_emb, query_emb, min_score, query_emb, top_k))

        results = []
        for row in cur.fetchall():
            results.append({
                "report_id": row[0],
                "title": row[1],
                "content_chunk": row[2],
                "similarity": round(float(row[3]), 4),
                "report_date": row[4].isoformat() if row[4] else None,
                "source": row[5],
                "rating": row[6],
            })
        return results
    except Exception as e:
        logger.warning(f"研报向量搜索失败: {e}")
        return []
    finally:
        conn.close()


# ─── 每日 Embedding 任务（供 APScheduler 调用） ───────────────────────────

def daily_embedding_job():
    """每日盘后运行：嵌入当日新闻"""
    logger.info("开始每日新闻向量化任务...")
    try:
        count = embed_news_articles(limit=100)
        logger.info(f"每日嵌入任务完成: {count} 条")
    except Exception as e:
        logger.error(f"每日嵌入任务失败: {e}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "init"

    if cmd == "init":
        init_vector_tables()
        print("向量表初始化完成")

    elif cmd == "embed":
        n = embed_news_articles(limit=50)
        print(f"嵌入完成: {n} 条")

    elif cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else "半导体芯片行情"
        results = search_similar_news(query, top_k=5)
        print(f"\n搜索: {query}")
        for r in results:
            print(f"  [{r['similarity']}] {r['title']}")

    elif cmd == "context":
        query = sys.argv[2] if len(sys.argv) > 2 else "半导体AI"
        print(get_news_context_for_prompt(query))
