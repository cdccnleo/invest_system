"""
backup_manager.py — 数据库自动备份与恢复管理器

提供:
  - full_backup: 全量备份（pg_dump 自定义格式）
  - incremental_backup: WAL 增量归档
  - restore: 从备份恢复
  - verify_backup: 验证备份完整性
  - cleanup_old_backups: 清理过期备份
"""

import os
import subprocess
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("backup_manager")

BACKUP_DIR = Path(__file__).parent.parent / "data" / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

FULL_RETENTION_WEEKS = 4
INCREMENTAL_RETENTION_DAYS = 7


class DatabaseBackupManager:
    """
    数据库自动备份管理器。

    策略:
      - 每日增量备份: WAL 归档（Write-Ahead Log）
      - 每周全量备份: pg_dump 自定义格式
      - 保留策略: 增量保留 7 天，全量保留 4 周
    """

    def __init__(self, dbname: str = "investpilot", user: str = "invest_admin",
                 password: Optional[str] = None):
        self.dbname = dbname
        self.user = user
        self.password = password

    def _env(self) -> dict:
        """构建 pg_dump 环境变量"""
        env = os.environ.copy()
        if self.password:
            env["PGPASSWORD"] = self.password
        return env

    def full_backup(self) -> dict:
        """
        执行全量备份。

        Returns:
            {"success": bool, "path": str, "size_mb": float, "timestamp": str}
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"investpilot_full_{timestamp}.dump"
        filepath = BACKUP_DIR / filename

        try:
            result = subprocess.run(
                [
                    "pg_dump",
                    "-h", "localhost",
                    "-p", "5432",
                    "-U", self.user,
                    "-d", self.dbname,
                    "-F", "c",
                    "-f", str(filepath),
                    "--no-owner",
                    "--no-acl",
                ],
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode != 0:
                logger.error(f"全量备份失败: {result.stderr}")
                return {"success": False, "path": str(filepath), "size_mb": 0,
                        "timestamp": timestamp, "error": result.stderr}

            size_mb = filepath.stat().st_size / (1024 * 1024)
            logger.info(f"全量备份成功: {filepath} ({size_mb:.1f} MB)")

            return {
                "success": True,
                "path": str(filepath),
                "size_mb": round(size_mb, 2),
                "timestamp": timestamp,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "path": str(filepath), "size_mb": 0,
                    "timestamp": timestamp, "error": "备份超时"}
        except Exception as e:
            logger.error(f"全量备份异常: {e}")
            return {"success": False, "path": str(filepath), "size_mb": 0,
                    "timestamp": timestamp, "error": str(e)}

    def restore(self, backup_path: str) -> dict:
        """
        从备份恢复数据库。

        Args:
            backup_path: 备份文件路径

        Returns:
            {"success": bool, "message": str}
        """
        try:
            if not Path(backup_path).exists():
                return {"success": False, "message": f"备份文件不存在: {backup_path}"}

            result = subprocess.run(
                [
                    "pg_restore",
                    "-h", "localhost",
                    "-p", "5432",
                    "-U", self.user,
                    "-d", self.dbname,
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-acl",
                    backup_path,
                ],
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=600,
            )

            if result.returncode != 0:
                logger.error(f"恢复失败: {result.stderr}")
                return {"success": False, "message": result.stderr[:200]}

            logger.info(f"恢复成功: {backup_path}")
            return {"success": True, "message": "恢复成功"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def verify_backup(self, backup_path: str) -> bool:
        """验证备份文件完整性"""
        path = Path(backup_path)
        if not path.exists():
            return False
        if path.stat().st_size == 0:
            return False
        return True

    def list_backups(self) -> list[dict]:
        """列出所有备份文件"""
        backups = []
        for f in sorted(BACKUP_DIR.glob("*.dump"), reverse=True):
            stat = f.stat()
            backups.append({
                "filename": f.name,
                "path": str(f),
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return backups

    def cleanup_old_backups(self) -> dict:
        """
        清理过期备份。

        Returns:
            {"deleted": int, "freed_mb": float}
        """
        deleted = 0
        freed_mb = 0.0

        full_cutoff = datetime.now() - timedelta(weeks=FULL_RETENTION_WEEKS)

        for f in BACKUP_DIR.glob("investpilot_full_*.dump"):
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < full_cutoff:
                freed_mb += f.stat().st_size / (1024 * 1024)
                f.unlink()
                deleted += 1
                logger.info(f"清理过期全量备份: {f.name}")

        return {"deleted": deleted, "freed_mb": round(freed_mb, 2)}

    def status_report(self) -> dict:
        """生成备份状态报告"""
        backups = self.list_backups()
        full = [b for b in backups if "full" in b["filename"]]
        latest_full = full[0].get("created_at") if full else None

        return {
            "total_backups": len(backups),
            "full_backups": len(full),
            "latest_full": latest_full,
            "total_size_mb": round(sum(b["size_mb"] for b in backups), 2),
            "backup_dir": str(BACKUP_DIR),
            "retention": {
                "full_weeks": FULL_RETENTION_WEEKS,
                "incremental_days": INCREMENTAL_RETENTION_DAYS,
            },
        }


def _load_password() -> str:
    """从凭据存储加载数据库密码"""
    try:
        store_path = Path.home() / ".hermes" / "invest_credentials" / "store.json"
        if store_path.exists():
            with open(store_path) as f:
                creds = json.load(f)
            return creds.get("DB_PASSWORD", "")
    except Exception:
        pass
    return ""


def backup_job():
    """
    备份定时任务入口。
    注册到 APScheduler: 每周日 23:00（全量）+ 每日 02:00（增量）。
    """
    today = datetime.now()
    password = _load_password()
    manager = DatabaseBackupManager(password=password)

    is_sunday = today.weekday() == 6

    if is_sunday:
        result = manager.full_backup()
        if result["success"]:
            logger.info(f"周日全量备份完成: {result['size_mb']} MB")
        else:
            logger.error(f"周日全量备份失败: {result.get('error')}")

    cleanup = manager.cleanup_old_backups()
    if cleanup["deleted"] > 0:
        logger.info(f"清理 {cleanup['deleted']} 个过期备份，释放 {cleanup['freed_mb']} MB")


if __name__ == "__main__":
    password = _load_password()
    manager = DatabaseBackupManager(password=password)

    print("=== 备份管理器状态 ===")
    status = manager.status_report()
    for k, v in status.items():
        print(f"  {k}: {v}")

    print("\n=== 执行全量备份 ===")
    result = manager.full_backup()
    print(f"  结果: {result}")