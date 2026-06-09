#!/bin/bash
# ensure_watchdog.sh — watchdog 自身死了之后自动拉起
# 用途: cron 每分钟跑一次
# 设计: 三重保险
#   1. pgrep 查找 (主要手段)
#   2. fcntl lock 兜底 (即便 pgrep 漏了, 锁文件还在说明 watchdog 还活着)
#   3. 孤儿 schedule_runner 检测: 锁文件 PID 写的是 schedule_runner,
#       但实际拥有的是孤儿 (PPid=1). 杀孤儿释放锁, 让 watchdog 能顺利拉起 schedule_runner.

set -e

WORK_DIR="/home/aileo/invest_system"
VENV_PY="$WORK_DIR/.venv/bin/python3.11"
LOCK_FILE="$WORK_DIR/logs/.watchdog.lock"
SR_LOCK_FILE="$WORK_DIR/logs/.schedule_runner.lock"
LOG_FILE="$WORK_DIR/logs/watchdog.log"

# 1. pgrep 查找
if pgrep -f "scripts/watchdog_daemon.py" > /dev/null 2>&1; then
    exit 0  # watchdog 进程在跑
fi

# 2. watchdog lock 兜底
if [ -f "$LOCK_FILE" ]; then
    lock_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$lock_pid" ] && [ -d "/proc/$lock_pid" ]; then
        # 锁 PID 存活, 说明 watchdog 真的在跑 (pgrep 漏了)
        exit 0
    fi
fi

# 3. 孤儿 schedule_runner 检测 + 清理
# 场景: watchdog 死了后, schedule_runner 变成孤儿, 但 fcntl 锁没释放
#       → 新 watchdog 拉起后启 schedule_runner 抢不到锁 → 死循环
# 解决: 拉 watchdog 之前, 检查 .schedule_runner.lock 写的 PID 是否还活着
#       若 PID 不存在 或 PID 是孤儿 (PPid=1), 则视为死锁残留, 强删
if [ -f "$SR_LOCK_FILE" ]; then
    sr_pid=$(cat "$SR_LOCK_FILE" 2>/dev/null | tr -d '[:space:]' || echo "")
    if [ -n "$sr_pid" ]; then
        if [ ! -d "/proc/$sr_pid" ]; then
            # 进程不存在, 强删锁
            rm -f "$SR_LOCK_FILE"
            echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] ensure_watchdog: 孤儿 schedule_runner (PID=$sr_pid) 残留, 强删 lock" >> "$LOG_FILE"
        else
            # 进程存在但可能是孤儿 (PPid=1). 实际孤儿持锁会导致新 schedule_runner 抢锁失败
            sr_ppid=$(awk '/PPid/{print $2}' "/proc/$sr_pid/status" 2>/dev/null || echo "0")
            if [ "$sr_ppid" = "1" ]; then
                # 孤儿 schedule_runner, 杀掉释放锁
                kill -9 "$sr_pid" 2>/dev/null || true
                rm -f "$SR_LOCK_FILE"
                echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] ensure_watchdog: 孤儿 schedule_runner (PID=$sr_pid, PPid=1) 已杀, 释放 lock" >> "$LOG_FILE"
            fi
        fi
    fi
fi

# 4. 拉起 watchdog
cd "$WORK_DIR"
nohup "$VENV_PY" scripts/watchdog_daemon.py >> "$LOG_FILE" 2>&1 &
disown
echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] ensure_watchdog: watchdog was down, restarted (PID=$!)" >> "$LOG_FILE"
