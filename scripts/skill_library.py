"""
skill_library.py — 技能半自动固化流程
触发条件：同一任务 5 个交易日内调用 ≥3 次且未被大幅修正
流程：草案生成 → 人工审核 → 确认启用 → 执行 → 结果抽查
"""

import os
import json
import logging
import re
from pathlib import Path
from datetime import datetime

import psycopg2
from dotenv import load_dotenv
load_dotenv(str(Path(__file__).parent.parent / ".env"))

logger = logging.getLogger("invest_system.skill_library")

SKILLS_DIR = Path(__file__).parent.parent / "skills"
DRAFT_DIR = SKILLS_DIR / "drafts"
APPROVED_DIR = SKILLS_DIR / "approved"

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_db_conn():
    """优先使用 credentials 模块（支持 WCM / 本地文件），降级为环境变量"""
    try:
        from credentials import get_credential
        db_url = get_credential("DATABASE_URL")
        if db_url and "***" not in db_url:
            return psycopg2.connect(db_url)
        # 尝试只传密码
        db_pass = get_credential("DB_PASSWORD")
        if db_pass:
            return psycopg2.connect(
                host="localhost", user="invest_admin",
                database="investpilot", password=db_pass,
            )
    except ImportError:
        pass
    # 降级：环境变量
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url and "***" not in db_url:
        return psycopg2.connect(db_url)
    return psycopg2.connect(
        host="localhost", user="invest_admin",
        database="investpilot",
        password=os.environ.get("DB_PASSWORD", "") or os.environ.get("POSTGRES_PASSWORD", ""),
    )

# ── 固化触发条件 ──────────────────────────────────────────────────────────
TRIGGER_DAYS = 5          # 5 个交易日内
TRIGGER_MIN_CALLS = 3     # 至少调用 3 次
REJECT_THRESHOLD = 0.20  # 修正幅度 > 20% 视为不认可


# ── 技能草案生成 ────────────────────────────────────────────────────────

def generate_skill_draft(
    task_pattern: str,
    call_count: int,
    recent_calls: list[dict],
    user_corrections: list[dict],
) -> dict:
    """
    根据调用日志和用户修正记录，生成技能草案。

    recent_calls: [{"task": str, "result": dict, "timestamp": datetime}, ...]
    user_corrections: [{"original": dict, "modified": dict, "reason": str}, ...]
    """
    from agent_interface import get_agent

    # 提取共同模式
    task_type = _classify_task_type(task_pattern)

    # 构建上下文
    context_parts = [
        f"任务类型: {task_type}",
        f"调用次数: {call_count} 次",
        f"用户修正次数: {len(user_corrections)} 次",
    ]
    for i, call in enumerate(recent_calls[:5]):
        context_parts.append(f"调用{i+1}: {call.get('task', '')[:100]}")

    for i, corr in enumerate(user_corrections[:3]):
        context_parts.append(
            f"修正{i+1}: {json.dumps(corr.get('original', {}), ensure_ascii=False)[:100]}"
            f" → {json.dumps(corr.get('modified', {}), ensure_ascii=False)[:100]}"
        )

    prompt = f"""你是一名量化投资技能工程师，请根据以下调用日志生成一个可固化的技能。

## 任务类型
{context_parts[0]}

## 调用日志
{chr(10).join(context_parts[1:])}

## 输出要求
生成一个技能文件，包含：
1. skill_name: 技能名称（英文，简短）
2. trigger_keywords: 触发关键词列表（正则表达式）
3. description: 技能描述（1-2句）
4. system_prompt: 专用 System Prompt（专业量化风格，200字以内）
5. routing_hint: 路由提示（"ollama" | "deepseek" | "local"）
6. confidence_threshold: 最低置信度要求（0.0-1.0）
7. blacklist: 禁忌场景（哪些情况下不能使用此技能）

请用 JSON 格式输出。
"""

    agent = get_agent()
    result = agent.chat(prompt, system="你是一个严谨的技能工程师，输出必须是有效JSON。", force_model="deepseek")  # noqa: E501
    raw_content = result.get("content", "")

    # 解析 JSON
    draft_json = _extract_json(raw_content)
    if not draft_json:
        logger.warning(f"技能草案 JSON 解析失败: {raw_content[:200]}")
        return {"error": "JSON解析失败", "raw": raw_content}

    # 保存草案文件
    draft_json["_meta"] = {
        "created_at": datetime.now().isoformat(),
        "task_pattern": task_pattern,
        "call_count": call_count,
        "correction_count": len(user_corrections),
        "status": "draft",
    }

    draft_name = draft_json.get("skill_name", "unknown").lower().replace(" ", "_")
    draft_path = DRAFT_DIR / f"{draft_name}_draft.json"
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    with open(draft_path, "w", encoding="utf-8") as f:
        json.dump(draft_json, f, ensure_ascii=False, indent=2)

    logger.info(f"技能草案已生成: {draft_path}")
    return draft_json


