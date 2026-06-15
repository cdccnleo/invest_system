#!/bin/bash
# ==============================================================================
# post_patch_reload.sh — PIT #113 + #114 + #125 跨 PIT 防御
# ==============================================================================
# 实战触发: 6/14 PIT #113 (schedule_runner .pyc) + 6/14 PIT #114 (streamlit .pyc)
#          + 6/16 PIT #125 (streamlit 不被 watchdog 自动重启)
# 实战教训: 任何 .py 修改后, **必须** 重启 schedule_runner + streamlit, 但 streamlit
#           不像 schedule_runner 有 watchdog 自动接管, 必显式 nohup 重启
# 实战用法: bash scripts/post_patch_reload.sh scripts/xxx.py
#            或: bash scripts/post_patch_reload.sh  (扫所有新 .pyc 自动判断)
# ==============================================================================
set -e

PROJECT_ROOT="${PROJECT_ROOT:-/home/aileo/invest_system}"
PATCHED_PY="${1:-}"

echo "=== post_patch_reload.sh 启动 (PIT #113+#114+#125 防御) ==="
echo "PATCHED_PY: ${PATCHED_PY:-<auto-scan all .pyc>}"
echo ""

# Step 1: 找最新 .pyc mtime
if [ -n "$PATCHED_PY" ]; then
    PYC_BASENAME=$(basename "$PATCHED_PY" .py)
    PYC=$(find "$PROJECT_ROOT/scripts/__pycache__" "$PROJECT_ROOT/hermes_coordination/scripts/__pycache__" \
          -name "${PYC_BASENAME}.cpython-311.pyc" 2>/dev/null | head -1)
    if [ -z "$PYC" ]; then
        echo "  ⚠️  $PATCHED_PY 没有 .pyc, 不必重启"
        exit 0
    fi
else
    # Auto-scan: 找所有 mtime 在最近 10 分钟内的 .pyc
    PYC=$(find "$PROJECT_ROOT/scripts/__pycache__" "$PROJECT_ROOT/hermes_coordination/scripts/__pycache__" \
          -name "*.cpython-311.pyc" -newer /tmp/post_patch_reload_last_run 2>/dev/null | head -1)
    if [ -z "$PYC" ]; then
        echo "  无新 .pyc (10 分钟内), 不必重启"
        exit 0
    fi
fi

PYC_MTIME=$(stat -c %Y "$PYC")
PYC_MTIME_STR=$(date -d @$PYC_MTIME '+%Y-%m-%d %H:%M:%S')
echo "最新 .pyc: $PYC (mtime: $PYC_MTIME_STR)"

