#!/usr/bin/env python3
"""
run_analysis.py — Phase 1 MVP 核心脚本
一键运行完整分析链路：
  1. 读取持仓 (positions.csv)
  2. 采集行情 (akshare)
  3. 采集新闻 (多源)
  4. 数据校验
  5. 写入 PostgreSQL
  6. 脱敏处理
  7. 组装 Prompt
  8. 调用 LLM (DeepSeek / Ollama)
  9. 输出 Markdown 报告
"""

import os
import sys
import csv
import logging
from datetime import datetime, date
from pathlib import Path

# ── 路径设置 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

# ── 内部模块 ────────────────────────────────────────────────────────────────
from storage_factory import get_storage
from data_validator import (
    validate_quotes_data, validate_news_data, validate_positions_data,
    print_validation_report,
)
from data_sanitizer import (
    reset_mapping, sanitize_snapshot, desensitize_plan,
    print_sanitized_report,
)
from fetch_quotes import collect_quotes, fetch_fund_nav
from fetch_news import collect_news
from prompt_builder import build_analysis_prompt, build_tamf_summaries_for_prompt, simple_position_analysis
from llm_caller import estimate_cost, _parse_llm_response
from agent_interface import get_agent
from circuit_breaker import get_circuit_breaker, CircuitBreakerStatus
from pgcrypto_migration import get_credential

# ── 日志配置 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("invest_system")


# ─── 读取持仓数据 ──────────────────────────────────────────────────────────

def load_positions(csv_path: str) -> list[dict]:
    """从 PostgreSQL 加密持仓表读取，DB为空时降级 positions.csv"""
    # 优先从加密持仓表读取
    try:
        from pgcrypto_migration import load_positions_from_db
        db_positions = load_positions_from_db()
        if db_positions:
            return db_positions
    except Exception as e:
        logger.debug(f"DB 读取持仓失败，降级 CSV: {e}")

    # 降级：读 CSV
    positions = []
    if not os.path.exists(csv_path):
        logger.error(f"持仓文件不存在: {csv_path}")
        return positions

    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("code"):
                continue
            positions.append({
                "code": str(row.get("code", "")).zfill(6),
                "name": row.get("name", ""),
                "account": row.get("account", ""),
                "type": row.get("type", "stock"),  # stock / fund
                "shares": float(row.get("shares", 0)),
                "cost": float(row.get("cost", 0)),
                "market_value": float(row.get("market_value", 0)),
                "weight": float(row.get("weight", 0)),
            })

    logger.info(f"从 {csv_path} 读取 {len(positions)} 条持仓")
    return positions


def enrich_positions_with_quotes(positions: list[dict]) -> list[dict]:
    """
    采集实时行情并合并到持仓数据
    基金 -> 东方财富基金净值 API
    股票/ETF -> 新浪财经行情 API
    返回：合并后的持仓列表（含 close, change_pct）
    """
    if not positions:
        return []

    # 分离基金和股票/ETF
    fund_positions = [p for p in positions if p["type"] == "fund"]
    stock_positions = [p for p in positions if p["type"] != "fund"]

    # ── 基金净值 ──────────────────────────────────────────────────────────
    fund_quotes = []
    if fund_positions:
        fund_codes = [p["code"] for p in fund_positions]
        logger.info(f"采集 {len(fund_codes)} 只基金净值...")
        fund_quotes = fetch_fund_nav(fund_codes)
        logger.info(f"基金净值获取完成: {len(fund_quotes)} 只")

    # ── 股票/ETF 行情 ─────────────────────────────────────────────────────
    stock_quotes = []
    if stock_positions:
        stock_codes = []
        for pos in stock_positions:
            code = pos["code"]
            if code.startswith("15") or code.startswith("30") or code.startswith("00"):
                stock_codes.append(f"{code}.XSHE")
            elif code.startswith("5") or code.startswith("6") or \
                 code.startswith("4") or code.startswith("8"):
                stock_codes.append(f"{code}.XSHG")
            else:
                stock_codes.append(f"{code}.XSHE")
        logger.info(f"采集 {len(stock_codes)} 只股票/ETF 行情...")
        stock_quotes, _, _ = collect_quotes(stock_codes)
        logger.info(f"股票/ETF 行情获取完成: {len(stock_quotes)} 只")

    # 建立行情索引（基金用 .OF，股票用原始 code）
    quote_map = {}
    for q in fund_quotes:
        raw = q["ts_code"].split(".")[0]  # e.g. "002943"
        quote_map[f"FUND:{raw}"] = q
    for q in stock_quotes:
        raw = q["ts_code"].split(".")[0]
        quote_map[raw] = q

    # 合并到持仓
    enriched = []
    for pos in positions:
        code = pos["code"]
        if pos["type"] == "fund":
            q = quote_map.get(f"FUND:{code}")
        else:
            q = quote_map.get(code)

        if q and q.get("close", 0) > 0:
            pos["close"] = q["close"]
            pos["change_pct"] = q.get("change_pct", 0)
            pos["ts_code"] = q["ts_code"]
            pos["source"] = q.get("source", "unknown")
        else:
            # 无行情时用成本价估算
            pos["close"] = pos.get("cost", 0)
            pos["change_pct"] = 0
            pos["ts_code"] = f"{code}.OF" if pos["type"] == "fund" else f"{code}.XSHE"
            pos["source"] = "cost_estimate"
        enriched.append(pos)

    return enriched


