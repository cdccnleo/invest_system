"""
prompt_builder.py — 增强版 Prompt 组装器
将上下文数据组装为 DeepSeek Prompt，发送给 LLM 生成分析
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
TAMF_DIR = PROJECT_ROOT / "data" / "target_memories"


def build_tamf_summaries_for_prompt(
    positions: list[dict],
    max_chars_per_stock: int = 400
) -> list[dict]:
    """
    为持仓列表构建 TAMF 摘要，供 prompt_builder 注入。
    每个持仓标的上读取对应的 TAMF 文件，提取关键段落：
      - 技术面简评（Agent段落）
      - 消息面判断（Agent段落）
      - 监控状态
    返回: [{anon_id, status_emoji, tech_summary, news_summary, monitoring_summary}]
    """
    if not TAMF_DIR.exists():
        return []

    summaries = []
    for pos in positions:
        code = str(pos.get("code", "")).zfill(6)
        anon = pos.get("anon_id", code)
        name = pos.get("name", code)
        status_emoji = "🟢"  # 持有中

        tamf_path = TAMF_DIR / f"{code}.md"
        if not tamf_path.exists():
            summaries.append({
                "anon_id": anon,
                "status_emoji": "⚪",
                "tech_summary": "无TAMF记录",
                "news_summary": "无TAMF记录",
                "monitoring_summary": "",
            })
            continue

        content = tamf_path.read_text(encoding="utf-8")

        # 提取技术面Agent段落（### Agent 技术面简评 后的代码块）
        tech_summary = _extract_agent_block(content, "技术面简评")
        # 提取消息面Agent段落
        news_summary = _extract_agent_block(content, "消息面综合判断")

        # 提取监控状态（第七章）
        mon = _extract_section(content, "七、跟踪状态")
        monitoring_summary = _extract_table_rows(mon) if mon else ""

        # 截断防超限
        tech_summary = tech_summary[:max_chars_per_stock]
        news_summary = news_summary[:max_chars_per_stock]

        summaries.append({
            "anon_id": anon,
            "name": name,
            "status_emoji": status_emoji,
            "tech_summary": tech_summary or "⚠️ 无技术面分析",
            "news_summary": news_summary or "⚠️ 无消息面分析",
            "monitoring_summary": monitoring_summary[:200] if monitoring_summary else "",
        })

    return summaries


def _extract_agent_block(content: str, section_heading: str) -> str:
    """从TAMF提取Agent生成的段落内容"""
    # 匹配 "### Agent {section_heading}\n```\n内容\n```"
    pattern = re.compile(
        rf"### Agent {re.escape(section_heading)}\n```\s*(.*?)\s*```",
        re.DOTALL
    )
    match = pattern.search(content)
    return match.group(1).strip() if match else ""


def _extract_section(content: str, section_heading: str) -> str:
    """提取指定章节的完整内容"""
    # 匹配从 ### {heading} 到下一个 ---

    escaped = re.escape(section_heading)
    pattern = re.compile(
        rf"(?:^|\n)(## {escaped}.*?)((?=^## |\n## |\Z))",
        re.DOTALL | re.MULTILINE
    )
    match = pattern.search(content)
    return match.group(1) if match else ""


def _extract_table_rows(section_text: str) -> str:
    """从markdown表格中提取关键行（状态为🟡/🔴的行）"""
    rows = []
    for line in section_text.split("\n"):
        if "| 🟡" in line or "| 🔴" in line:
            # 提取标的+状态
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                rows.append(f"{parts[1]}: {parts[2]}")
    return "; ".join(rows)



SYSTEM_PROMPT = """你是一名拥有15年经验的A股量化投资经理，精通技术分析、资金流向研判和行业轮动策略。

## 重要约束
1. 所有数据均为脱敏处理：金额为百分比，代码为匿名ID
2. 每条操作建议必须附带2-3句逻辑说明
3. 策略生成需考虑大盘趋势、行业轮动、资金流向
4. 输出严格JSON格式，不可输出JSON以外的任何内容