def _classify_task_type(pattern: str) -> str:
    """识别任务类型"""
    patterns = {
        "technical_screener": r"RSI|MACD|布林带|均线|技术指标|KDJ|ATR",
        "news_sentiment": r"新闻|情绪|快讯|市场情绪",
        "risk_check": r"风控|仓位检查|超限|合规",
        "rebalance": r"调仓|再平衡|仓位调整",
        "sector_rotation": r"行业|板块|赛道|轮动|景气",
    }
    for name, regex in patterns.items():
        if re.search(regex, pattern, re.IGNORECASE):
            return name
    return "general"


def _extract_json(content: str) -> dict:
    """从文本中提取 JSON"""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    first, last = content.find("{"), content.rfind("}")
    if first != -1 and last != -1 and last > first:
        try:
            return json.loads(content[first:last + 1])
        except json.JSONDecodeError:
            pass
    return {}


# ── 技能生命周期管理 ────────────────────────────────────────────────

class SkillLifecycle:
    """技能生命周期管理器"""

    def __init__(self):
        self._ensure_dirs()

    def _ensure_dirs(self):
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        DRAFT_DIR.mkdir(parents=True, exist_ok=True)
        APPROVED_DIR.mkdir(parents=True, exist_ok=True)

    def list_drafts(self) -> list[dict]:
        drafts = []
        for f in DRAFT_DIR.glob("*_draft.json"):
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
                data["_file"] = str(f)
                drafts.append(data)
        return drafts

    def approve_draft(self, draft_name: str, modifications: dict = None) -> bool:
        """人工审核通过后，将草案转为正式技能"""
        draft_path = DRAFT_DIR / f"{draft_name}_draft.json"
        if not draft_path.exists():
            logger.error(f"草案不存在: {draft_path}")
            return False

        with open(draft_path, encoding="utf-8") as f:
            draft = json.load(f)

        # 应用人工修改
        if modifications:
            draft.update(modifications)

        # 生成 markdown 文件
        skill_md = self._generate_skill_md(draft)

        approved_name = draft.get("skill_name", draft_name).lower().replace(" ", "_")
        approved_path = APPROVED_DIR / f"{approved_name}.md"
        with open(approved_path, "w", encoding="utf-8") as f:
            f.write(skill_md)

        # 记录到数据库
        self._record_skill_version(draft, approved_path)

        # 删除草案
        draft_path.unlink()

        logger.info(f"技能已批准: {approved_path}")
        return True

    def reject_draft(self, draft_name: str, reason: str) -> bool:
        """拒绝草案"""
        draft_path = DRAFT_DIR / f"{draft_name}_draft.json"
        if not draft_path.exists():
            return False

        rejected_path = DRAFT_DIR / f"{draft_name}_rejected_{datetime.now().strftime('%Y%m%d%H%M')}.json"  # noqa: E501
        draft_path.rename(rejected_path)

        # 记录拒绝原因
        conn = get_db_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO audit.audit_log
                    (event_type, operator, target_type, detail, result)
                VALUES ('SKILL_REJECTED', 'USER', 'SKILL', %s, 'REJECTED')
            """, (json.dumps({"draft": draft_name, "reason": reason}),))
            conn.commit()
        finally:
            conn.close()

        logger.info(f"技能草案已拒绝: {draft_name}, 原因: {reason}")
        return True

    def list_approved(self) -> list[dict]:
        """列出所有已批准的技能（含元数据）"""
        approved = []
        for f in APPROVED_DIR.glob("*.md"):
            content = f.read_text(encoding="utf-8")
            name_m = re.search(r"^#\s+(.+)$", content, re.M)
            kw_m = re.search(r"## 触发关键词\n```regex\n(.*?)\n```", content, re.DOTALL)
            route_m = re.search(r"路由目标:\s*`(\w+)`", content)
            ver_m = re.search(r"## 版本历史\n(.+?)(?=\n## |$)", content, re.DOTALL)
            approved.append({
                "skill_name": name_m.group(1).strip() if name_m else f.stem,
                "trigger_keywords": kw_m.group(1).strip() if kw_m else "",
                "routing": route_m.group(1) if route_m else "ollama",
                "version_history": ver_m.group(1).strip() if ver_m else "",
                "path": str(f),
                "modified": f.stat().st_mtime,
            })
        return approved

    def get_skill_version_history(self, skill_name: str) -> list[dict]:
        """查询某技能的版本变更历史（从 audit_log）"""
        conn = get_db_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT event_type, operator, detail, result, event_time
                FROM audit.audit_log
                WHERE target_type = 'SKILL'
                  AND target_id = %s
                  AND event_type IN ('SKILL_APPROVED', 'SKILL_REJECTED', 'SKILL_ROLLBACK')
                ORDER BY event_time DESC
                LIMIT 20
            """, (skill_name,))
            return [
                {"event": r[0], "operator": r[1], "detail": json.loads(r[2]) if r[2] else {},
                 "result": r[3], "at": r[4].isoformat() if r[4] else ""}
                for r in cur.fetchall()
            ]
        finally:
            conn.close()

    def rollback_skill(self, skill_name: str, to_version: int = None) -> bool:
        """
        回滚技能到上一版本。
        - 读取当前 APPROVED_DIR/{skill_name}.md 的版本历史
        - 将当前版本重命名为备份
        - 从 audit_log 恢复上一版本的 detail
        """
        skill_path = APPROVED_DIR / f"{skill_name}.md"
        if not skill_path.exists():
            logger.error(f"技能不存在: {skill_name}")
            return False

        # 备份当前版本
        backup_path = APPROVED_DIR / f"{skill_name}_backup_{datetime.now().strftime('%Y%m%d%H%M')}.md"  # noqa: E501
        skill_path.rename(backup_path)
        logger.info(f"已备份当前版本到: {backup_path.name}")

        # 从 audit_log 取最新批准版本恢复
        history = self.get_skill_version_history(skill_name)
        approved_versions = [h for h in history if h["event"] == "SKILL_APPROVED"]
        if not approved_versions:
            logger.error("无历史版本可恢复")
            backup_path.rename(skill_path)  # 还原
            return False

        target = approved_versions[0] if to_version is None else approved_versions[to_version - 1]
        prev_draft = target["detail"]

        # 重建 markdown 并写入
        if "system_prompt" in prev_draft:
            prev_draft["_meta"] = prev_draft.get("_meta", {})
            skill_md = self._generate_skill_md(prev_draft)
        else:
            # audit_log 中无完整数据，从备份文件提取
            content = backup_path.read_text(encoding="utf-8")
            match = re.search(r"## System Prompt\n(.*?)(?=\n## |$)", content, re.DOTALL)
            prev_draft["system_prompt"] = match.group(1).strip() if match else ""
            skill_md = self._generate_skill_md(prev_draft)

        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(skill_md)

        # 记录回滚事件
        conn = get_db_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO audit.audit_log
                    (event_type, operator, target_type, target_id, detail, result)
                VALUES ('SKILL_ROLLBACK', 'USER', 'SKILL', %s, %s, 'SUCCESS')
            """, (skill_name, json.dumps({"from": str(backup_path.name)}, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()

        logger.info(f"技能已回滚: {skill_name}")
        return True

    def _generate_skill_md(self, draft: dict) -> str:
        """生成标准 Markdown 技能文件"""
        meta = draft.get("_meta", {})
        name = draft.get("skill_name", "Unknown")
        desc = draft.get("description", "")
        keywords = draft.get("trigger_keywords", "")
        prompt = draft.get("system_prompt", "")
        routing = draft.get("routing_hint", "ollama")
        threshold = draft.get("confidence_threshold", 0.7)
        blacklist = draft.get("blacklist", [])

        return f"""# {name}

