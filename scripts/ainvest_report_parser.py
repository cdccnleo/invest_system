"""
ainvest_report_parser.py — AInvest 投资分析报告解析引擎

核心职责:
  1. 扫描 C:/PythonProject/AInvest/reports 目录下的 Markdown 报告
  2. 按类型分类（events/trackers/deep-analysis/daily）
  3. 提取结构化字段（股票代码、事件标签、投资信号、操作建议）
  4. 调用 LLM 进行语义增强提取（ENRICHMENT 层）
  5. 输出 JSON 结构化数据供知识库写入

解析策略:
  - 正则表达式提取（确定性字段：股票代码、日期、标题）
  - LLM 辅助提取（不确定性字段：信号方向、置信度、风险评估）
  - 渐进式解析：先正则 → 不满足再 LLM

修正记录（基于审核确认文档）:
  - M1: LLM 调用使用 DeepSeekClient().chat() 而非不存在的 call_deepseek()
  - M2: trackers 报告日期从文件修改时间 fallback 提取
  - E1: 多编码容错（utf-8 → gbk → cp936 → utf-8-sig）
"""

import os
import re
import json
import logging
import hashlib
from pathlib import Path
from datetime import datetime, date
from typing import Optional

from utils import read_file_with_encoding

logger = logging.getLogger("invest_system.ainvest_parser")

# ── 路径常量 ────────────────────────────────────────────────
# WSL + Windows 兼容：优先 Windows 路径，WSL 下自动映射到 /mnt/c/
_WIN_PATH = Path("C:/PythonProject/AInvest/reports")
_MNT_PATH = Path("/mnt/c/PythonProject/AInvest/reports")
if _WIN_PATH.exists():
    AINVEST_REPORTS_DIR = _WIN_PATH
elif _MNT_PATH.exists():
    AINVEST_REPORTS_DIR = _MNT_PATH
else:
    AINVEST_REPORTS_DIR = _WIN_PATH  # 保持原值，目录不存在时 scan 会有 warning

# 股票代码正则：匹配 6位数字（A股）+ 可选5位港股代码
STOCK_CODE_PATTERN = re.compile(
    r'(?:\d{6}(?:\.(?:SH|SZ|BJ|HK))?'   # 6位数字+可选交易所后缀
    r'|\d{5}\.HK'                          # 5位港股代码
    r'|\d{4}\.HK)'                         # 4位港股代码
)

# ── 报告分类映射 ────────────────────────────────────────────
REPORT_TYPE_MAP = {
    "events": "events",
    "trackers": "trackers",
    "deep-analysis": "deep-analysis",
    "daily": "daily",
}


def compute_file_hash(filepath: Path) -> str:
    """计算文件的 SHA-256 哈希，用于变更检测"""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def extract_report_date_from_filename(filename: str, filepath: Path = None) -> Optional[date]:
    """
    从文件名提取报告日期。
    
    events/deep-analysis/daily 格式: YYYY-MM-DD_xxx.md → 直接提取
    trackers 格式: {代码}_{名称}_投资跟踪.md → 从文件修改时间 fallback
    
    修正 M2: 增加 trackers 格式的 fallback 逻辑
    """
    match = re.match(r'(\d{4}-\d{2}-\d{2})', filename)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    
    # trackers 格式：{代码}_{名称}_投资跟踪.md → 从文件修改时间提取
    if filepath and filepath.exists():
        return date.fromtimestamp(os.path.getmtime(filepath))
    
    return None


def extract_stock_codes_from_content(content: str) -> list[str]:
    """从报告内容提取所有 A 股代码（6位数字，去重归一化）"""
    codes = set()
    for match in STOCK_CODE_PATTERN.finditer(content):
        code = match.group(0).strip()
        # 归一化：去掉 .SH/.SZ/.BJ 后缀
        code_clean = re.sub(r'\.(SH|SZ|BJ|HK)$', '', code)
        if code_clean.isdigit() and len(code_clean) == 6:
            codes.add(code_clean)
    return sorted(codes)