## 响应格式
```json
{
  "plans": [
    {
      "anon_id": "STK_001",
      "action": "buy",       // buy/sell/hold/rebalance
      "position_pct": 5.0,   // 建议仓位变化（百分比）
      "limit_price": 12.80,  // 买入限价（仅buy有效，卖出/rebalance填null）
      "exit_price": 15.50,   // 卖出目标价（仅sell/rebalance有效，buy填null）
      "reason": "..."        // 2-3句逻辑
    }
  ],
  "risks": ["风险点1", "风险点2", "风险点3"],
  "market_outlook": "市场展望（2-3句）",
  "confidence_level": "high|medium|low"
}
```
"""


def build_analysis_prompt(
    user_profile: dict,
    sanitized_positions: list[dict],
    total_mv: float,
    index_history: list[dict],
    sector_flows: list[dict],
    recent_news: list[dict],
    macro_calendar: list[dict],
    research_reports: list[dict] = None,
    financial_data: list[dict] = None,
    international_research: list[dict] = None,
    announcements: list[dict] = None,  # 持仓股公告
    tamf_summaries: list[dict] = None,  # TAMF文件摘要（新增）
) -> str:
    """
    组装完整的分析 Prompt
    所有数据已经是脱敏后的格式
    """

    # ── 用户画像 ────────────────────────────────────────────────────────────
    profile_section = f"""## 用户画像
风险偏好：{user_profile.get('risk_tolerance', 'medium')}
单股仓位上限：{user_profile.get('max_single_position_pct', 20)}%
行业仓位上限：{user_profile.get('max_sector_position_pct', 30)}%
日最大亏损容忍：{user_profile.get('max_daily_loss_pct', 5)}%
投资目标：{user_profile.get('investment_goal', '资产稳健增值')}
"""

    # ── 持仓快照 ────────────────────────────────────────────────────────────
    positions_lines = []
    for pos in sanitized_positions:
        positions_lines.append(
            f"| {pos['anon_id']} | {pos['name']} | "
            f"{pos.get('cost_pct', 0):.1f}% | {pos.get('weight_pct', 0):.1f}% | "
            f"{pos['pnl_dir']} ({pos.get('pnl_pct', 0):+.1f}%) |"
        )
    positions_section = """## 当前持仓快照（总市值占比）
