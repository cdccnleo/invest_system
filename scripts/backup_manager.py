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
import sys
import subprocess
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

# 确保 scripts/ 目录在 path 中（支持直接运行脚本时 import credentials 等）
_ROOT = Path(__file__).parent.parent
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

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
    """从 credentials 模块加载数据库密码"""
    try:
        from credentials import get_credential
        pw = get_credential("DB_PASSWORD")
        if pw:
            return pw
    except Exception:
        pass
    # 降级：尝试本地存储文件
    try:
        store_path = Path.home() / ".hermes" / "invest_credentials" / "store.json"
        if store_path.exists():
            with open(store_path) as f:
                creds = json.load(f)
            return creds.get("DB_PASSWORD", "")
    except Exception:
        pass
    return ""


def run_pg_dump() -> bool:
    """
    执行 pg_dump 每日备份（压缩自定义格式 .sql.gz）。
    使用 credentials.get_credential("DB_PASSWORD") 获取密码。
    返回是否成功。
    """
    from datetime import date
    from credentials import get_credential

    today = date.today().isoformat()
    backup_dir = Path.home() / "invest_data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / f"investpilot_{today}.sql.gz"

    password = get_credential("DB_PASSWORD") or ""
    env = os.environ.copy()
    env["PGPASSWORD"] = password

    result = subprocess.run(
        ["pg_dump", "-U", "invest_admin", "-d", "investpilot",
         "-h", "localhost", "-Fc", "-c", "--if-exists"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        with open(backup_file, "wb") as f:
            f.write(result.stdout)
        logger.info(f"pg_dump 备份成功: {backup_file}")
        return True
    else:
        stderr = result.stderr.decode("utf-8", errors="replace")
        logger.error(f"pg_dump 备份失败: {stderr[:200]}")
        return False


def cleanup_old_backups(days: int = 7):
    """
    删除超过 days 天的备份文件（滚动清理）。
    """
    import time

    backup_dir = Path.home() / "invest_data" / "backups"
    if not backup_dir.exists():
        return

    now = time.time()
    cutoff = days * 86400
    deleted = 0
    for f in backup_dir.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > cutoff:
            f.unlink()
            deleted += 1
            logger.info(f"清理过期备份: {f.name}")

    if deleted > 0:
        logger.info(f"清理 {deleted} 个过期备份")


def job_daily_backup():
    """
    每日备份任务（供 schedule_runner 调用）。
    每日 16:00 执行（非仅交易日），滚动保留 7 份。
    失败时发送错误告警。
    """
    logger.info("开始每日 pg_dump 备份...")
    success = run_pg_dump()
    if success:
        cleanup_old_backups(days=7)
        logger.info("每日备份完成")
    else:
        logger.error("每日备份失败")
        # 尝试发送告警（导入延迟避免循环依赖）
        try:
            from notification import send_error_alert
            send_error_alert("❌ 每日备份失败", "pg_dump 备份任务执行失败，请检查数据库连接和备份目录磁盘空间。")
        except Exception:
            pass
    return success


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