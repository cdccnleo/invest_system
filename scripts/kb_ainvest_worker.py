"""
kb_ainvest_worker.py — AInvest 知识库工作引擎

核心职责:
  1. 调度文件扫描 → 解析 → 写入 PostgreSQL 全流程
  2. 向量嵌入生成（复用 embedding_service）
  3. 触发 TAMF 增量更新（当报告涉及持仓标的时）
  4. 记录时间线事件
  5. 写入扫描审计日志

调用关系:
  schedule_runner.py (APScheduler) → kb_ainvest_job()
      → scan_reports_directory()
      → parse_single_report() × N
      → upsert_parsed_report()
      → generate_report_embeddings()
      → update_stock_kb_links()
      → trigger_tamf_updates()
      → write_scan_audit()

修正记录（基于审核确认文档）:
  - M3: 使用 embedding_service.chunk_text 公开别名（或内联实现）
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

logger = logging.getLogger("invest_system.kb_ainvest")

# ── 导入解析器 ─────────────────────────────────────────────
from ainvest_report_parser import (
    scan_reports_directory,
    parse_single_report,
    AINVEST_REPORTS_DIR,
)

# ── 哈希状态持久化 ─────────────────────────────────────────
_KNOWN_HASHES_CACHE: dict[str, str] = {}
_HASH_CACHE_FILE = Path(__file__).parent.parent / "data" / "ainvest_kb_hashes.json"


def _load_known_hashes() -> dict[str, str]:
    """从文件加载已知哈希（持久化状态）"""
    global _KNOWN_HASHES_CACHE
    if _KNOWN_HASHES_CACHE:
        return _KNOWN_HASHES_CACHE
    
    if _HASH_CACHE_FILE.exists():
        try:
            with open(_HASH_CACHE_FILE, "r", encoding="utf-8") as f:
                _KNOWN_HASHES_CACHE = json.load(f)
        except Exception as e:
            logger.warning(f"加载哈希缓存失败: {e}")
    
    return _KNOWN_HASHES_CACHE


def _save_known_hashes(hashes: dict[str, str]):
    """保存哈希到文件"""
    global _KNOWN_HASHES_CACHE
    _KNOWN_HASHES_CACHE = hashes
    _HASH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_HASH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(hashes, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存哈希缓存失败: {e}")


# ── 数据库连接 ─────────────────────────────────────────────

def _get_db_conn():
    """获取 PostgreSQL 连接，不可用时返回 None"""
    from storage_factory import get_pg_connection
    conn = get_pg_connection()
    if conn is None:
        logger.warning("PostgreSQL 不可用，跳过知识库写入")
    return conn


# ── 文本分块（内联实现，避免依赖 _chunk_text 私有函数）────

def _chunk_text(text: str, max_chars: int = 500) -> list[str]:
    """文本分块：按句号/换行切分，合并到 max_chars"""
    sentences = text.replace("\n", "。").split("。")
    chunks, current = [], ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(current) + len(s) < max_chars:
            current += ("。" if current else "") + s
        else:
            if current:
                chunks.append(current)
            current = s
    if current:
        chunks.append(current)
    return chunks


# ── 数据库写入操作 ─────────────────────────────────────────

def _get_or_create_parsed_report(conn, cur, file_path: str) -> tuple:
    """查询是否已解析过该文件，返回 (report_id, file_hash) 或 (None, None)"""
    cur.execute(
        "SELECT id, file_hash FROM ainvest_kb.parsed_reports WHERE file_path = %s",
        (file_path,)
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def upsert_parsed_report(parsed: dict) -> Optional[int]:
    """
    写入/更新 parsed_reports 表。
    返回 report_id 或 None（写入失败）。
    """
    conn = _get_db_conn()
    if conn is None:
        return None
    
    try:
        cur = conn.cursor()
        
        existing_id, existing_hash = _get_or_create_parsed_report(
            conn, cur, parsed["file_path"]
        )
        
        if existing_id and existing_hash == parsed["file_hash"]:
            return existing_id  # 文件无变化，跳过
        
        related_codes = parsed.get("related_codes", []) or []
        event_tags = parsed.get("event_tags", []) or []
        investment_signals = json.dumps(
            parsed.get("investment_signals", []), ensure_ascii=False
        )
        key_judgments = json.dumps(
            parsed.get("key_judgments", []), ensure_ascii=False
        )
        operation_actions = json.dumps(
            parsed.get("operation_actions", []), ensure_ascii=False
        )
        
        raw_text = (parsed.get("raw_text") or "")[:50000]
        
        if existing_id:
            cur.execute("""
                UPDATE ainvest_kb.parsed_reports
                SET file_hash = %s, title = %s, summary = %s,
                    related_codes = %s, event_tags = %s,
                    investment_signals = %s::jsonb, key_judgments = %s::jsonb,
                    risk_assessment = %s, operation_actions = %s::jsonb,
                    primary_stock_code = %s, confidence_score = %s,
                    raw_text = %s, file_modified_at = %s::timestamptz,
                    parsed_at = NOW(), version = version + 1
                WHERE id = %s
            """, (
                parsed["file_hash"], parsed["title"], parsed.get("summary"),
                related_codes, event_tags,
                investment_signals, key_judgments,
                parsed.get("risk_assessment"), operation_actions,
                parsed.get("primary_stock_code"), parsed.get("confidence_score", 0.5),
                raw_text, parsed["file_modified_at"], existing_id,
            ))
            report_id = existing_id
        else:
            cur.execute("""
                INSERT INTO ainvest_kb.parsed_reports
                    (file_path, file_hash, report_type, title, report_date,
                     file_modified_at, summary, related_codes, event_tags,
                     investment_signals, key_judgments, risk_assessment,
                     operation_actions, primary_stock_code, confidence_score, raw_text)
                VALUES (%s, %s, %s, %s, %s, %s::timestamptz,
                        %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s,
                        %s::jsonb, %s, %s, %s)
                RETURNING id
            """, (
                parsed["file_path"], parsed["file_hash"], parsed["report_type"],
                parsed["title"], parsed.get("report_date"),
                parsed["file_modified_at"], parsed.get("summary"),
                related_codes, event_tags,
                investment_signals, key_judgments,
                parsed.get("risk_assessment"), operation_actions,
                parsed.get("primary_stock_code"), parsed.get("confidence_score", 0.5),
                raw_text,
            ))
            report_id = cur.fetchone()[0]
        
        conn.commit()
        logger.info(
            f"{'更新' if existing_id else '新增'}知识库报告: id={report_id}, {parsed['title'][:40]}"
        )
        return report_id
    
    except Exception as e:
        conn.rollback()
        logger.error(f"写入知识库报告失败: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def generate_report_embeddings(report_id: int, text: str):
    """
    为报告生成向量嵌入（复用 embedding_service）。
    将报告全文分块后调用 Ollama 生成 768 维向量。
    """
    try:
        from embedding_service import get_embedding
    except ImportError:
        logger.warning("embedding_service 不可用，跳过向量嵌入")
        return
    
    chunks = _chunk_text(text[:10000], max_chars=500)
    
    conn = _get_db_conn()
    if conn is None:
        return
    
    try:
        cur = conn.cursor()
        
        # 清除旧嵌入
        cur.execute(
            "DELETE FROM ainvest_kb.report_embeddings WHERE report_id = %s",
            (report_id,)
        )
        
        embedded = 0
        for idx, chunk in enumerate(chunks):
            emb = get_embedding(chunk)
            if emb is None:
                continue
            try:
                cur.execute("""
                    INSERT INTO ainvest_kb.report_embeddings
                        (report_id, chunk_index, content_chunk, embedding)
                    VALUES (%s, %s, %s, %s::vector)
                """, (report_id, idx, chunk, emb))
                embedded += 1
            except Exception as e:
                logger.warning(
                    f"报告 embedding 写入失败 (report_id={report_id}, idx={idx}): {e}"
                )
        
        conn.commit()
        logger.info(f"报告 {report_id} 嵌入完成: {embedded}/{len(chunks)} chunks")
    
    except Exception as e:
        conn.rollback()
        logger.error(f"报告 embedding 生成失败: {e}")
    finally:
        cur.close()
        conn.close()


def update_stock_kb_links(report_id: int, related_codes: list[str]):
    """
    建立报告与持仓标的的关联。
    对于报告中提到的股票代码，检查是否在系统持仓中，若在则建立关联。
    """
    if not related_codes:
        return
    
    conn = _get_db_conn()
    if conn is None:
        return
    
    try:
        cur = conn.cursor()
        
        for code in related_codes:
            # 检查是否在持仓中
            cur.execute(
                "SELECT 1 FROM memory.target_memory_files WHERE ts_code = %s",
                (code,)
            )
            if cur.fetchone():
                cur.execute("""
                    INSERT INTO ainvest_kb.stock_kb_links
                        (ts_code, report_id, relevance_score)
                    VALUES (%s, %s, 0.7)
                    ON CONFLICT (ts_code, report_id)
                    DO UPDATE SET
                        relevance_score = GREATEST(
                            ainvest_kb.stock_kb_links.relevance_score, 0.7
                        ),
                        last_accessed = NOW(),
                        accessed_count = ainvest_kb.stock_kb_links.accessed_count + 1
                """, (code, report_id))
        
        conn.commit()
        logger.info(f"建立标的关联: 报告 {report_id} → {len(related_codes)} 个标的")
    
    except Exception as e:
        conn.rollback()
        logger.warning(f"建立标的关联失败: {e}")
    finally:
        cur.close()
        conn.close()


def trigger_tamf_updates_for_report(report_id: int, related_codes: list[str]):
    """
    当新报告涉及持仓标的时，记录 TAMF 时间线事件。
    后续 TAMF 增量更新会检测到这些事件并触发对应章节更新。
    """
    if not related_codes:
        return
    
    conn = _get_db_conn()
    if conn is None:
        return
    
    try:
        cur = conn.cursor()
        
        # 查询报告类型
        cur.execute(
            "SELECT report_type FROM ainvest_kb.parsed_reports WHERE id = %s",
            (report_id,)
        )
        row = cur.fetchone()
        report_type = row[0] if row else "unknown"
        
        # 只对持仓标的记录事件
        for code in related_codes:
            cur.execute(
                "SELECT 1 FROM memory.target_memory_files WHERE ts_code = %s",
                (code,)
            )
            if not cur.fetchone():
                continue
            
            event_title = f"知识库更新: {report_type}报告"
            cur.execute("""
                INSERT INTO memory.target_timeline_events
                    (ts_code, event_time, event_type, event_source, severity,
                     title, description)
                VALUES (%s, NOW(), 'KNOWLEDGE_UPDATE', 'AINVEST_KB',
                        'INFO', %s, 'AInvest报告触发知识库更新')
            """, (code, event_title))
        
        conn.commit()
        logger.info(f"触发 TAMF 时间线事件: 报告 {report_id} → {len(related_codes)} 个标的")
    
    except Exception as e:
        conn.rollback()
        logger.warning(f"触发 TAMF 更新失败: {e}")
    finally:
        cur.close()
        conn.close()


def write_scan_audit(audit_result: dict):
    """写入扫描审计日志"""
    conn = _get_db_conn()
    if conn is None:
        return
    
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ainvest_kb.scan_audit
                (scan_start, scan_end, total_files, new_files, changed_files,
                 unchanged_files, parsed_ok, parsed_failed, errors)
            VALUES (%s::timestamptz, NOW(), %s, %s, %s, %s, %s, %s, %s::jsonb)
        """, (
            audit_result.get("scan_start", datetime.now().isoformat()),
            len(audit_result.get("all_files", [])),
            len(audit_result.get("new_files", [])),
            len(audit_result.get("changed_files", [])),
            len(audit_result.get("unchanged_files", [])),
            audit_result.get("parsed_ok", 0),
            audit_result.get("parsed_failed", 0),
            json.dumps(audit_result.get("errors", [])),
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"写入扫描审计失败: {e}")
    finally:
        cur.close()
        conn.close()


