"""
tamf_shadow.py - TAMF 影子模式

在正式写入生产 TAMF 文件前，增量更新先写入隔离的影子目录。
经过 N 个交易日验证通过后，人工或自动晋升到生产目录。

工作流:
  1. SHADOW: 增量更新 → 写入 data/target_memories_shadow/{code}.md
  2. DIFF:  对比影子版本与生产版本的差异
  3. PROMOTE: 验证通过后，将影子文件晋升到生产目录
  4. ROLLBACK: 发现问题时，回滚影子目录

安全保证:
  - 影子写入绝不触达生产目录
  - 晋升前生产文件自动打时间戳备份
  - 差异报告可读、可审计
"""

import difflib
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tamf_shadow")

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TAMF_DIR = PROJECT_ROOT / "data" / "target_memories"
SHADOW_DIR = PROJECT_ROOT / "data" / "target_memories_shadow"
SHADOW_STATE_FILE = SHADOW_DIR / "shadow_state.json"


def _load_state() -> dict:
    """加载影子模式运行状态"""
    if SHADOW_STATE_FILE.exists():
        with open(SHADOW_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "enabled": False,
        "cycle_count": 0,
        "promoted_count": 0,
        "last_run": None,
        "holdings_shadowed": [],
        "promotion_history": [],
    }


