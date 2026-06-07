"""
tamf_git_commit.py — TAMF文件的Git自动提交
每日15:35 TAMF增量更新后自动调用，将变更文件提交到Git仓库。

用法（独立运行）:
    python tamf_git_commit.py [--dry-run]

用法（从 schedule_runner 调用）:
    from tamf_git_commit import commit_tamf_changes
    result = commit_tamf_changes()
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TAMF_DIR = PROJECT_ROOT / "data" / "target_memories"


def get_git_changes() -> dict:
    """检查Git工作区状态，返回变更文件字典"""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", str(TAMF_DIR)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = result.stdout.strip().split("\n")
        changed = []
        for line in lines:
            if line.strip():
                # porcelain format: XY filename, X=staged, Y=worktree
                status = line[:2]
                filepath = line[3:].strip()
                if filepath.endswith(".md"):
                    changed.append({"status": status.strip(), "path": filepath})
        return {"changed": changed, "exit_code": result.returncode}
    except Exception as e:
        return {"changed": [], "exit_code": -1, "error": str(e)}


def commit_tamf_changes(dry_run: bool = False, auto_message: bool = True) -> dict:
    """
    主函数：将变更的TAMF文件提交到Git。
    
    Args:
        dry_run: True=只检查不提交
        auto_message: True=自动生成提交信息
    
    Returns:
        dict: {"committed": bool, "count": int, "message": str}
    """
    changes = get_git_changes()
    
    if "error" in changes:
        return {"committed": False, "count": 0, "message": f"Git错误: {changes['error']}"}
    
    changed_files = changes["changed"]
    
    if not changed_files:
        return {"committed": False, "count": 0, "message": "无TAMF文件变更，无需提交"}
    
    count = len(changed_files)
    
    if dry_run:
        file_list = "\n".join([f"  {c['status']} {c['path']}" for c in changed_files])
        return {
            "committed": False,
            "count": count,
            "message": f"[DRY RUN] 将提交 {count} 个文件:\n{file_list}",
        }
    
    # git add 所有变更的 .md 文件
    file_paths = [str(TAMF_DIR / c["path"]) for c in changed_files]
    try:
        subprocess.run(
            ["git", "add", "--"] + file_paths,
            cwd=str(PROJECT_ROOT),
            check=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        return {"committed": False, "count": 0, "message": f"git add失败: {e}"}
    
    # 自动生成提交信息
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 分类变更
    [c["path"] for c in changed_files if c["status"].startswith("?")]
    modified = [c["path"] for c in changed_files if c["status"] not in ("??", "A ", "??M")]
    modified = [c["path"] for c in changed_files if c["status"] not in ["??", "A "]]
    modified = [c for c in changed_files if c["status"] not in ["??", "A "]]
    new_files = [c["path"] for c in changed_files if c["status"] in ["A ", "??"]]
    
    # 生成消息
    if new_files and not modified:
        msg = f" TAMF新标的初始化 ({today})\n新增 {len(new_files)} 个标的"
    elif modified and not new_files:
        msg = f" TAMF每日增量更新 ({today})\n更新 {count} 个标的"
    else:
        msg = f" TAMF文件变更 ({today})\n更新 {count} 个文件 (新增{len(new_files)}/修改{len(modified)})"  # noqa: E501
    
    try:
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(PROJECT_ROOT),
            check=True,
            timeout=10,
        )
        return {"committed": True, "count": count, "message": f"✅ 提交成功: {msg}"}
    except subprocess.CalledProcessError as e:
        return {"committed": False, "count": 0, "message": f"git commit失败: {e}"}


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    result = commit_tamf_changes(dry_run=dry_run)
    print(result["message"])
    sys.exit(0 if result["committed"] or result["count"] == 0 else 1)