**状态**: 已批准 | **{datetime.now().strftime('%Y-%m-%d')}**

## 描述
{desc}

## 触发关键词
```regex
{keywords}
```

## 路由
- 路由目标: `{routing.upper()}`
- 最低置信度: `{threshold}`

## System Prompt
{prompt}

## 禁忌场景
{chr(10).join(f"- {b}" for b in blacklist) if blacklist else "- 无"}

## 版本历史
- v1.0: 初始版本（由 {meta.get('task_pattern', '')} 模式生成）
"""

    def _record_skill_version(self, draft: dict, path: Path):
        conn = get_db_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO audit.audit_log
                    (event_type, operator, target_type, target_id, detail, result)
                VALUES ('SKILL_APPROVED', 'USER', 'SKILL', %s, %s, 'SUCCESS')
            """, (
                draft.get("skill_name", ""),
                json.dumps({**draft, "_path": str(path)}, ensure_ascii=False),
            ))
            conn.commit()
        finally:
            conn.close()


# ── 技能执行 ──────────────────────────────────────────────────────────

def run_skill(skill_name: str, query: str, context: dict = None) -> dict:
    """
    执行已批准的技能
    1. 读取技能 Markdown
    2. 提取 system_prompt
    3. 调用对应模型
    """
    skill_path = APPROVED_DIR / f"{skill_name}.md"
    if not skill_path.exists():
        return {"error": f"技能不存在: {skill_name}"}

    content = skill_path.read_text(encoding="utf-8")

    # 提取 system_prompt 部分
    match = re.search(r"## System Prompt\n(.*?)(?=\n## |$)", content, re.DOTALL)
    system_prompt = match.group(1).strip() if match else ""

    # 提取路由
    route_match = re.search(r"路由目标:\s*`(\w+)`", content)
    routing = route_match.group(1) if route_match else "ollama"

    # 执行
    from agent_interface import get_agent
    agent = get_agent()

    if routing == "deepseek":
        result = agent.chat(query, system=system_prompt, force_model="deepseek")
    else:
        result = agent.chat(query, system=system_prompt, force_model="ollama")

    # 记录执行
    _log_skill_execution(skill_name, query, result)

    return result