def _save_state(state: dict) -> None:
    """持久化影子模式运行状态"""
    SHADOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = datetime.now().isoformat()
    with open(SHADOW_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


class TamfShadowMode:
    """
    TAMF 影子模式 — 增量更新先写入影子目录，
    人工或自动化验证后再晋升到生产目录。
    """

    def __init__(self, enabled: bool = None):
        """
        初始化影子模式。
        若 enabled=None，则从 shadow_state.json 读取上一次状态。
        """
        state = _load_state()
        if enabled is not None:
            self.enabled = enabled
            state["enabled"] = enabled
            _save_state(state)
        else:
            self.enabled = state.get("enabled", False)
        self.state = state

    # ═══════════════════════════════════════════════════════════
    # 影子写入
    # ═══════════════════════════════════════════════════════════

    def shadow_write(self, code: str, content: str) -> str:
        """
        将更新内容写入影子目录（而非生产目录）。
        每次写入都覆盖上一次的影子文件。

        Returns:
            影子文件的绝对路径
        """
        SHADOW_DIR.mkdir(parents=True, exist_ok=True)
        shadow_path = SHADOW_DIR / f"{code}.md"
        shadow_path.write_text(content, encoding="utf-8")

        if code not in self.state["holdings_shadowed"]:
            self.state["holdings_shadowed"].append(code)
            _save_state(self.state)

        logger.info(f"影子写入: {code} → {shadow_path}")
        return str(shadow_path)

    # ═══════════════════════════════════════════════════════════
    # 差异对比
    # ═══════════════════════════════════════════════════════════

    def diff_with_production(self, code: str) -> dict:
        """
        对比影子版本与生产版本的差异。

        Returns:
            {
                "code": code,
                "has_diff": bool,
                "diff_lines": int,
                "diff_text": str,   # unified diff 可读格式
                "shadow_exists": bool,
                "production_exists": bool,
            }
        """
        result = {
            "code": code,
            "has_diff": False,
            "diff_lines": 0,
            "diff_text": "",
            "shadow_exists": False,
            "production_exists": False,
        }

        shadow_path = SHADOW_DIR / f"{code}.md"
        prod_path = TAMF_DIR / f"{code}.md"

        result["shadow_exists"] = shadow_path.exists()
        result["production_exists"] = prod_path.exists()

        if not shadow_path.exists():
            result["diff_text"] = "[影子文件不存在]"
            return result

        shadow_content = shadow_path.read_text(encoding="utf-8").splitlines(keepends=True)
        prod_content = []
        if prod_path.exists():
            prod_content = prod_path.read_text(encoding="utf-8").splitlines(keepends=True)

        diff_lines = list(difflib.unified_diff(
            prod_content, shadow_content,
            fromfile=f"{{生产}} {code}.md",
            tofile=f"{{影子}} {code}.md",
            lineterm="",
        ))

        if diff_lines:
            result["has_diff"] = True
            result["diff_lines"] = len(diff_lines)
            result["diff_text"] = "\n".join(diff_lines)

        return result

    # ═══════════════════════════════════════════════════════════
    # 晋升机制
    # ═══════════════════════════════════════════════════════════

    def promote(self, code: str) -> dict:
        """
        将影子文件晋升为生产文件。
        晋升前自动打时间戳备份当前生产文件。

        Returns:
            {"code": code, "promoted": bool, "backup_path": str|None}
        """
        shadow_path = SHADOW_DIR / f"{code}.md"
        prod_path = TAMF_DIR / f"{code}.md"

        if not shadow_path.exists():
            logger.warning(f"晋升失败: 影子文件不存在 {code}")
            return {"code": code, "promoted": False, "backup_path": None}

        # 备份当前生产文件
        backup_path = None
        if prod_path.exists():
            backup_dir = SHADOW_DIR / "promotion_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = str(backup_dir / f"{code}_{ts}.md")
            shutil.copy2(prod_path, backup_path)

        # 晋升
        prod_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shadow_path, prod_path)

        # 清理影子文件
        shadow_path.unlink()

        # 更新状态
        if code in self.state["holdings_shadowed"]:
            self.state["holdings_shadowed"].remove(code)
        self.state["promoted_count"] += 1
        self.state.setdefault("promotion_history", [])
        self.state["promotion_history"].append({
            "code": code,
            "promoted_at": datetime.now().isoformat(),
            "backup": backup_path,
        })
        _save_state(self.state)

        logger.info(f"影子晋升: {code} → 生产目录 (备份: {backup_path})")
        return {"code": code, "promoted": True, "backup_path": backup_path}

    # ═══════════════════════════════════════════════════════════
    # 回滚
    # ═══════════════════════════════════════════════════════════

    def rollback_shadow(self, code: str) -> bool:
        """清空指定标的的影子文件"""
        shadow_path = SHADOW_DIR / f"{code}.md"
        if shadow_path.exists():
            shadow_path.unlink()
            if code in self.state["holdings_shadowed"]:
                self.state["holdings_shadowed"].remove(code)
                _save_state(self.state)
            logger.info(f"影子回滚: {code} 影子文件已清除")
            return True
        return False

    def rollback_all(self) -> int:
        """清空所有影子文件"""
        count = 0
        for code in list(self.state.get("holdings_shadowed", [])):
            if self.rollback_shadow(code):
                count += 1
        return count

    # ═══════════════════════════════════════════════════════════
    # 批量操作
    # ═══════════════════════════════════════════════════════════

    def diff_all(self) -> list[dict]:
        """对比所有已影子化的标的"""
        results = []
        for code in self.state.get("holdings_shadowed", []):
            results.append(self.diff_with_production(code))
        return results

    def promote_all(self) -> dict:
        """
        批量晋升所有已验证的影子文件。

        Returns:
            {"total": int, "promoted": int, "failed": int, "details": [...]}
        """
        results = {"total": 0, "promoted": 0, "failed": 0, "details": []}
        for code in list(self.state.get("holdings_shadowed", [])):
            results["total"] += 1
            r = self.promote(code)
            if r["promoted"]:
                results["promoted"] += 1
            else:
                results["failed"] += 1
            results["details"].append(r)
        return results

    # ═══════════════════════════════════════════════════════════
    # 状态报告
    # ═══════════════════════════════════════════════════════════

    def status_report(self) -> dict:
        """生成影子模式运行状态报告"""
        shadow_files = list(SHADOW_DIR.glob("*.md")) if SHADOW_DIR.exists() else []
        total_shadowed = len(shadow_files)

        # 汇总差异信息
        diffs = self.diff_all()
        with_diff = sum(1 for d in diffs if d["has_diff"])
        total_diff_lines = sum(d["diff_lines"] for d in diffs)

        return {
            "enabled": self.enabled,
            "cycle_count": self.state.get("cycle_count", 0),
            "promoted_count": self.state.get("promoted_count", 0),
            "last_run": self.state.get("last_run"),
            "holdings_shadowed": self.state.get("holdings_shadowed", []),
            "total_shadow_files": total_shadowed,
            "with_differences": with_diff,
            "total_diff_lines": total_diff_lines,
            "promotion_history": self.state.get("promotion_history", [])[-5:],
        }

    def enable(self) -> None:
        """激活影子模式"""
        self.enabled = True
        self.state["enabled"] = True
        _save_state(self.state)
        logger.info("影子模式已激活")

    def disable(self) -> None:
        """停用影子模式"""
        self.enabled = False
        self.state["enabled"] = False
        _save_state(self.state)
        logger.info("影子模式已停用")