def extract_event_tags(content: str) -> list[str]:
    """从报告内容提取事件标签"""
    tags = set()
    
    # 从一级标题提取
    title_match = re.search(r'^#\s+(.+?)$', content, re.MULTILINE)
    if title_match:
        title = title_match.group(1)
        parts = re.split(r'[_\-\u2014\s\u3001]+', title)
        for part in parts:
            part = part.strip()
            if 3 <= len(part) <= 30 and not part.startswith('#'):
                tags.add(part)
    
    # 从二级标题（##）提取前几个
    for match in re.finditer(r'^##\s+(.+?)$', content, re.MULTILINE):
        section = match.group(1).strip()
        if len(section) <= 20:
            tags.add(section)
    
    return sorted(tags)[:20]  # 限制标签数量


def extract_report_type_by_path(filepath: Path) -> str:
    """根据文件路径判断报告类型"""
    for key, value in REPORT_TYPE_MAP.items():
        if key in filepath.parts:
            return value
    return "unknown"


def extract_structured_report(report_type: str, content: str, title: str) -> dict:
    """
    根据报告类型，提取结构化字段。
    返回 dict，包含 summary, investment_signals, key_judgments, risk_assessment, operation_actions 等。  # noqa: E501
    """
    result = {
        "summary": None,
        "related_codes": extract_stock_codes_from_content(content),
        "event_tags": extract_event_tags(content),
        "investment_signals": [],
        "key_judgments": [],
        "risk_assessment": None,
        "operation_actions": [],
        "primary_stock_code": None,
        "confidence_score": 0.5,
    }

    if report_type == "events":
        result = _extract_event_report(content, result)
    elif report_type == "trackers":
        result = _extract_tracker_report(content, result)
    elif report_type == "deep-analysis":
        result = _extract_deep_analysis_report(content, result)
    elif report_type == "daily":
        result = _extract_daily_report(content, result)

    # 确定主标的代码
    if result["related_codes"]:
        result["primary_stock_code"] = result["related_codes"][0]

    return result


def _extract_event_report(content: str, base: dict) -> dict:
    """解析事件分析报告 — 提取持仓暴露度评估"""
    # 提取"持仓标的暴露度评估"表格
    exposure_table = _extract_table_section(content, "持仓标的", "暴露度|受影响|影响评估")
    if exposure_table:
        base["investment_signals"].append({
            "type": "exposure_assessment",
            "source": exposure_table[:1000],
        })
    
    # 提取核心冲击/影响判断
    impact_section = _extract_section(content, "核心冲击|执行摘要|核心判断")
    if impact_section:
        base["key_judgments"].append(impact_section[:300])
    
    return base


def _extract_tracker_report(content: str, base: dict) -> dict:
    """解析个股跟踪报告 — 提取核心投资要点和风险因素"""
    # 提取投资逻辑
    summary_match = re.search(
        r'(?:投资逻辑|核心逻辑|核心观点)[：:](.+?)(?:\n\n|\n---|\n##)',
        content, re.DOTALL
    )
    if summary_match:
        base["summary"] = summary_match.group(1).strip()[:300]
    
    # 提取风险因素
    risk_section = _extract_section(content, "风险|风险因素|主要风险")
    if risk_section:
        base["risk_assessment"] = risk_section[:500]
    
    # 提取核心业务与竞争优势
    comp_section = _extract_section(content, "核心业务|竞争优势|竞争壁垒")
    if comp_section:
        base["key_judgments"].append(comp_section[:300])
    
    return base


def _extract_deep_analysis_report(content: str, base: dict) -> dict:
    """解析深度分析报告 — 提取执行摘要和投资建议"""
    # 提取执行摘要或核心判断
    exec_summary = _extract_section(content, "执行摘要|核心判断|投资结论")
    if exec_summary:
        base["key_judgments"].append(exec_summary[:300])
    
    # 提取操作建议
    action_section = _extract_section(content, "操作建议|投资建议|策略建议")
    if action_section:
        base["operation_actions"].append({
            "type": "investment_advice",
            "content": action_section[:500],
        })
    
    # 提取估值分析
    valuation_section = _extract_section(content, "估值分析|估值|目标价")
    if valuation_section:
        base["investment_signals"].append({
            "type": "valuation_analysis",
            "content": valuation_section[:500],
        })
    
    return base