def _log_skill_execution(skill_name: str, query: str, result: dict):
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO audit.audit_log
                (event_type, operator, target_type, detail, result)
            VALUES ('SKILL_EXECUTED', 'SYSTEM', 'SKILL', %s, %s)
        """, (
            json.dumps({"skill": skill_name, "query": query[:100]}, ensure_ascii=False),
            "SUCCESS" if not result.get("error") else "FAILED",
        ))
        conn.commit()
    finally:
        conn.close()


# ── 技能合规检查 ──────────────────────────────────────────────────────

def check_skill_safety(skill_name: str, query: str) -> tuple[bool, str]:
    """
    检查技能是否在禁忌场景中
    返回: (is_safe, reason)
    """
    skill_path = APPROVED_DIR / f"{skill_name}.md"
    if not skill_path.exists():
        return True, "技能不存在，跳过安全检查"

    content = skill_path.read_text(encoding="utf-8")

    # 提取黑名单关键词
    blacklist_section = re.search(r"## 禁忌场景\n(.*?)(?=\n## |$)", content, re.DOTALL)
    if not blacklist_section:
        return True, "无禁忌场景"

    blacklist_text = blacklist_section.group(1)
    blacklist_items = re.findall(r"- (.+)", blacklist_text)

    for item in blacklist_items:
        if re.search(item.strip(), query, re.IGNORECASE):
            return False, f"触发禁忌场景: {item.strip()}"

    return True, "安全"


# ── 结果抽查 ──────────────────────────────────────────────────────────

def spot_check_skill_result(skill_name: str, result: dict, expected: dict) -> dict:
    """
    随机抽查 10% 的技能执行结果
    对比实际输出与预期的偏差
    """
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO audit.audit_log
                (event_type, operator, target_type, detail, result)
            VALUES ('SKILL_SPOT_CHECK', 'SYSTEM', 'SKILL', %s, %s)
        """, (
            json.dumps({
                "skill": skill_name,
                "result_summary": str(result.get("content", ""))[:100],
                "expected_summary": str(expected)[:100],
            }, ensure_ascii=False),
            "FLAGGED" if _is_result_suspicious(result, expected) else "OK",
        ))
        conn.commit()
    finally:
        conn.close()

    return {
        "suspicious": _is_result_suspicious(result, expected),
        "skill": skill_name,
    }


def _is_result_suspicious(result: dict, expected: dict) -> bool:
    """判断结果是否可疑"""
    if result.get("error"):
        return True
    content = result.get("content", "")
    if len(content) < 10:
        return True
    return False


# ── 固化触发检测 ────────────────────────────────────────────────────

def check_skill_triggers() -> list[dict]:
    """
    扫描调用日志，检测是否有任务满足固化条件。
    返回满足条件的任务列表。
    """
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                detail->>'task' as task_pattern,
                COUNT(*) as call_count,
                MAX(event_time) as last_call
            FROM audit.audit_log
            WHERE event_type = 'SKILL_EXECUTED'
              AND event_time >= CURRENT_DATE - INTERVAL '%s days'
            GROUP BY detail->>'task'
            HAVING COUNT(*) >= %s
        """, (TRIGGER_DAYS, TRIGGER_MIN_CALLS))

        triggered = []
        for row in cur.fetchall():
            triggered.append({
                "task_pattern": row[0],
                "call_count": row[1],
                "last_call": row[2].isoformat() if row[2] else None,
            })
        return triggered
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sl = SkillLifecycle()

    print("=== 技能库 ===")
    print(f"草案: {len(sl.list_drafts())} 个")
    print(f"已批准: {len(list(APPROVED_DIR.glob('*.md')))} 个")

    print("\n=== 固化触发检测 ===")
    triggers = check_skill_triggers()
    print(f"满足固化条件的任务: {len(triggers)} 个")
    for t in triggers:
        print(f"  - {t['task_pattern']}: {t['call_count']} 次调用")