# ═══════════════════════════════════════════════════════════════
# 便捷入口 — 供 schedule_runner 和 tamf_updater 调用
# ═══════════════════════════════════════════════════════════════

def is_shadow_mode_active() -> bool:
    """快速检查影子模式是否激活"""
    state = _load_state()
    return state.get("enabled", False)


def tamf_shadow_write(code: str, content: str) -> Optional[str]:
    """
    影子安全写入：若影子模式激活，写入影子目录并返回路径；
    否则返回 None，由调用方走正常生产写入路径。
    """
    if not is_shadow_mode_active():
        return None
    shadow = TamfShadowMode()
    return shadow.shadow_write(code, content)


# ─── 主入口 ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    shadow = TamfShadowMode()

    if len(sys.argv) < 2:
        print("用法:")
        print("  python tamf_shadow.py status         查看影子模式状态")
        print("  python tamf_shadow.py enable         激活影子模式")
        print("  python tamf_shadow.py disable        停用影子模式")
        print("  python tamf_shadow.py diff [code]    对比差异（不指定code则全部）")
        print("  python tamf_shadow.py promote <code> 晋升指定标的")
        print("  python tamf_shadow.py promote-all    晋升全部已验证标的")
        print("  python tamf_shadow.py rollback [code] 回滚影子文件")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "status":
        report = shadow.status_report()
        print(json.dumps(report, ensure_ascii=False, indent=2))

    elif cmd == "enable":
        shadow.enable()
        print("✅ 影子模式已激活 — 所有TAMF更新将先写入影子目录")

    elif cmd == "disable":
        shadow.disable()
        print("✅ 影子模式已停用 — TAMF更新恢复正常生产写入")

    elif cmd == "diff":
        if len(sys.argv) > 2:
            r = shadow.diff_with_production(sys.argv[2])
            if r["has_diff"]:
                print(f"🔍 {sys.argv[2]} 有差异 ({r['diff_lines']}行):\n")
                print(r["diff_text"])
            else:
                print(f"✅ {sys.argv[2]} 无差异" if r["shadow_exists"] else f"⚠️ {sys.argv[2]} 无影子文件")  # noqa: E501
        else:
            diffs = shadow.diff_all()
            for d in diffs:
                status = "🔴 有差异" if d["has_diff"] else "✅ 无差异" if d["shadow_exists"] else "⚠️ 无影子文件"  # noqa: E501
                detail = f" ({d['diff_lines']}行)" if d["has_diff"] else ""
                print(f"{status} {d['code']}{detail}")
            if not diffs:
                print("无已影子化的标的")

    elif cmd == "promote":
        if len(sys.argv) > 2:
            r = shadow.promote(sys.argv[2])
            print(f"{'✅' if r['promoted'] else '❌'} 晋升 {'成功' if r['promoted'] else '失败'}: {sys.argv[2]}")  # noqa: E501
        else:
            print("用法: python tamf_shadow.py promote <code>")

    elif cmd == "promote-all":
        print("⚠️ 将晋升全部影子文件到生产目录，是否继续？(y/N)")
        confirm = input().strip().lower()
        if confirm == "y":
            result = shadow.promote_all()
            print(f"✅ 晋升完成: {result['promoted']}成功 / {result['failed']}失败 (共{result['total']}只)")  # noqa: E501

    elif cmd == "rollback":
        if len(sys.argv) > 2:
            shadow.rollback_shadow(sys.argv[2])
            print(f"✅ 已回滚: {sys.argv[2]}")
        else:
            count = shadow.rollback_all()
            print(f"✅ 已回滚全部 {count} 个影子文件")

    else:
        print(f"未知命令: {cmd}")