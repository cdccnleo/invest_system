"""
log_rotator.py — 日志轮转模块
基于 RotatingFileHandler 实现日志按大小自动轮转和归档
"""

import logging
import logging.handlers
import os
from pathlib import Path
from datetime import datetime

LOG_DIR = Path(__file__).parent.parent / "logs"
MAX_BYTES = 10 * 1024 * 1024  # 10MB per file
BACKUP_COUNT = 10              # Keep 10 rotated files
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_log_rotation(logger_name: str = None) -> logging.Logger:
    """
    配置日志轮转

    Args:
        logger_name: 日志记录器名称，默认根日志器

    Returns:
        配置好的 Logger 实例
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    logger.setLevel(logging.INFO)

    # 清除已有的 handlers（避免重复）
    logger.handlers.clear()

    # 文件轮转 handler
    log_file = LOG_DIR / "investpilot.log"
    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    logger.addHandler(file_handler)

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    logger.addHandler(console_handler)

    # 错误日志单独文件
    error_log = LOG_DIR / "investpilot_error.log"
    error_handler = logging.handlers.RotatingFileHandler(
        filename=str(error_log),
        maxBytes=MAX_BYTES,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    logger.addHandler(error_handler)

    return logger


def get_log_stats() -> dict:
    """
    获取日志文件统计信息

    Returns:
        {"log_files": [...], "total_size_mb": float, "oldest_date": str}
    """
    if not LOG_DIR.exists():
        return {"log_files": [], "total_size_mb": 0, "oldest_date": ""}

    files = []
    total_size = 0
    oldest = None

    for f in sorted(LOG_DIR.glob("*.log*")):
        stat = f.stat()
        size = stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime)
        files.append({
            "name": f.name,
            "size_kb": round(size / 1024, 1),
            "modified": mtime.isoformat(),
        })
        total_size += size
        if oldest is None or mtime < oldest:
            oldest = mtime

    return {
        "log_files": files,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "oldest_date": oldest.isoformat() if oldest else "",
    }


def cleanup_old_logs(max_age_days: int = 90):
    """
    清理过期日志文件

    Args:
        max_age_days: 最大保留天数
    """
    if not LOG_DIR.exists():
        return

    cutoff = datetime.now().timestamp() - max_age_days * 86400
    deleted = 0
    for f in LOG_DIR.glob("*.log*"):
        if f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass

    if deleted > 0:
        logging.getLogger(__name__).info(f"已清理 {deleted} 个过期日志文件")


if __name__ == "__main__":
    logger = setup_log_rotation("test")
    logger.info("日志轮转模块已初始化")
    logger.warning("这是警告日志")
    logger.error("这是错误日志")

    stats = get_log_stats()
    print(f"\n日志统计: {stats['total_size_mb']}MB, {len(stats['log_files'])} 个文件")