匿名ID | 名称 | 成本占比% | 仓位% | 盈亏
""" + "\n".join(positions_lines)

    # ── 大盘指数趋势 ─────────────────────────────────────────────────────────
    index_lines = []
    for idx in index_history[:5]:
        idx_name = idx.get("index_code", "")
        if "000300" in idx_name:
            idx_name = "沪深300"
        elif "000001" in idx_name:
            idx_name = "上证指数"
        elif "399006" in idx_name:
            idx_name = "创业板指"
        change = idx.get("change_pct", 0)
        arrow = "📈" if change >= 0 else "📉"
        index_lines.append(f"{idx_name} {arrow} {change:+.2f}%")
    index_section = "## 近5日大盘趋势\n" + "\n".join(index_lines) if index_lines else "## 近5日大盘趋势\n暂无数据"

    # ── 板块资金流向 ────────────────────────────────────────────────────────
    sector_lines = []
    for sf in sector_flows[:10]:
        name = sf.get("sector_name", "")
        flow = sf.get("net_flow", 0)
        pct = sf.get("net_flow_pct", 0)
        arrow = "↑" if flow >= 0 else "↓"
        sector_lines.append(f"{name} {arrow} {abs(flow/1e8):.1f}亿 ({pct:+.1f}%)")
    sector_section = "## 行业板块资金流向（当日）\n" + "\n".join(sector_lines) if sector_lines else "## 行业板块资金流向\n暂无数据"

    # ── 近期重要新闻 ────────────────────────────────────────────────────────
    news_lines = []
    for n in recent_news[:15]:
        sev_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}.get(n.get("severity", "LOW"), "🔵")
        news_lines.append(f"{sev_icon} {n['title'][:50]}")
    news_section = "## 近15条重要新闻（按严重程度排序）\n" + "\n".join(news_lines)

    # ── 近期重要新闻（向量检索补充）───────────────────────────────────────
    news_vector_section = ""
    try:
        from embedding_service import get_news_context_for_prompt as _get_news_ctx
        pos_names = [p.get("name", "") for p in (sanitized_positions or [])[:5]]
        _query = " ".join(pos_names) + " 市场分析 投资策略"
        news_vector_section = _get_news_ctx(_query, top_k=5)
        if news_vector_section:
            news_vector_section = f"\n\n## 近7日高度相关新闻（向量检索）\n{news_vector_section}"
    except Exception:
        pass

    # ── 持仓相关研报 ─────────────────────────────────────────────────────
    reports_section = ""
    if research_reports:
        report_lines = []
        for r in research_reports[:10]:
            rating_icon = {"买入": "🟢", "增持": "🟢", "中性": "🟡",
                          "减持": "🔴", "卖出": "🔴"}.get(str(r.get("rating", "")).strip(), "⚪")
            stock = r.get("ts_code", "")
            src = r.get("source", "")
            title = r.get("title", "")[:50]
            date = str(r.get("report_date", ""))[:10]
            report_lines.append(f"{rating_icon} [{date}] {src} | {stock} | {title}")
        reports_section = "## 持仓相关券商研报（近7日）\n" + "\n".join(report_lines)
    else:
        reports_section = "## 持仓相关券商研报\n暂无持仓相关研报数据"

    # ── 持仓个股财务数据 ─────────────────────────────────────────────────
    financial_section = ""
    if financial_data:
        fin_lines = []
        for fd in financial_data[:10]:
            ts = fd.get("ts_code", "")
            date_str = str(fd.get("report_date", ""))[:10]
            eps = fd.get("eps")
            roe = fd.get("roe")
            rev = fd.get("total_revenue")
            np_ = fd.get("net_profit")
            gm = fd.get("gross_margin")
            dr = fd.get("debt_ratio")
            eps_str = f"{float(eps):.3f}" if eps else "N/A"
            roe_str = f"{float(roe):.2f}%" if roe else "N/A"
            rev_str = f"{float(rev)/1e8:.2f}亿" if rev else "N/A"
            np_str = f"{float(np_)/1e8:.2f}亿" if np_ else "N/A"
            gm_str = f"{float(gm):.2f}%" if gm else "N/A"
            dr_str = f"{float(dr):.2f}%" if dr else "N/A"
            fin_lines.append(
                f"- **{ts}** [{date_str}]: EPS={eps_str} ROE={roe_str} | "
                f"营收={rev_str} 净利={np_str} | 毛利率={gm_str} 负债率={dr_str}"
            )
        financial_section = "## 持仓个股最新财务数据\n" + "\n".join(fin_lines)
    else:
        financial_section = "## 持仓个股财务数据\n暂无财务数据"

    # ── 国际投行研究 ─────────────────────────────────────────────────────────
    intl_section = ""
    if international_research:
        bank_articles = [a for a in international_research if a.get('is_bank_related')]
        if bank_articles:
            intl_lines = []
            for a in bank_articles[:6]:
                cited = ', '.join(a.get('cited_institutions', [])) or a.get('source', '')
                art_type = a.get('article_type', '')
                type_str = f"[{art_type}] " if art_type else ""
                intl_lines.append(
                    f"- {type_str}**{cited}**: {a['title']}"
                )
                if a.get('desc'):
                    intl_lines.append(f"  {a['desc'][:120]}")
            intl_section = "## 国际投行研究（宏观/市场策略）\n" + "\n".join(intl_lines)
        else:
            intl_section = ""
    if not intl_section:
        intl_section = "## 国际投行研究\n暂无国际投行研究数据"

    # ── 持仓股公告 ──────────────────────────────────────────────────────────
    ann_section = ""
    if announcements:
        # 只取近7天重大公告
        recent = [a for a in announcements if a.get('is_major')]
        if recent:
            ann_lines = []
            for a in recent[:8]:
                ts = a.get('ts_code', '')
                ann_type = a.get('ann_type', '公告')
                date = a.get('notice_date', '')[:10]
                title = a.get('title', '')
                ann_lines.append(f"- [{date}] `[{ts}]` **{ann_type}**: {title}")
            ann_section = "## 持仓个股重要公告（近7日）\n" + "\n".join(ann_lines)
    if not ann_section:
        ann_section = "## 持仓个股重要公告（近7日）\n暂无重要公告"

    # ── 宏观日历 ────────────────────────────────────────────────────────────
    macro_lines = []
    for m in macro_calendar[:5]:
        macro_lines.append(f"- {m.get('date', '')} {m.get('event', '')}")
    macro_section = "## 近期宏观日历\n" + "\n".join(macro_lines) if macro_lines else "## 近期宏观日历\n暂无重大事件"

    # ── 资金概览 ────────────────────────────────────────────────────────────
    used_pct = sum(p.get("weight_pct", 0) for p in sanitized_positions)
    fund_section = f"""## 资金管理概览