def _extract_daily_report(content: str, base: dict) -> dict:
    """解析日常复盘与操作计划 — 提取操作计划和止损调整"""
    # 提取操作计划表
    plan_table = _extract_table_section(content, "操作计划|操作矩阵|操作策略")
    if plan_table:
        base["operation_actions"].append({
            "type": "daily_operation_plan",
            "content": plan_table[:1000],
        })
    
    # 提取止损/止盈调整
    stop_loss = _extract_section(content, "止损|止盈|止损价|止盈价")
    if stop_loss:
        base["investment_signals"].append({
            "type": "stop_loss_adjustment",
            "content": stop_loss[:300],
        })
    
    return base


def _extract_section(content: str, section_keywords: str) -> Optional[str]:
    """提取指定关键词所在的章节内容（前1000字符）"""
    pattern = re.compile(
        r'(?:^|\n)##\s*(?:' + section_keywords + r')[^。\n]*[。\n]'
        r'((?:(?!\n##)[\s\S]){0,1000})',
        re.MULTILINE | re.IGNORECASE
    )
    match = pattern.search(content)
    if match:
        return match.group(0).strip()
    return None


def _extract_table_section(content: str, table_keyword: str, row_filter: str = "") -> Optional[str]:
    """提取包含指定关键词的 Markdown 表格"""
    lines = content.split("\n")
    in_table = False
    table_lines = []
    
    for line in lines:
        stripped = line.strip()
        if "|" in stripped and table_keyword in stripped:
            in_table = True
        if in_table:
            if "|" not in stripped and stripped == "":
                if table_lines:
                    break
            if "|" in stripped:
                table_lines.append(stripped)
    
    if not table_lines:
        return None
    
    if row_filter:
        filtered = [line for line in table_lines if re.search(row_filter, line)]
        if filtered:
            return "\n".join(filtered)
    
    return "\n".join(table_lines)


# ── LLM 增强提取（ENRICHMENT 层）──────────────────────────

def enrich_with_llm(report_data: dict, content: str) -> dict:
    """
    使用 LLM 对解析结果进行语义增强。
    
    修正 M1: 使用 DeepSeekClient().chat() 而非不存在的 call_deepseek()。
    仅在正则提取结果不充分时调用，节省 API 成本。
    """
    # 如果已有充分的结构化数据，跳过 LLM 增强
    if (report_data.get("investment_signals")
            and report_data.get("key_judgments")
            and report_data.get("confidence_score", 0) > 0.7):
        return report_data
    
    # 构建精简 prompt
    prompt = _build_enrichment_prompt(report_data, content)
    
    try:
        from llm_caller import DeepSeekClient
        client = DeepSeekClient()
        response = client.chat(prompt, system="")
        
        if response.get("error"):
            logger.warning(f"LLM 增强提取失败: {response['error']}")
            return report_data
        
        enriched = _parse_enrichment_response(response.get("content", ""))
        report_data.update(enriched)
        report_data["confidence_score"] = max(
            report_data.get("confidence_score", 0.5),
            enriched.get("confidence_score", 0.5)
        )
    except Exception as e:
        logger.warning(f"LLM 增强提取异常: {e}")
    
    return report_data


