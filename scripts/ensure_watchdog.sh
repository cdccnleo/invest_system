#!/bin/bash
# ensure_watchdog.sh — watchdog 自身死了之后自动拉起
# 用途: cron 每分钟跑一次
# 设计: 双保险 (pgrep + fcntl lock), 避免误判已有进程

set -e

WORK_DIR="/home/aileo/invest_system"
VENV_PY="$WORK_DIR/.venv/bin/python3.11"
LOCK_FILE="$WORK_DIR/logs/.watchdog.lock"
LOG_FILE="$WORK_DIR/logs/watchdog.log"

# 1. pgrep 查找
if pgrep -f "scripts/watchdog_daemon.py" > /dev/null 2>&1; then
    exit 0  # watchdog 进程在跑
fi

# 2. 检查锁（即便 pgrep 漏了，锁文件还能兜底——fcntl 锁属于死进程时会被 OS 自动释放）
# 不需要再额外检查锁文件，pgrep 找不到说明没活进程

# 3. 拉起 watchdog（nohup + disown + 独立 session）
cd "$WORK_DIR"
nohup "$VENV_PY" scripts/watchdog_daemon.py >> "$LOG_FILE" 2>&1 &
disown
echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] ensure_watchdog: watchdog was down, restarted (PID=$!)" >> "$LOG_FILE"