总资产 = 持仓市值 + 可用资金
已用仓位：{used_pct:.1f}%
可用仓位：{100 - used_pct:.1f}%
"""

    # ── TAMF 投资记忆摘要（节省 token）───────────────────────────────────
    tamf_section = ""
    if tamf_summaries:
        tamf_lines = []
        for s in tamf_summaries[:15]:  # 最多15只持仓
            anon = s.get("anon_id", "?")
            status = s.get("status_emoji", "⚪")
            tech = s.get("tech_summary", "数据不足")
            news = s.get("news_summary", "数据不足")
            mon = s.get("monitoring_summary", "")
            tamf_lines.append(
                f"**{anon}** {status}\n"
                f"  技术面: {tech}\n"
                f"  消息面: {news}"
                + (f"\n  预警: {mon}" if mon else "")
            )
        tamf_section = "## 持仓标的分析记忆摘要（TAMF）\n" + "\n\n".join(tamf_lines)
    else:
        tamf_section = "## 持仓标的分析记忆摘要（TAMF）\n⚠️ 暂无TAMF数据，将基于原始数据生成建议"

    # ── 完整 Prompt ─────────────────────────────────────────────────────────
    prompt = f"""{SYSTEM_PROMPT}

{profile_section}

{tamf_section}

{positions_section}

{fund_section}

{index_section}

{sector_section}

{news_section}
{news_vector_section}

{reports_section}

{financial_section}

{intl_section}

{ann_section}

{macro_section}

