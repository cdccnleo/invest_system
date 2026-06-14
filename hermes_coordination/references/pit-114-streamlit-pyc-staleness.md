# PIT #114 — Streamlit 也是 daemon: pgcrypto_migration 修复后需重启 (6/14 实战)

**实战时间**: 2026-06-14 21:30 (用户 dashboard 手工触发公告同步失败)
**实战损失**: 研报/新闻手工同步正常 (不依赖 load_positions_from_db), 但公告同步失败. 实战证明: 同一个 streamlit 进程内, **不同子模块 import 的 pgcrypto_migration 共享同一份 .pyc**, 已加载则不感知 .py 改动

## 根因

PIT #112 修复 (commit d6fedbe, 20:57) 后:
- schedule_runner 进程 449203 跑了 30min (20:43 启动), 加载旧 .pyc → 21:05 失败 (PIT #113)
- streamlit 进程 609 跑了 2 天 (启动时间 2026-06-12), 加载旧 .pyc → **公告同步**失败 (本 PIT)

**关键**: 研报/新闻手工同步**不依赖** `load_positions_from_db` (它们用 `get_credential` 或不用 pgcrypto), 所以**没崩**, 给用户造成"补丁生效"假象.

| 模块 | 调 pgcrypto_migration 的什么? | 是否会触发 "Wrong key"? |
|------|------------------------------|------------------------|
| **fetch_news.py** | ❌ 不调 | 不会崩 |
| **fetch_reports.py** | `get_credential` (DB_PASSWORD) | 不会崩 (不读持仓) |
| **fetch_announcements.py** | `load_positions_from_db` (line 316) | **会崩** (PIT #112 数据坏) |

**测试方式**: 用户从 dashboard 同步研报/新闻**一切正常**, 但同步公告**必崩**.

## 诊断

```bash
# 1. 看 streamlit 进程启动时间 vs pyc mtime
ps -p <streamlit_pid> -o lstart
# Sat Jun 12 21:50:24 2026  ← 2 天前, 旧 .pyc

ls -la scripts/__pycache__/pgcrypto_migration.cpython-311.pyc
# -rw-r--r-- 1 aileo aileo 28095 Jun 14 20:57  ← 补丁后

# 2. 进程启动时间 < pyc mtime = 进程用旧 .pyc = patch 未生效
```

## 修复 (P0 必做)

```bash
# 1. kill streamlit 进程
kill -9 <streamlit_pid>
# 2. watchdog 自动重启 (从 logs/watchdog.log 看 "Restarting streamlit")
# 3. 验证新进程启动时间 > pyc mtime
```

## 防御 (P1 待办)

**Streamlit 是 PIT #113 的"另一类 daemon"**:
- 修改 `pgcrypto_migration.py` 后, **必须重启所有 import 该模块的进程**:
  - schedule_runner (PIT #113 已记录)
  - **streamlit** (本 PIT)
  - **streamlit 通过 subprocess 启的子脚本** (eg merge_holdings.py, quote_streamer.py)
  - **任何手动跑的 python -c** (开发用)

### 推荐自动化 (复用 PIT #113 防御):

1. **`scripts/post_patch_reload.sh`** - 接受 .py 路径, 自动 kill+重启 schedule_runner + streamlit + 验证
2. **CI 检查**: 每次 git push 后, 对所有 daemon (ps aux | grep python) 计算 pyc mtime vs 进程启动时间差
3. **watchdog 增强**: 检测到 schedule_runner/streamlit 自己 import 的 .py mtime 改变 → 自动重启

## 教训

- **"代码修复" ≠ "线上生效"** (PIT #113 教训的延伸).
- **同一进程内不同子模块的 import 共享 sys.modules 缓存** - 一个子模块 import 了 pgcrypto, 另一个子模块就**继承**这个 .pyc 版本
- **"部分手工同步正常" 容易掩盖问题** - 必须**全链路**测试
- **daemon 引用 .py 的修复, 必做 4 步**:
  1. commit
  2. push
  3. **重启 schedule_runner** (PIT #113)
  4. **重启 streamlit** (本 PIT #114)
  - 还要考虑: quote_streamer / merge_holdings / 任何 daemon subprocess

## PIT 计数

- v2.6.0 release: 110
- PIT #111 schedule_runner 9h 僵尸 (6/14 20:43)
- PIT #112 load_positions_from_db 容错 (6/14 20:50)
- PIT #113 schedule_runner 加载旧 .pyc (6/14 21:11)
- **PIT #114 streamlit 也是 daemon 加载旧 .pyc (新)** (6/14 21:30)
- 累计: **114 PIT**
