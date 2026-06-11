#!/bin/bash
# watch_hermes_agent.sh
# Hermes Agent watchdog监控脚本
# 创建时间: 2026-06-11
# 配合补丁2（可观测性）使用

# ============================================================
# 配置
# ============================================================
LOG_DIR="/mnt/c/PythonProject/invest_system/logs"
ALERT_LOG="$LOG_DIR/watchdog_alerts.log"
CHECK_INTERVAL=60  # 每60秒检查一次
MAX_FAILURE_COUNT=3  # 连续失败3次触发告警

# 监控的进程名
declare -A PROCESSES=(
    ["hermes_event_analyst"]="python.*hermes_event_analyst"
    ["hermes_kb_ingest"]="python.*hermes_kb_ingest"
)

# ============================================================
# 函数
# ============================================================
log_alert() {
    local level=$1
    local message=$2
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] [$level] $message" >> "$ALERT_LOG"
    echo "[$timestamp] [$level] $message"

    # 钉钉告警（mock - 实际对接notification.py）
    if [ "$level" == "CRITICAL" ] || [ "$level" == "P0" ]; then
        curl -s -X POST "$DINGTALK_WEBHOOK" \
            -H 'Content-Type: application/json' \
            -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"🚨 Hermes Agent Watchdog: $message\"}}" \
            > /dev/null 2>&1 || true
    fi
}

check_process() {
    local name=$1
    local pattern=$2

    if ! pgrep -f "$pattern" > /dev/null; then
        echo "FAIL"
        return 1
    else
        echo "OK"
        return 0
    fi
}

restart_process() {
    local name=$1
    log_alert "WARN" "尝试重启进程: $name"

    case $name in
        hermes_event_analyst)
            cd /mnt/c/PythonProject/invest_system && \
                nohup python -m scripts.hermes_event_analyst > "$LOG_DIR/hermes_event_analyst.out" 2>&1 &
            ;;
        hermes_kb_ingest)
            cd /mnt/c/PythonProject/invest_system && \
                nohup python -m scripts.hermes_kb_ingest > "$LOG_DIR/hermes_kb_ingest.out" 2>&1 &
            ;;
    esac

    sleep 5
    log_alert "INFO" "重启完成: $name"
}

# ============================================================
# 主循环
# ============================================================
declare -A FAILURE_COUNT

log_alert "INFO" "==============================================="
log_alert "INFO" "Hermes Agent Watchdog启动"
log_alert "INFO" "监控进程: ${!PROCESSES[@]}"
log_alert "INFO" "检查间隔: ${CHECK_INTERVAL}秒"
log_alert "INFO" "==============================================="

while true; do
    for name in "${!PROCESSES[@]}"; do
        pattern="${PROCESSES[$name]}"
        status=$(check_process "$name" "$pattern")

        if [ "$status" == "FAIL" ]; then
            FAILURE_COUNT[$name]=$(( ${FAILURE_COUNT[$name]:-0} + 1 ))
            count=${FAILURE_COUNT[$name]}

            log_alert "WARN" "进程异常: $name (连续失败 $count 次)"

            if [ $count -ge $MAX_FAILURE_COUNT ]; then
                log_alert "CRITICAL" "🚨 进程 $name 连续失败 $count 次，触发告警!"
                restart_process "$name"
                FAILURE_COUNT[$name]=0
            fi
        else
            if [ "${FAILURE_COUNT[$name]:-0}" -gt 0 ]; then
                log_alert "INFO" "进程恢复: $name"
                FAILURE_COUNT[$name]=0
            fi
        fi
    done

    sleep $CHECK_INTERVAL
done