def _build_enrichment_prompt(report_data: dict, content: str) -> str:
    """构建 LLM 增强提取的 prompt"""
    excerpt = content[:2000]
    related_codes = report_data.get("related_codes", [])
    
    prompt = f"""你是一个专业的投资分析报告解析助手。请从以下报告片段中提取结构化信息。

报告关联股票代码: {', '.join(related_codes) if related_codes else '未知'}

报告片段:
{excerpt}

请以 JSON 格式输出以下字段:
1. "summary": 报告摘要（50字以内）
2. "investment_signals": 投资信号数组，每项包含 {{"type": "信号类型", "direction": "positive/negative/neutral", "description": "信号描述", "magnitude": 0.0-1.0}}  # noqa: E501
3. "key_judgments": 核心判断数组（每项30字以内）
4. "risk_assessment": 风险评估（30字以内）
5. "confidence_score": 你对提取结果的置信度 (0.0-1.0)

只输出 JSON，不要其他内容。"""
    return prompt


def _parse_enrichment_response(response: str) -> dict:
    """解析 LLM 响应中的 JSON"""
    # 尝试提取 JSON 代码块
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
    
    # 直接解析整个响应
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        logger.warning(f"LLM 响应 JSON 解析失败: {response[:200]}")
        return {}


# ── 主解析函数 ─────────────────────────────────────────────

def parse_single_report(filepath: Path) -> Optional[dict]:
    """
    解析单份报告，返回结构化数据。
    
    修正 E1: 多编码容错（utf-8 → gbk → cp936 → utf-8-sig）。
    返回 None 表示解析失败。
    """
    # 多编码尝试读取
    content = read_file_with_encoding(filepath)
    if content is None:
        logger.error(f"无法解码文件: {filepath}")
        return None
    
    filename = filepath.name
    report_type = extract_report_type_by_path(filepath) or "unknown"
    report_date = extract_report_date_from_filename(filename, filepath)
    title = content.split("\n")[0].lstrip("#").strip() if content.startswith("#") else filename
    
    # 基础结构化提取
    report_data = extract_structured_report(report_type, content, title)
    
    # LLM 增强提取（按需，仅 events/deep-analysis 类型）
    if report_type in ("events", "deep-analysis"):
        report_data = enrich_with_llm(report_data, content)
    
    return {
        "file_path": str(filepath),
        "file_hash": compute_file_hash(filepath),
        "report_type": report_type,
        "title": title,
        "report_date": str(report_date) if report_date else None,
        "report_date_obj": report_date,
        "file_modified_at": datetime.fromtimestamp(
            os.path.getmtime(filepath)
        ).isoformat(),
        "raw_text": content,
        **report_data,
    }


def scan_reports_directory(
    reports_dir: Path = AINVEST_REPORTS_DIR,
    known_hashes: dict = None
) -> dict:
    """
    扫描报告目录，返回新文件/变更文件/未变更文件统计。
    
    known_hashes: {filepath: hash} 已知的文件哈希映射
    Returns: {
        "all_files": [...],
        "new_files": [...],
        "changed_files": [...],
        "unchanged_files": [...],
        "scan_time": "YYYY-MM-DD HH:MM:SS"
    }
    """
    if known_hashes is None:
        known_hashes = {}
    
    results = {
        "all_files": [],
        "new_files": [],
        "changed_files": [],
        "unchanged_files": [],
        "scan_time": datetime.now().isoformat(),
    }
    
    if not reports_dir.exists():
        logger.warning(f"AInvest 报告目录不存在: {reports_dir}")
        return results
    
    # 递归扫描所有 .md 文件（排除 README.md、INDEX.md、archive/、templates/）
    md_files = list(reports_dir.rglob("*.md"))
    md_files = [
        f for f in md_files
        if f.name not in ("README.md", "INDEX.md")
        and "archive" not in f.parts
        and "templates" not in f.parts
    ]
    
    for filepath in md_files:
        str_path = str(filepath)
        try:
            current_hash = compute_file_hash(filepath)
        except Exception as e:
            logger.warning(f"计算文件哈希失败 {filepath}: {e}")
            continue
        
        results["all_files"].append(str_path)
        
        if str_path not in known_hashes:
            results["new_files"].append(str_path)
        elif known_hashes[str_path] != current_hash:
            results["changed_files"].append(str_path)
        else:
            results["unchanged_files"].append(str_path)
    
    return results