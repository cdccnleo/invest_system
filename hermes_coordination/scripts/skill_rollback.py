"""
skill_rollback.py — Skill 版本回滚机制 (P2-T4 补丁9 落地 v1.0)

功能:
- patch 之前自动备份 SKILL.md 到 ~/.hermes/backups/skills/<date>/<skill>/
- patch 之后自动验证（skill_view 检查核心字段）
- 验证失败 → 自动 git revert 或从备份还原
- 备份保留 30 天滚动

使用:
    from skill_rollback import SkillBackup
    sb = SkillBackup(skill_name="hermes-investpilot-coordination-v2")
    backup_path = sb.backup()
    # ... patch 操作 ...
    if not sb.verify():
        sb.rollback()
"""

import hashlib
import logging
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("skill_rollback")

HERMES_SKILLS_DIR = Path("/home/aileo/.hermes/skills")
BACKUP_ROOT = Path.home() / ".hermes" / "backups" / "skills"
RETENTION_DAYS = 30


class SkillRollbackError(Exception):
    pass


class SkillBackup:
    """单 skill 的备份/回滚"""

    def __init__(self, skill_name: str, backup_root: Path = BACKUP_ROOT):
        self.skill_name = skill_name
        self.backup_root = Path(backup_root)
        # skill_name 可能带子目录前缀（如 investing/hermes-x）
        # 也可能直接是 dir 名
        # 先尝试直接路径
        candidates = [
            HERMES_SKILLS_DIR / skill_name,  # 直接子目录
        ]
        # 再尝试 rglob（深一层）
        candidates.extend(HERMES_SKILLS_DIR.rglob(skill_name))
        # 也支持 "category/skill" 格式
        for sub in HERMES_SKILLS_DIR.iterdir():
            if sub.is_dir():
                candidates.append(sub / skill_name)

        for c in candidates:
            if c.exists() and c.is_dir():
                self.skill_dir = c
                return
        raise SkillRollbackError(f"skill dir not found: {skill_name} (tried {len(candidates)} paths)")

    def _backup_dir(self) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        ts = datetime.now().strftime("%H%M%S")
        d = self.backup_root / today / f"{self.skill_name}_{ts}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def backup(self) -> Path:
        """备份当前 SKILL.md"""
        target = self._backup_dir()
        for f in ["SKILL.md"]:
            src = self.skill_dir / f
            if src.exists():
                shutil.copy2(src, target / f)
        # 同时备份 references/ 和 scripts/ 下变更的文件
        for sub in ["references", "scripts"]:
            src = self.skill_dir / sub
            if src.exists():
                shutil.copytree(src, target / sub, dirs_exist_ok=True)
        # 记录 sha256
        skill_md = self.skill_dir / "SKILL.md"
        if skill_md.exists():
            sha = hashlib.sha256(skill_md.read_bytes()).hexdigest()
            (target / "sha256.txt").write_text(sha)
        logger.info(f"备份完成: {target}")
        return target

    def rollback(self, backup_path: Path) -> bool:
        """从指定备份还原"""
        if not backup_path.exists():
            raise SkillRollbackError(f"backup not found: {backup_path}")
        # 还原 SKILL.md
        src = backup_path / "SKILL.md"
        if src.exists():
            shutil.copy2(src, self.skill_dir / "SKILL.md")
        # 还原 references/ scripts/
        for sub in ["references", "scripts"]:
            src = backup_path / sub
            if src.exists():
                target = self.skill_dir / sub
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(src, target)
        logger.info(f"已从 {backup_path} 还原")
        return True

    def verify(self) -> bool:
        """验证 skill 是否完整（核心字段检查）"""
        skill_md = self.skill_dir / "SKILL.md"
        if not skill_md.exists():
            logger.error(f"SKILL.md 不存在")
            return False
        content = skill_md.read_text(encoding="utf-8")
        # 检查 frontmatter
        if not content.startswith("---"):
            logger.error("缺少 frontmatter")
            return False
        # 检查关键字段
        if "name:" not in content[:500]:
            logger.error("frontmatter 缺少 name")
            return False
        if "description:" not in content[:1000]:
            logger.error("frontmatter 缺少 description")
            return False
        logger.info(f"验证通过: {self.skill_name}")
        return True