## 任务要求
1. 逐只持仓给出操作建议（增持 buy / 减持 sell / 持有 hold / 调仓 rebalance），并附2-3句逻辑
2. 生成下一交易日具体操作计划，精确到仓位百分比，附带建议限价
3. 列出当日需重点关注的3个风险点
4. 给出简明市场展望（2-3句）
5. 评估本次分析置信度（high/medium/low）
6. 严格输出JSON格式，不要输出任何JSON以外的内容
"""

    return prompt


# ─── 简单持仓分析（无需 LLM，本地可完成）────────────────────────────────

def simple_position_analysis(positions: list[dict], total_mv: float) -> str:
    """
    本地持仓分析（不调用 LLM）
    适用于快速预览模式
    """
    if not positions:
        return "暂无持仓数据"

    lines = [f"\n{'='*50}",
             f"📊 持仓概览（总市值: ¥{total_mv:,.2f}）",
             f"{'='*50}\n"]

    lines.append(f"{'名称':<12} {'代码':<8} {'成本':>8} {'现价':>8} "
                 f"{'盈亏%':>8} {'仓位%':>6}")
    lines.append("-" * 60)

    total_pnl = 0
    for pos in positions:
        code = pos.get("code", "")
        name = pos.get("name", "")[:10]
        cost = pos.get("cost", 0)
        close = pos.get("close", cost)
        mv = pos.get("market_value", 0)
        pnl_pct = (close - cost) / cost * 100 if cost > 0 else 0
        weight = mv / total_mv * 100 if total_mv > 0 else 0
        total_pnl += mv - cost * pos.get("shares", 0)

        arrow = "🔴" if pnl_pct >= 0 else "🟢"
        lines.append(f"{name:<12} {code:<8} {cost:>8.3f} {close:>8.3f} "
                     f"{arrow}{pnl_pct:>+6.1f}% {weight:>5.1f}%")

    lines.append("-" * 60)
    total_return = total_pnl / (total_mv - total_pnl) * 100 if total_mv > total_pnl else 0
    lines.append(f"总盈亏: {'+' if total_pnl >= 0 else ''}¥{total_pnl:,.2f} ({total_return:+.1f}%)")
    lines.append(f"{'='*50}\n")

    return "\n".join(lines)


# ── Token 上限保护 ───────────────────────────────────────────────────────

MAX_TOKENS = 120_000  # DeepSeek 128k 上下文，保留 8k 缓冲


def count_tokens(text: str) -> int:
    """
    估算 token 数（中英文混合优化版）。
    中文按 2 char ≈ 1 token，英文按 4 char ≈ 1 token。
    """
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    english = len(text) - chinese
    return chinese * 2 + english * 3


def truncate_prompt(
    prompt: str,
    sections: list[tuple[str, str]] | None = None,
    max_tokens: int = MAX_TOKENS,
) -> str:
    """
    超长 prompt 压缩保护。

    sections: [(section_name, text), ...] 命名的分段列表，
              按出现顺序保留，优先截断靠后的非关键段。
    若未提供 sections，则直接按 max_tokens 截断 prompt 尾部。

    返回截断后的 prompt，并在开头附加截断提示行。
    """
    current = count_tokens(prompt)
    if current <= max_tokens:
        return prompt

    if sections is None:
        # 无分段信息：直接从尾部截断
        target_chars = int(len(prompt) * max_tokens / current)
        truncated = prompt[:target_chars]
        return f"[⚠️ 上下文已截断，原长度 ~{current} tokens]\n\n{truncated}"

    # 按优先级分组（末尾 = 低优先级）
    critical = ["# 角色设定", "你是一名", "系统提示", "持仓快照", "用户画像",
                "约束条件", "输出格式"]
    low_priority = ["财务数据", "研究机构", "国际投行", "近期新闻", "宏观日历",
                    "公告", "研报"]

    ordered = []
    for name, text in sections:
        priority = 0
        for kw in critical:
            if kw in name:
                priority = 2
                break
        for kw in low_priority:
            if kw in name:
                priority = 0
                break
        if priority == 0:
            for other_name, _ in ordered:
                if priority <= 0 and any(kw in other_name for kw in critical):
                    priority = 1
        ordered.append((name, text, priority))

    # 优先保留高优先级，丢弃低优先级
    kept = []
    dropped_tokens = 0
    for name, text, priority in reversed(ordered):
        seg_tokens = count_tokens(text)
        if current - dropped_tokens - seg_tokens <= max_tokens:
            break
        dropped_tokens += seg_tokens
    else:
        pass

    kept_tokens = current - dropped_tokens
    result_parts = []
    for name, text, priority in ordered:
        if kept_tokens + count_tokens(text) > max_tokens:
            continue
        kept_tokens += count_tokens(text)
        kept.append(f"## {name}\n{text}")

    truncated_prompt = "\n\n".join(result_parts)
    return (f"[⚠️ 上下文已压缩（~{current}→{kept_tokens} tokens，"
            f"已丢弃 {len(ordered) - len(kept)} 个低优先级分段）]\n\n"
            + truncated_prompt)
