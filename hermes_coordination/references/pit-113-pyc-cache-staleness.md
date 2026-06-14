# PIT #113 — Python daemon .pyc 固化陷阱 (6/14 实战)

**实战时间**: 2026-06-14 21:05 (公告采集修复后 5min)
**实战损失**: PIT #112 修复 commit `d6fedbe` (20:57 落地) 后, schedule_runner 21:05 cron 仍用 20:43 启动时的旧 pyc, 情绪因子 cron 再次失败 "Wrong key or corrupt data"

## 根因

Python 进程在启动时**已经把所有 import 的 .py 文件编译成 .pyc 加载到内存**。修改 .py 后, **已经在跑的进程不感知**。必须重启。

| 关键时间 | 事件 |
|---------|------|
| 20:43 | schedule_runner PID 449203 启动 → import pgcrypto_migration.py → 编译为 .pyc (mtime 20:43) |
| 20:50 | 公告采集 cron 触发 → 旧 load_positions_from_db 抛 "Wrong key or corrupt data" |
| 20:55-20:57 | 我加 PIT #112 容错补丁 → `pgcrypto_migration.py` mtime 20:57 → pyc mtime 20:57 |
| 20:58 | git commit d6fedbe + push |
| 20:59 | 验证补丁 OK, 公告采集 cron 6/15 20:50 会自动恢复 |
| 21:00 | risk_escalation 评估异常 (调 load_positions_from_db, 旧 pyc) |
| 21:05 | **情绪因子更新异常 (调 load_positions_from_db, 旧 pyc)** ⚠️ |
| 21:11 | 用户发告警 🔴 任务失败 |
| 21:13 | 我 kill 449203 + watchdog 自动重启 463850 (新 pyc 生效) |

## 诊断

```bash
# 1. 看 pyc mtime vs 进程启动时间
ls -la scripts/__pycache__/pgcrypto_migration.cpython-311.pyc
#  -rw-r--r-- 1 aileo aileo 28095 Jun 14 20:57 scripts/__pycache__/pgcrypto_migration.cpython-311.pyc

# 2. 看 schedule_runner 进程启动时间
ps -p 449203 -o lstart
#           STARTED
# Sun Jun 14 20:43:55 2026

# 3. → pyc 20:57 > 进程 20:43 → 进程用的是 20:43 旧 pyc → patch 未生效
```

## 修复 (P0 必做)

```bash
# 1. 看哪些 daemon 用了 pgcrypto_migration
grep -rn "from pgcrypto_migration" scripts/

# 2. 重启每个 daemon (生产)
kill -TERM <schedule_runner_pid>
# watchdog 自动重启
# 或手动:
cd scripts && nohup .venv/bin/python3.11 schedule_runner.py > logs/restart.log 2>&1 &

# 3. 验证新进程启动时间 > pyc mtime
ps -p <new_pid> -o lstart
```

## 防御 (P1 待办)

**核心**: 修改任何 daemon 引用的 .py 后, **必须重启该 daemon**。建议自动化:

1. **`scripts/post_patch_reload.sh`** - 接受 .py 路径, 自动 kill + 重启 schedule_runner + 验证
2. **CI 检查**: 每次 git push 后, 对 daemon 引用的 .py 计算 pyc mtime vs 进程启动时间差, 若 pyc mtime > 进程启动时间 → ALERT
3. **watchdog 增强**: 检测到 schedule_runner 自己 import 的 .py mtime 改变 → 自动重启 (类似 Erlang hot reload)
4. **pyc hash 监控**: daemon 启动时记录所有 import 的 .py 的 SHA256, 定时 (每 5min) 重新 hash 比对, 变化 → ALERT
5. **强制 reload decorator**: 在 load_positions_from_db 等关键函数加 `@watch_pyc_change`, 一旦 pyc mtime 变化 → logger.warning("patch detected, daemon restart required")

## 教训

- **"代码修复" ≠ "线上生效"**。patch commit 不等于用户不再受影响。
- **daemon 引用 .py 的修复**, 必做 3 步: (1) commit (2) push (3) **重启引用该 .py 的所有 daemon**
- **重启是 patch 完整流程的最后一环**, 不可省略。
- **重启优先级**: schedule_runner / quote_streamer / cron daemon / 所有 .py 改动的引用方

## PIT 计数

- v2.6.0 release: 110
- PIT #111 schedule_runner 9h 僵尸 (6/14 20:43)
- PIT #112 load_positions_from_db 容错 (6/14 20:50)
- **PIT #113 .pyc 固化陷阱 (新)** (6/14 21:11)
- 累计: **113 PIT**