# Step 2: 检查 schedule_runner 是否需要重启
SR_PID=$(pgrep -f "scripts/schedule_runner.py" | head -1)
SR_NEED_RESTART=0
if [ -n "$SR_PID" ]; then
    SR_LSTART=$(stat -c %Y /proc/$SR_PID 2>/dev/null || echo 0)
    SR_LSTART_STR=$(date -d @$SR_LSTART '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
    if [ "$PYC_MTIME" -gt "$SR_LSTART" ]; then
        echo "🔴 schedule_runner (PID $SR_PID) lstart=$SR_LSTART_STR < .pyc=$PYC_MTIME_STR → 必重启 (PIT #113)"
        SR_NEED_RESTART=1
    else
        echo "✅ schedule_runner (PID $SR_PID) lstart=$SR_LSTART_STR >= .pyc=$PYC_MTIME_STR (无新 .pyc, 不必重启)"
    fi
else
    echo "  schedule_runner 未运行, watchdog 必启动"
    SR_NEED_RESTART=1
fi

# Step 3: 重启 schedule_runner (PIT #113 必做)
if [ "$SR_NEED_RESTART" -eq 1 ]; then
    if [ -n "$SR_PID" ]; then
        kill -TERM "$SR_PID"
        sleep 5
    fi
    # watchdog 自动启, 等 5s
    sleep 5
    NEW_SR_PID=$(pgrep -f "scripts/schedule_runner.py" | head -1)
    if [ -n "$NEW_SR_PID" ]; then
        echo "  ✅ schedule_runner 重启: 新 PID $NEW_SR_PID"
    else
        echo "  ⚠️  schedule_runner 未自动启, 手动 nohup"
        cd "$PROJECT_ROOT/scripts"
        nohup .venv/bin/python3.11 schedule_runner.py > /tmp/schedule_runner_manual.log 2>&1 &
    fi
fi

# Step 4: 检查 streamlit 是否需要重启 (PIT #114 + #125)
ST_PID=$(pgrep -f "streamlit run scripts/dashboard" | head -1)
ST_NEED_RESTART=0
if [ -n "$ST_PID" ]; then
    ST_LSTART=$(stat -c %Y /proc/$ST_PID 2>/dev/null || echo 0)
    ST_LSTART_STR=$(date -d @$ST_LSTART '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
    # streamlit 用的可能是不同 .py (dashboard_views/__main__.py), 实战查最新 .pyc
    LATEST_PYC=$(find "$PROJECT_ROOT/scripts/dashboard_views/__pycache__" \
                 -name "*.cpython-311.pyc" 2>/dev/null | \
                 xargs -I {} stat -c '%Y {}' {} 2>/dev/null | sort -rn | head -1 | awk '{print $1}')
    if [ -n "$LATEST_PYC" ] && [ "$LATEST_PYC" -gt "$ST_LSTART" ]; then
        echo "🔴 streamlit (PID $ST_PID) lstart=$ST_LSTART_STR < 最新 .pyc=$(date -d @$LATEST_PYC '+%Y-%m-%d %H:%M:%S') → 必重启 (PIT #114+#125)"
        ST_NEED_RESTART=1
    else
        echo "✅ streamlit (PID $ST_PID) lstart=$ST_LSTART_STR (无新 .pyc, 不必重启)"
    fi
else
    echo "  streamlit 未运行, 必手动 nohup 启 (PIT #125)"
    ST_NEED_RESTART=1
fi

# Step 5: 重启 streamlit (PIT #114+#125 必做, watchdog 不管 streamlit, 必手动 nohup)
if [ "$ST_NEED_RESTART" -eq 1 ]; then
    if [ -n "$ST_PID" ]; then
        kill -9 "$ST_PID"  # streamlit 不响应 TERM, 必 -9
        sleep 8  # 等 kernel ps cache
    fi
    # 必手动 nohup 启 (PIT #125 实战新发现: watchdog 不管 streamlit)
    cd "$PROJECT_ROOT"
    nohup .venv/bin/python3.11 -m streamlit run scripts/dashboard_views/__main__.py \
          --server.port 8501 --server.address 0.0.0.0 --server.headless true \
          > logs/streamlit.log 2>&1 &
    sleep 8
    NEW_ST_PID=$(pgrep -f "streamlit run scripts/dashboard" | head -1)
    if [ -n "$NEW_ST_PID" ]; then
        echo "  ✅ streamlit 重启: 新 PID $NEW_ST_PID"
    else
        echo "  ❌ streamlit 启动失败, 查 logs/streamlit.log"
        tail -20 logs/streamlit.log
        exit 1
    fi
fi

# Step 6: 验证 (5 步)
echo ""
echo "=== 验证 (5 步) ==="
sleep 3

# 6.1 schedule_runner 健康
NEW_SR_PID=$(pgrep -f "scripts/schedule_runner.py" | head -1)
if [ -n "$NEW_SR_PID" ]; then
    echo "  ✅ schedule_runner PID $NEW_SR_PID 运行中"
    # 看是否新 PID
    if [ "$NEW_SR_PID" != "$SR_PID" ]; then
        echo "    (新 PID, PIT #113 重启实战 ✅)"
    fi
else
    echo "  ❌ schedule_runner 未运行"
fi

# 6.2 streamlit 健康
NEW_ST_PID=$(pgrep -f "streamlit run scripts/dashboard" | head -1)
if [ -n "$NEW_ST_PID" ]; then
    echo "  ✅ streamlit PID $NEW_ST_PID 运行中"
    # 6.3 HTTP 8501 验证
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8501/ 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "  ✅ Streamlit HTTP 200 (dashboard 端到端通)"
    else
        echo "  ⚠️  Streamlit HTTP $HTTP_CODE (等 5s 再试)"
    fi
else
    echo "  ❌ streamlit 未运行"
fi

# 6.4 端口 8501 LISTEN
ss -tlnp 2>/dev/null | grep -E ":8501" | head -1 | awk '{print "  ✅ 8501 端口: " $0}'

# 6.5 实战 30s 后再做一次手工同步入口测 (PIT #114 实战教训)
echo ""
echo "  ⚠️  实战必做: 30s 后手工测每个 dashboard 同步入口"
echo "     (PIT #114 教训: 研报/新闻 OK 不代表公告 OK, 必逐个入口测)"

# 记录本次时间戳 (用于 auto-scan)
date '+%Y-%m-%d %H:%M:%S' > /tmp/post_patch_reload_last_run

echo ""
echo "=== post_patch_reload.sh 完成 ==="