# ── 主工作流 ───────────────────────────────────────────────

def process_ainvest_reports() -> dict:
    """
    主工作流：扫描 → 解析 → 写入 → 嵌入 → 关联 → 审计
    
    Returns:
        处理结果统计 dict
    """
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info("AInvest 知识库更新工作流启动")
    
    known_hashes = _load_known_hashes()
    
    # Step 1: 扫描目录
    scan_result = scan_reports_directory(
        reports_dir=AINVEST_REPORTS_DIR,
        known_hashes=known_hashes,
    )
    
    to_process = scan_result["new_files"] + scan_result["changed_files"]
    
    if not to_process:
        logger.info("无新增或变更报告，跳过处理")
        return {"status": "skipped", "reason": "no_new_files"}
    
    logger.info(
        f"待处理报告: {len(to_process)} 份 "
        f"(新增{len(scan_result['new_files'])} + 变更{len(scan_result['changed_files'])})"
    )
    
    # Step 2: 逐份解析
    parsed_ok = 0
    parsed_failed = 0
    errors = []
    updated_hashes = dict(known_hashes)
    
    for filepath_str in to_process:
        filepath = Path(filepath_str)
        
        # 解析
        parsed = parse_single_report(filepath)
        if parsed is None:
            parsed_failed += 1
            errors.append(f"解析失败: {filepath_str}")
            continue
        
        # 写入数据库
        report_id = upsert_parsed_report(parsed)
        if report_id is None:
            parsed_failed += 1
            errors.append(f"写入失败: {filepath_str}")
            continue
        
        # 生成向量嵌入
        raw_text = parsed.get("raw_text", "")
        if raw_text:
            generate_report_embeddings(report_id, raw_text)
        
        # 建立标的关联
        related_codes = parsed.get("related_codes", [])
        if related_codes:
            update_stock_kb_links(report_id, related_codes)
        
        # 触发 TAMF 时间线事件（events/trackers/deep-analysis 类型）
        if related_codes and parsed["report_type"] in ("events", "trackers", "deep-analysis"):
            trigger_tamf_updates_for_report(report_id, related_codes)
        
        parsed_ok += 1
        updated_hashes[filepath_str] = parsed["file_hash"]
    
    # Step 3: 保存哈希状态
    _save_known_hashes(updated_hashes)
    
    # Step 4: 写入审计日志
    audit_result = {
        "scan_start": start_time.isoformat(),
        "all_files": scan_result["all_files"],
        "new_files": scan_result["new_files"],
        "changed_files": scan_result["changed_files"],
        "unchanged_files": scan_result["unchanged_files"],
        "parsed_ok": parsed_ok,
        "parsed_failed": parsed_failed,
        "errors": errors,
    }
    write_scan_audit(audit_result)
    
    # Step 5: 发送通知
    elapsed = (datetime.now() - start_time).total_seconds()
    summary = (
        f"AInvest 知识库更新完成 ⏱ {elapsed:.0f}s\n"
        f"📄 扫描 {len(scan_result['all_files'])} 份\n"
        f"✅ 新增解析 {len(scan_result['new_files'])} 份\n"
        f"🔄 变更更新 {len(scan_result['changed_files'])} 份\n"
        f"📝 成功 {parsed_ok} / 失败 {parsed_failed}"
    )
    
    try:
        from notification import send_notification
        send_notification("📚 AInvest 知识库更新", summary)
    except Exception as e:
        logger.warning(f"发送通知失败: {e}")
    
    logger.info(summary)
    logger.info("=" * 60)
    
    return {
        "status": "completed",
        "total_scanned": len(scan_result["all_files"]),
        "new": len(scan_result["new_files"]),
        "changed": len(scan_result["changed_files"]),
        "parsed_ok": parsed_ok,
        "parsed_failed": parsed_failed,
        "elapsed_seconds": elapsed,
    }