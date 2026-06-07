"""
remote_backup.py — 灾备异地备份模块
支持将数据库备份文件同步到本地备用路径或云存储
避免单点故障导致的备份数据丢失
"""

import logging
import shutil
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger("invest_system.remote_backup")

BACKUP_DIR = Path(__file__).parent.parent / "backups"
REMOTE_DIRS = [
    Path("D:/Backup/investpilot"),  # 本地第二磁盘
    Path("E:/Backup/investpilot"),  # 外接硬盘（如果存在）
]

MAX_BACKUP_AGE_DAYS = 30
MAX_REMOTE_BACKUPS = 20


def _ensure_remote_dir(remote_path: Path) -> bool:
    """
    确保远程备份目录存在

    Args:
        remote_path: 远程路径

    Returns:
        目录是否可用
    """
    try:
        remote_path.mkdir(parents=True, exist_ok=True)
        test_file = remote_path / ".write_test"
        test_file.write_text("test")
        test_file.unlink()
        return True
    except Exception as e:
        logger.warning(f"远程目录不可用 {remote_path}: {e}")
        return False


def get_available_remote_dirs() -> list[Path]:
    """
    获取所有可用的远程备份目录

    Returns:
        可用目录列表
    """
    available = []
    for d in REMOTE_DIRS:
        if _ensure_remote_dir(d):
            available.append(d)
    return available


def sync_to_remote(source_path: Path, remote_path: Path) -> bool:
    """
    将备份文件同步到远程目录

    Args:
        source_path: 源文件路径
        remote_path: 目标目录

    Returns:
        是否同步成功
    """
    if not source_path.exists():
        logger.warning(f"源文件不存在: {source_path}")
        return False

    dest = remote_path / source_path.name
    try:
        shutil.copy2(source_path, dest)
        logger.info(f"异地备份成功: {source_path.name} -> {remote_path}")
        return True
    except Exception as e:
        logger.warning(f"异地备份失败: {e}")
        return False


def sync_all_recent_backups(days: int = 7) -> dict:
    """
    同步最近 N 天的所有备份到异地

    Args:
        days: 同步天数

    Returns:
        {"synced": int, "failed": int, "destinations": int}
    """
    if not BACKUP_DIR.exists():
        return {"synced": 0, "failed": 0, "destinations": 0}

    cutoff = datetime.now() - timedelta(days=days)
    recent_files = [
        f for f in BACKUP_DIR.glob("*.dump")
        if datetime.fromtimestamp(f.stat().st_mtime) > cutoff
    ]

    if not recent_files:
        logger.info("无近期备份需要同步")
        return {"synced": 0, "failed": 0, "destinations": 0}

    remotes = get_available_remote_dirs()
    if not remotes:
        logger.warning("无可用的远程备份目录")
        return {"synced": 0, "failed": 0, "destinations": 0}

    synced = 0
    failed = 0
    for f in recent_files:
        for remote in remotes:
            if sync_to_remote(f, remote):
                synced += 1
            else:
                failed += 1

    return {
        "synced": synced,
        "failed": failed,
        "destinations": len(remotes),
        "files": len(recent_files),
    }


def cleanup_remote_backups():
    """清理过期的远程备份文件"""
    remotes = get_available_remote_dirs()
    cutoff = datetime.now() - timedelta(days=MAX_BACKUP_AGE_DAYS)

    cleaned = 0
    for remote in remotes:
        if not remote.exists():
            continue
        files = sorted(remote.glob("*.dump"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                try:
                    f.unlink()
                    cleaned += 1
                except OSError:
                    pass
            elif len([x for x in files if datetime.fromtimestamp(x.stat().st_mtime) >= cutoff]) > MAX_REMOTE_BACKUPS:
                try:
                    f.unlink()
                    cleaned += 1
                except OSError:
                    pass

    if cleaned > 0:
        logger.info(f"已清理 {cleaned} 个过期远程备份")


def get_backup_status() -> dict:
    """
    获取备份状态概览

    Returns:
        {
            "local_backups": int,
            "local_size_mb": float,
            "remote_status": {path: {"backups": int, "size_mb": float, "available": bool}}
        }
    """
    status = {"local_backups": 0, "local_size_mb": 0, "remote_status": {}}

    if BACKUP_DIR.exists():
        local_files = list(BACKUP_DIR.glob("*.dump"))
        status["local_backups"] = len(local_files)
        status["local_size_mb"] = round(
            sum(f.stat().st_size for f in local_files) / (1024 * 1024), 2
        )

    for remote in REMOTE_DIRS:
        available = remote.exists() and any(remote.iterdir()) if remote.exists() else False
        remote_files = list(remote.glob("*.dump")) if remote.exists() else []
        status["remote_status"][str(remote)] = {
            "backups": len(remote_files),
            "size_mb": round(
                sum(f.stat().st_size for f in remote_files) / (1024 * 1024), 2
            ) if remote_files else 0,
            "available": available,
        }

    return status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== 灾备状态 ===")
    status = get_backup_status()
    print(f"本地备份: {status['local_backups']} 个 ({status['local_size_mb']}MB)")
    for path, info in status["remote_status"].items():
        icon = "✅" if info["available"] else "❌"
        print(f"  {icon} {path}: {info['backups']} 个 ({info['size_mb']}MB)")

    result = sync_all_recent_backups(days=7)
    print(f"\n同步结果: {result['synced']} 成功, {result['failed']} 失败")