class SkillBackupManager:
    """多 skill 的备份管理（保留 30 天滚动）"""

    def __init__(self, backup_root: Path = BACKUP_ROOT, retention_days: int = RETENTION_DAYS):
        self.backup_root = Path(backup_root)
        self.retention_days = retention_days
        self.backup_root.mkdir(parents=True, exist_ok=True)

    def cleanup_old_backups(self) -> int:
        """清理超期备份"""
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        removed = 0
        if not self.backup_root.exists():
            return 0
        for date_dir in self.backup_root.iterdir():
            if not date_dir.is_dir():
                continue
            try:
                dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
            except ValueError:
                continue
            if dir_date < cutoff:
                shutil.rmtree(date_dir)
                removed += 1
                logger.info(f"已清理超期备份: {date_dir.name}")
        return removed

    def list_backups(self, skill_name: Optional[str] = None) -> List[Path]:
        """列出备份（按时间倒序）

        Args:
            skill_name: skill 名, None 表示列全部
        """
        if not self.backup_root.exists():
            return []
        result = []
        for date_dir in sorted(self.backup_root.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for b in sorted(date_dir.iterdir(), reverse=True):
                # ⚠️ PIT #12 修复: skill_name=None 时列全部
                if skill_name is None:
                    result.append(b)
                elif b.name.startswith(skill_name + "_"):
                    result.append(b)
        return result

    def list_all_backups(self) -> List[Dict]:
        """列出所有备份（含元数据）

        Returns:
            [{"path": Path, "skill_name": str, "date": str, "size_bytes": int}, ...]
        """
        result = []
        for b in self.list_backups(skill_name=None):
            # 解析 <skill_name>_<HHMMSS>
            name = b.name
            if "_" in name:
                skill_name = name.rsplit("_", 1)[0]
            else:
                skill_name = name
            date_str = b.parent.name
            size = sum(f.stat().st_size for f in b.rglob('*') if f.is_file())
            result.append({
                "path": b,
                "skill_name": skill_name,
                "date": date_str,
                "size_bytes": size,
            })
        return result

    def get_latest_backup(self, skill_name: str) -> Optional[Path]:
        backups = self.list_backups(skill_name)
        return backups[0] if backups else None

    def auto_patch(self, skill_name: str, patch_callback) -> bool:
        """带备份的自动 patch

        Args:
            skill_name: skill 名
            patch_callback: callable() -> bool，执行实际 patch 并返回是否成功

        Returns:
            True: patch 成功（保留）
            False: patch 失败（已自动回滚）
        """
        sb = SkillBackup(skill_name)
        backup_path = sb.backup()

        try:
            # 1. 执行 patch
            logger.info(f"开始 patch {skill_name}")
            success = patch_callback()
            if not success:
                raise SkillRollbackError("patch_callback 返回 False")

            # 2. 验证
            if not sb.verify():
                raise SkillRollbackError("patch 后验证失败")

            logger.info(f"patch 成功: {skill_name} | 备份: {backup_path}")
            return True

        except Exception as e:
            logger.error(f"patch 失败: {e} — 自动回滚")
            try:
                sb.rollback(backup_path)
                logger.info(f"已回滚: {skill_name}")
            except Exception as re:
                logger.error(f"回滚失败: {re}")
                raise
            return False

    def git_rollback(self, skill_path: Path) -> bool:
        """用 git revert 回滚（如果 skill 在 git 仓库中）"""
        if not (skill_path / ".git").exists() and not (skill_path.parent / ".git").exists():
            logger.warning("skill 不在 git 仓库中，无法 git revert")
            return False
        try:
            result = subprocess.run(
                ["git", "revert", "--no-edit", "HEAD"],
                cwd=skill_path.parent if (skill_path.parent / ".git").exists() else skill_path,
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                logger.info(f"git revert 成功: {skill_path}")
                return True
            else:
                logger.error(f"git revert 失败: {result.stderr[:200]}")
                return False
        except Exception as e:
            logger.error(f"git revert 异常: {e}")
            return False


def main():
    """演示备份/回滚流程"""
    print("=" * 70)
    print("Skill 备份/回滚演示")
    print("=" * 70)

    mgr = SkillBackupManager()

    # 1. 备份当前状态
    print("\n--- 步骤 1: 备份当前 v2.0 SKILL.md ---")
    sb = SkillBackup("hermes-investpilot-coordination-v2")
    backup_path = sb.backup()
    print(f"  备份路径: {backup_path}")
    print(f"  备份大小: {sum(f.stat().st_size for f in backup_path.rglob('*') if f.is_file())}B")

    # 2. 模拟 patch 失败 → 自动回滚
    print("\n--- 步骤 2: 模拟 patch 失败 → 自动回滚 ---")
    def bad_patch():
        # 模拟破坏文件
        skill_md = HERMES_SKILLS_DIR / "hermes-investpilot-coordination-v2" / "SKILL.md"
        # 不真破坏，写个临时文件验证流程
        test_file = Path("/tmp/skill_patch_test.txt")
        test_file.write_text("patched content")
        # 返回 True 但 verify 会失败（因为我们没真改 SKILL.md 但假设改了）
        return True  # 实际项目里这会返回 True 然后由 verify() 检测

    # 实际演示：用 bad_patch 模拟，写一个明显坏的 SKILL.md
    def bad_patch_real():
        skill_md = HERMES_SKILLS_DIR / "hermes-investpilot-coordination-v2" / "SKILL.md"
        skill_md.write_text("this is broken - no frontmatter")
        return True

    success = mgr.auto_patch("hermes-investpilot-coordination-v2", bad_patch_real)
    print(f"  patch 结果: {'✓ 成功' if success else '✗ 失败（已回滚）'}")
    # 此时 SKILL.md 应已恢复
    if success:
        print("  ⚠ 警告：不应该成功")
    else:
        # 验证回滚成功 - 用 investing/ 子目录路径
        skill_md = HERMES_SKILLS_DIR / "investing" / "hermes-investpilot-coordination-v2" / "SKILL.md"
        if skill_md.exists():
            first_line = skill_md.read_text()[:50]
            print(f"  SKILL.md 首行（应已恢复）: {first_line}")
        else:
            print(f"  ✗ 仍不存在: {skill_md}")

    # 3. 列出所有备份
    print("\n--- 步骤 3: 列出所有备份 ---")
    backups = mgr.list_backups("hermes-investpilot-coordination-v2")
    print(f"  共 {len(backups)} 个备份:")
    for b in backups[:3]:
        size = sum(f.stat().st_size for f in b.rglob('*') if f.is_file())
        print(f"    {b.name}: {size}B")

    # 4. 清理超期备份
    print("\n--- 步骤 4: 清理超期备份（演示用，不实际清理）---")
    print("  跳过（保留所有备份）")


if __name__ == "__main__":
    main()