# ─── 宏观日历（简化版）─────────────────────────────────────────────────────

def get_macro_calendar() -> list[dict]:
    """获取近期宏观日历（简化版，可扩展）"""
    # 目前返回空列表，后续可接入财联社宏观日历 API
    return []


# ─── 主流程 ────────────────────────────────────────────────────────────────

def run_analysis():
    print("\n" + "=" * 65)
    print("🚀 InvestPilot MVP — 个人投资分析系统")
    print("=" * 65)
    print(f"⏰ 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # ── Step 1: 读取持仓 ──────────────────────────────────────────────────
    print("📌 Step 1: 读取持仓数据...")
    positions_csv = os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv")
    positions = load_positions(positions_csv)
    if not positions:
        logger.error("无持仓数据，退出")
        return

    # 计算总市值
    total_mv = sum(p.get("market_value", 0) for p in positions)
    logger.info(f"总市值: ¥{total_mv:,.2f}")

    # ── Step 2: 采集实时行情 ───────────────────────────────────────────────
    print("\n📌 Step 2: 采集实时行情...")
    positions = enrich_positions_with_quotes(positions)

    # 提取所有 ts_code 列表（用于后续查询）
    [p.get("ts_code", f"{p['code']}.XSHE") for p in positions]

    # ── Step 3: 采集新闻 ──────────────────────────────────────────────────
    print("\n📌 Step 3: 采集新闻...")
    news = collect_news()

    # ── Step 4: 数据校验 ──────────────────────────────────────────────────
    print("\n📌 Step 4: 数据校验...")
    quotes_raw = [{"ts_code": p.get("ts_code", ""), "trade_date": date.today().strftime("%Y-%m-%d"),
                   "close": p.get("close", 0), "volume": 0,  # 不写入持仓数量(≠成交量)，留空让ON CONFLICT保留历史真实均量
                   "change_pct": p.get("change_pct", 0), "source": "run_analysis"}
                  for p in positions if p.get("close", 0) > 0]
    quotes_valid, quotes_err = validate_quotes_data(quotes_raw)
    positions_valid, positions_err = validate_positions_data(positions)
    news_valid, news_err = validate_news_data(news)

    print_validation_report(quotes_valid, quotes_err,
                           news_valid, news_err,
                           positions_valid, positions_err)

    # ── Step 5: 写入 PostgreSQL ───────────────────────────────────────────
    print("\n📌 Step 5: 写入数据库...")
    storage = get_storage()
    try:
        rows_q = storage.write_quotes(quotes_valid)
        rows_n = storage.write_news(news_valid)
        storage.write_audit("MVP_ANALYSIS_RUN", "SYSTEM",
                             detail={"positions": len(positions_valid),
                                     "quotes": rows_q, "news": rows_n})
        print(f"  ✅ 写入: {rows_q} 条行情, {rows_n} 条新闻")
    except Exception as e:
        logger.warning(f"数据库写入异常（不影响后续）: {e}")

    # ── Step 5.5: 熔断检查 ──────────────────────────────────────────
    print("\n📌 Step 5.5: 熔断检查...")
    try:
        cb = get_circuit_breaker()
        # quotes 包含今日行情，含 change_pct
        # indices 已在行情采集中获取（沪深300/上证）
        cb_status = cb.check(quotes=quotes_valid, indices=[])
        print(f"  熔断状态: {cb.status}")
        if cb.status != CircuitBreakerStatus.NORMAL:
            print(f"  ⚠️ {cb.reason}")
        buy_allowed, buy_reason = cb.is_buy_allowed()
        position_limit = cb.get_position_limit()
        print(f"  买入许可: {'✅' if buy_allowed else '❌'} {buy_reason}")
        print(f"  当前仓位上限: {position_limit}%")
    except Exception as e:
        logger.warning(f"熔断检查异常: {e}")
        cb_status = CircuitBreakerStatus.NORMAL
        buy_allowed = True
        buy_reason = "熔断检查失败，默认允许交易"
        position_limit = 100.0

    # ── Step 6: 脱敏处理 ──────────────────────────────────────────────────
    print("\n📌 Step 6: 脱敏处理...")
    reset_mapping()
    # 用 enriched positions（含 close）重新计算
    enriched_for_sanit = []
    for p in positions_valid:
        if p.get("close", 0) > 0:
            enriched_for_sanit.append(p)
    sanitized, id_mapping = sanitize_snapshot(total_mv, enriched_for_sanit)
    print_sanitized_report(sanitized, total_mv)

    # ── Step 6.4: 构建 TAMF 摘要 ───────────────────────────────────────────
    print("\n📌 Step 6.4: 读取持仓TAMF记忆文件...")
    try:
        tamf_summaries = build_tamf_summaries_for_prompt(positions, max_chars_per_stock=400)
        print(f"  TAMF摘要: {len(tamf_summaries)} 只标的已读取")
    except Exception as e:
        logger.warning(f"TAMF摘要构建异常: {e}")
        tamf_summaries = []

    # ── Step 6.5: 采集持仓相关研报 ────────────────────────────────────────
    print("\n📌 Step 6.5: 采集持仓相关研报...")
    position_codes = []
    pos_reports = []
    try:
        from fetch_reports import collect_reports_for_positions
        # 提取持仓股票代码（纯数字）
        position_codes = []
        for p in positions:
            code = (p.get("code") or p.get("ts_code") or "").strip()
            # 去掉 .SH/.SZ/.XSHE 后缀
            code = code.split(".")[0]
            if code:
                position_codes.append(code)
        if position_codes:
            # 持仓相关研报（近7天，每个股最多3条）
            pos_reports = collect_reports_for_positions(position_codes)
            print(f"  持仓相关研报: {len(pos_reports)} 份已入库")
    except Exception as e:
        logger.warning(f"研报采集异常（不影响分析）: {e}")

    # ── Step 6.5: 持仓个股财务数据 ──────────────────────────────────────
    def _get_position_financials(codes: list[str]) -> list[dict]:
        """获取持仓个股最新财务数据（每只股最新2期）"""
        try:
            from fetch_financial import get_latest_financial
            results = []
            for code in codes:
                records = get_latest_financial(code, n=2)
                for r in records:
                    r["ts_code"] = code
                    results.append(r)
            return results
        except Exception as e:
            logger.warning(f"获取持仓财务数据异常: {e}")
            return []

    # ── Step 6.6: 国际投行研究 ───────────────────────────────────────────
    print("\n📌 Step 6.6: 采集国际投行研究...")
    intl_research = []
    try:
        from fetch_international_research import collect_international_research
        intl_research = collect_international_research(days=3)
        logger.info(f"国际投行研究: {len(intl_research)} 条, "
                    f"投行相关: {sum(1 for a in intl_research if a.get('is_bank_related'))} 条")
        # 持久化到 DB
        if intl_research:
            saved = storage.write_international_research(intl_research)
            logger.info(f"国际投行研究写入 DB: {saved} 条")
    except Exception as e:
        logger.warning(f"国际投行研究采集异常: {e}")

    # ── Step 6.7: 持仓股重要公告（从 DB 读取近7天）──────────────────────
    print("\n📌 Step 6.7: 读取持仓股重要公告...")
    announcements_for_prompt = []
    try:
        import psycopg2
        ann_conn = psycopg2.connect(
            host='localhost', port=5432, database='investpilot',
            user='invest_admin', password=get_credential("DB_PASSWORD"))
        ann_cur = ann_conn.cursor()
        ann_cur.execute("""
            SELECT ts_code, notice_date::text, title, ann_type
            FROM research.announcements
            WHERE notice_date >= CURRENT_DATE - INTERVAL '7 days'
              AND ann_type IN (
                  '年度报告', '半年度报告', '季报', '业绩预告',
                  '董事会决议', '股东大会', '分红公告', '回购公告',
                  '增持公告', '减持公告', '股权激励', '审计报告',
                  '监管措施', '退市风险'
              )
            ORDER BY notice_date DESC
            LIMIT 20
        """)
        for row in ann_cur.fetchall():
            ts_code, notice_date, title, ann_type = row
            announcements_for_prompt.append({
                'ts_code': ts_code,
                'notice_date': notice_date,
                'title': title,
                'ann_type': ann_type,
                'is_major': True,  # 上述类型均为重大公告
            })
        ann_cur.close()
        ann_conn.close()
        logger.info(f"持仓重要公告: {len(announcements_for_prompt)} 条")
    except Exception as e:
        logger.warning(f"读取公告失败: {e}")

    # ── Step 7: 组装 Prompt 并调用 LLM ────────────────────────────────────
    print("\n📌 Step 7: 生成分析（调用 LLM）...")

    # 用户画像（默认，可后续扩展）
    user_profile = {
        "risk_tolerance": "medium",
        "max_single_position_pct": 20.0,
        "max_sector_position_pct": 30.0,
        "max_daily_loss_pct": 5.0,
        "investment_goal": "资产稳健增值",
    }

    prompt = build_analysis_prompt(
        user_profile=user_profile,
        sanitized_positions=sanitized,
        total_mv=total_mv,
        index_history=storage.get_index_history("000300.XSHG", 5) +
                     storage.get_index_history("000001.XSHG", 5),
        sector_flows=[],
        recent_news=news_valid[:15],
        macro_calendar=get_macro_calendar(),
        research_reports=pos_reports[:10],
        financial_data=_get_position_financials(position_codes),
        international_research=intl_research[:6],
        announcements=announcements_for_prompt,
        tamf_summaries=tamf_summaries,
    )

    from prompt_builder import count_tokens, truncate_prompt, MAX_TOKENS
    token_count = count_tokens(prompt)
    if token_count > MAX_TOKENS:
        logger.warning(f"Prompt 超长: ~{token_count} tokens，开始压缩...")
        prompt = truncate_prompt(prompt, max_tokens=MAX_TOKENS)
        logger.info(f"压缩后: ~{count_tokens(prompt)} tokens")

    # 成本估算
    cost_info = estimate_cost(prompt)
    logger.info(f"预估成本: ${cost_info['estimated_cost_usd']} "
                f"(约 ¥{cost_info['estimated_cost_cny']})")

    # ── 熔断状态 → LLM 提示 ──────────────────────────────────────────
    circuit_warning = ""
    if 'cb_status' in dir() and cb_status != CircuitBreakerStatus.NORMAL:
        circuit_warning = (
            f"\n【熔断警告】{cb.reason}\n"
            f"当前仓位上限: {position_limit}%\n"
            f"买入状态: {'禁止' if not buy_allowed else '受限'}\n"
        )
        logger.warning(f"🚨 熔断触发: {cb.reason}")

    # ── Step 7: 模型路由 + LLM 调用 ────────────────────────────────────────
    # RouterAgent 根据 prompt 关键词自动路由：
    #   策略/宏观/行业/基本面/决策类 → DeepSeek（白名单强制）
    #   持仓查询/行情/技术指标/计算类 → Ollama（白名单强制）
    #   未命中 → LLM 判断（默认 Ollama）
    agent = get_agent()
    logger.info(f"[路由] 当前 Agent: {type(agent).__name__}")
    system_msg = (
        "你是一名专业量化投资顾问。" + circuit_warning
    )
    raw = agent.chat(prompt, system=system_msg)
    result = _parse_llm_response(raw.get("content", "")) if not raw.get("error") else {}
    if raw.get("error"):
        result["error"] = raw["error"]

    # ── Step 7.5: 质量评估（reflection_engine 联动）───────────────────────
    print("\n📌 Step 7.5: 分析质量评估...")
    try:
        from reflection_engine import evaluate_analysis_quality, log_quality_to_audit
        quality = evaluate_analysis_quality(result)
        print(f"  质量评分: {quality['quality_score']}/100 ({quality['quality_level']})")
        if quality["warnings"]:
            for w in quality["warnings"]:
                print(f"  ⚠️ {w}")
        if quality["flagged"]:
            print("  🚨 低质量输出已标记，建议人工审核")
        try:
            log_quality_to_audit(result, quality, agent_type=type(agent).__name__)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"质量评估异常: {e}")
        quality = {"quality_score": 0, "quality_level": "unknown", "warnings": [str(e)], "flagged": False}

    # ── Step 8: 还原脱敏并输出报告 ───────────────────────────────────────
    print("\n" + "=" * 65)
    print("📊 持仓分析报告")
    print("=" * 65)

    # 先打印本地分析（始终可用）
    print(simple_position_analysis(positions_valid, total_mv))

    # 打印 LLM 分析结果
    if result.get("error"):
        print(f"⚠️ LLM 分析暂时不可用: {result.get('error')}\n")
    else:
        # 还原匿名ID
        if id_mapping:
            result = desensitize_plan(result, id_mapping)

        print("📋 操作计划:")
        print("-" * 50)
        for plan in result.get("plans", []):
            code = plan.get("ts_code", "未知")
            name = plan.get("name", "")
            action = {"buy": "🟢增持", "sell": "🔴减持", "hold": "⚪持有",
                      "rebalance": "🔵调仓"}.get(plan.get("action", "hold"), "⚪持有")
            pos = plan.get("position_pct", 0)
            price = plan.get("limit_price")
            reason = plan.get("reason", "")
            price_str = f"限价 ¥{price:.2f}" if price else "市价"
            print(f"  {name}({code}) | {action} {pos}% | {price_str}")
            print(f"    逻辑: {reason}")
            print()

        print("⚠️ 风险提示:")
        for risk in result.get("risks", []):
            print(f"  • {risk}")

        print("\n📈 市场展望:")
        print(f"  {result.get('market_outlook', '暂无')}")

        confidence = result.get("confidence_level", "unknown")
        conf_icon = {"high": "🟢高", "medium": "🟡中", "low": "🔴低"}.get(confidence, "⚪未知")
        print(f"\n🔍 置信度: {conf_icon}")

    print("\n" + "=" * 65)
    print(f"✅ 分析完成 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # 写审计日志 + 分析运行记录
    try:
        plans_list = result.get("plans", [])
        detail = {
            "positions_count": len(positions_valid),
            "quotes_count": len(quotes_valid),
            "news_count": len(news_valid),
            "confidence": result.get("confidence_level", "unknown"),
            "plans_count": len(plans_list),
            "plans": plans_list,  # 完整计划正文
        }
        storage.write_audit(
            "ANALYSIS_COMPLETE", "SYSTEM",
            detail=detail,
            result="SUCCESS" if not result.get("error") else "PARTIAL"
        )

        # 同时写入 analysis.analysis_runs（供计划审核页使用）
        try:
            import uuid as _uuid
            run_id = str(_uuid.uuid4())
            from storage_factory import get_pg_connection
            pg_conn = get_pg_connection()
            if pg_conn:
                import json as _json
                cur2 = pg_conn.cursor()
                cur2.execute("""
                    INSERT INTO analysis.analysis_runs (run_id, completed_at, detail, plans, confidence)
                    VALUES (%s, NOW(), %s, %s, %s)
                    ON CONFLICT (run_id) DO UPDATE SET
                        detail = EXCLUDED.detail,
                        plans = EXCLUDED.plans,
                        completed_at = EXCLUDED.completed_at
                """, (run_id, _json.dumps(detail), _json.dumps(plans_list), result.get("confidence_level", "unknown")))
                pg_conn.commit()
                cur2.close()
                pg_conn.close()
        except Exception as e:
            print(f"  [WARN] 写入 analysis_runs 失败: {e}")
    except Exception:
        pass

    storage.close()


if __name__ == "__main__":
    run_analysis()
