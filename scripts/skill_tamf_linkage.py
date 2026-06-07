"""
skill_tamf_linkage.py — 技能与TAMF的联动机制

当TAMF检测到标的的基本面发生重大变化时，自动标记关联技能为"待复审"。
存储: memory.target_memory_files.linked_skills (TEXT[])

核心能力:
  1. link_skill_to_target()      — 将技能关联到投资标的
  2. _get_linked_skills()        — 获取某标的关联的技能列表
  3. on_tamf_fundamental_change()— 基本面重大变化 → 技能自动标记复审
  4. _flag_skill_for_review()    — 在技能文件中添加复审标记
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("skill_tamf_linkage")

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
SKILLS_DIR = PROJECT_ROOT / "skills"
APPROVED_DIR = SKILLS_DIR / "approved"
LINKAGE_FILE = SKILLS_DIR / "tamf_linkage.json"
CREDENTIAL_STORE = Path.home() / ".hermes" / "invest_credentials" / "store.json"


def _get_db_conn():
    """获取数据库连接"""
    import psycopg2
    with open(CREDENTIAL_STORE) as f:
        creds = json.load(f)
    return psycopg2.connect(
        host="localhost", port=5432,
        dbname="investpilot", user="invest_admin",
        password=creds["DB_PASSWORD"]
    )


def _load_linkage() -> dict:
    """加载本地联动配置文件"""
    if LINKAGE_FILE.exists():
        with open(LINKAGE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"links": {}, "pending_reviews": []}


def _save_linkage(data: dict) -> None:
    """保存联动配置到本地文件"""
    LINKAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LINKAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class SkillTAMFLinkage:
    """技能与TAMF的联动机制"""

    def link_skill_to_target(self, skill_name: str, ts_code: str) -> bool:
        """
        将已批准的技能关联到投资标的。
        同时更新 DB 和本地联动文件。
        """
        # 确认技能文件存在
        skill_path = APPROVED_DIR / f"{skill_name}.md"
        if not skill_path.exists():
            logger.warning(f"技能文件不存在: {skill_path}")
            return False

        # 更新 DB 中的 linked_skills 字段
        try:
            conn = _get_db_conn()
            cur = conn.cursor()
            cur.execute("""
                UPDATE memory.target_memory_files
                SET linked_skills = array_append(
                    COALESCE(linked_skills, ARRAY[]::TEXT[]), %s
                )
                WHERE ts_code = %s
            """, (skill_name, ts_code))
            updated = cur.rowcount
            conn.commit()
            conn.close()
            if updated > 0:
                logger.info(f"技能联动: {skill_name} ↔ {ts_code}")
        except Exception as e:
            logger.warning(f"DB联动写入失败: {e}")
            return False

        # 更新本地联动文件
        data = _load_linkage()
        if ts_code not in data["links"]:
            data["links"][ts_code] = []
        if skill_name not in data["links"][ts_code]:
            data["links"][ts_code].append(skill_name)
        _save_linkage(data)

        return True

    def unlink_skill_from_target(self, skill_name: str, ts_code: str) -> bool:
        """解除技能与标的的关联"""
        try:
            conn = _get_db_conn()
            cur = conn.cursor()
            cur.execute("""
                UPDATE memory.target_memory_files
                SET linked_skills = array_remove(linked_skills, %s)
                WHERE ts_code = %s
            """, (skill_name, ts_code))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"DB联动删除失败: {e}")

        data = _load_linkage()
        if ts_code in data["links"] and skill_name in data["links"][ts_code]:
            data["links"][ts_code].remove(skill_name)
            _save_linkage(data)
        return True

    def _get_linked_skills(self, ts_code: str) -> list[str]:
        """获取某标的关联的技能列表（优先DB，降级本地文件）"""
        try:
            conn = _get_db_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT linked_skills FROM memory.target_memory_files WHERE ts_code = %s",
                (ts_code,)
            )
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                return [s for s in row[0] if s]
        except Exception as e:
            logger.debug(f"DB读取linked_skills失败: {e}")

        # 降级到本地文件
        data = _load_linkage()
        return data["links"].get(ts_code, [])

    def _flag_skill_for_review(self, skill_name: str, reason: str = "") -> bool:
        """
        标记技能为"待复审"状态。
        在技能 Markdown 文件头部添加复审标记，并在联动文件中记录。
        """
        skill_path = APPROVED_DIR / f"{skill_name}.md"
        if not skill_path.exists():
            logger.warning(f"技能文件不存在，无法标记复审: {skill_path}")
            return False

        content = skill_path.read_text(encoding="utf-8")

        # 检查是否已有复审标记
        if "<!-- TAMF_REVIEW_PENDING" in content:
            logger.debug(f"技能 {skill_name} 已有复审标记，跳过")
            return False

        # 在文件头部（第一个 --- 之后）添加复审标记
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        review_tag = f"\n<!-- TAMF_REVIEW_PENDING: {now_str} — {reason} -->\n"

        # 在文件开头插入标记（在 --- 标题分隔符后）
        first_hr = content.find("\n---")
        if first_hr > 0:
            content = content[:first_hr + 4] + review_tag + content[first_hr + 4:]
        else:
            content = review_tag + content

        skill_path.write_text(content, encoding="utf-8")
        logger.info(f"技能已标记复审: {skill_name} — {reason}")

        # 记录到联动文件
        data = _load_linkage()
        data.setdefault("pending_reviews", [])
        data["pending_reviews"].append({
            "skill_name": skill_name,
            "reason": reason,
            "flagged_at": now_str,
        })
        _save_linkage(data)

        return True

    def on_tamf_fundamental_change(self, ts_code: str) -> list[str]:
        """
        标的基本面发生重大变化时，自动标记关联技能为"待复审"。
        供 TAMF 深度分析或基本面更新时调用。

        Returns:
            [被标记复审的技能名称列表]
        """
        linked_skills = self._get_linked_skills(ts_code)
        if not linked_skills:
            logger.debug(f"TAMF联动: {ts_code} 无关联技能")
            return []

        flagged = []
        reason = f"{ts_code} 基本面重大变化，关联技能可能需要调整策略参数"

        for skill_name in linked_skills:
            if self._flag_skill_for_review(skill_name, reason):
                flagged.append(skill_name)

        if flagged:
            logger.info(f"TAMF联动: {ts_code} 基本面变化 → {len(flagged)} 个技能标记复审: {flagged}")

        return flagged

    def list_pending_reviews(self) -> list[dict]:
        """列出所有待复审的技能"""
        data = _load_linkage()
        return data.get("pending_reviews", [])

    def clear_review_flag(self, skill_name: str) -> bool:
        """清除技能的复审标记"""
        skill_path = APPROVED_DIR / f"{skill_name}.md"
        if not skill_path.exists():
            return False

        content = skill_path.read_text(encoding="utf-8")
        import re
        content = re.sub(
            r'\n<!--\s*TAMF_REVIEW_PENDING:.*?-->\n',
            '',
            content,
            flags=re.DOTALL
        )
        skill_path.write_text(content, encoding="utf-8")

        # 清除联动文件中的记录
        data = _load_linkage()
        data["pending_reviews"] = [
            r for r in data.get("pending_reviews", [])
            if r.get("skill_name") != skill_name
        ]
        _save_linkage(data)

        logger.info(f"复审标记已清除: {skill_name}")
        return True

    def list_all_linkages(self) -> dict:
        """列出所有技能-标的关联关系"""
        data = _load_linkage()
        return {
            "links": data.get("links", {}),
            "pending_reviews": len(data.get("pending_reviews", [])),
        }


# ─── 便捷入口 ────────────────────────────────────────────────

def on_fundamental_change_detected(ts_code: str) -> list[str]:
    """
    便捷函数：检测到标的基本面变化时调用。
    TAMF 深度分析或增量更新在发现季报/年报发布后调用此函数。
    """
    linkage = SkillTAMFLinkage()
    return linkage.on_tamf_fundamental_change(ts_code)


def bulk_link_target_skills(mappings: dict[str, list[str]]) -> dict:
    """
    批量建立技能-标的关联。
    mappings: {ts_code: [skill_name1, skill_name2, ...]}
    """
    linkage = SkillTAMFLinkage()
    results = {"success": 0, "failed": 0}
    for ts_code, skills in mappings.items():
        for skill_name in skills:
            if linkage.link_skill_to_target(skill_name, ts_code):
                results["success"] += 1
            else:
                results["failed"] += 1
    return results


# ─── 主入口 ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    linkage = SkillTAMFLinkage()

    if len(sys.argv) > 1 and sys.argv[1] == "list":
        print("=== 技能-标的联动 ===")
        print(json.dumps(linkage.list_all_linkages(), ensure_ascii=False, indent=2))

    elif len(sys.argv) > 1 and sys.argv[1] == "pending":
        reviews = linkage.list_pending_reviews()
        print(f"待复审技能: {len(reviews)} 个")
        for r in reviews:
            print(f"  - {r['skill_name']}: {r['reason']} ({r['flagged_at']})")

    elif len(sys.argv) >= 3 and sys.argv[1] == "link":
        # python skill_tamf_linkage.py link 000977 position_check
        code = sys.argv[2]
        skill = sys.argv[3] if len(sys.argv) > 3 else "position_check"
        ok = linkage.link_skill_to_target(skill, code)
        print(f"联动 {'成功' if ok else '失败'}: {skill} ↔ {code}")

    elif len(sys.argv) >= 2 and sys.argv[1] == "clear":
        skill = sys.argv[2] if len(sys.argv) > 2 else ""
        if skill:
            linkage.clear_review_flag(skill)
            print(f"已清除: {skill}")

    else:
        # 默认：演示基本面变化触发
        print("=== TAMF联动演示 ===")
        test_code = sys.argv[1] if len(sys.argv) > 1 else "000977"
        flagged = on_fundamental_change_detected(test_code)
        print(f"触发基本面变化: {test_code} → 标记 {len(flagged)} 个技能复审")
        for f in flagged:
            print(f"  - {f}")