# PIT #124 + #125 + #130 — V2.7.1 防御改进实战 (cost CHECK + post_patch_reload + 模板 + 回归 + append-only)

**实战时间**: 2026-06-16 07:35
**触发场景**: 用户指令 "(B) V2.7.1 防御改进 (PIT #124+#125)" + V26-A 5min 自动按 (A) 全清执行
**影响范围**: 4 防御 (cost CHECK + reload.sh + v27_governance_final + test_track_cron_task) + 2 文档化 (PIT #124 文档 + PIT #125 实战发现)
**修复版本**: V2.7.1 防御改进实战 (4 防御 + 2 文档化 + 3 实战新发现)

## 一、4 防御实战

### 1.1 防御 1: cost_enc >= 0 trigger (PIT #124 实战核心)

**实战触发**: V2.7 治理 159819 cost=-1.1568 录入错, 留 V2.7.1 防御
**实战修复**:
1. **id 10 588290 5/23 历史负数 cost -4.2041 → 4.2041** (pgp_sym_encrypt 重写)
2. **validate_cost_enc() trigger 函数** (硬编码 DB_KEY 兜底, 实战 session 无关)
3. **trg_validate_cost_enc trigger** (BEFORE INSERT/UPDATE)
4. **实战测试 3 场景**:
   - ✅ 正数 cost INSERT 成功
   - ✅ 负数 cost INSERT 被拒 (PIT #124 防御触发): "PIT #124 V2.7.1 防御: cost_enc decrypt 失败 (负数 cost), code=TEST_NEG, account=test_acc, cost=-10.5"
   - ✅ cost=0 INSERT 成功 (标准券类允许)

**Class-level 铁律** (PIT #124 实战新): 
- 任何**加密字段** (bytea + pgp_sym_encrypt) 不能用标准 CHECK 约束 (`cost >= 0`), 必用 **trigger + decrypt 表达式 + ERRCODE 23514** 组合
- trigger 内**优先用 session `app.db_encryption_key`**, 兜底硬编码 (实战 trigger 不依赖外部 session)
- decrypt 失败 (PIT #121 空 bytea) → 跳过 (不抛错, PIT #121 兼容)
- decrypt 成功但 cost<0 → 抛错 + ERRCODE 23514 (check_violation)

### 1.2 防御 2: post_patch_reload.sh (PIT #113+#114+#125 跨 PIT 防御)

**实战触发**: 
- PIT #113: schedule_runner 加载旧 .pyc
- PIT #114: streamlit 加载旧 .pyc
- **PIT #125 实战新发现 (6/16 早盘)**: streamlit **不被 watchdog 自动重启**, 必手动 nohup

**实战脚本** (`scripts/post_patch_reload.sh`, 200 行):
1. 找最新 .pyc mtime
2. 检查 schedule_runner PID lstart, pyc mtime > lstart → kill -TERM + 等 watchdog
3. **PIT #125 实战**: 检查 streamlit PID lstart, 必 kill -9 + **手动 nohup 重启** (watchdog 不管)
4. 5 步验证: 进程健康 + HTTP 200 + 端口 LISTEN + lstart 正确 + 手工同步入口测

**实战测试 6/16 07:35**:
- 无新 .pyc → 自动退出 ✅
- 手动触发 .pyc 生成 → 实战 PIT #114+#125 防御 ✅
- 实战 PIT #125 修复: streamlit kill -9 后必 nohup 启 (watchdog 不接管)

### 1.3 防御 3: v27_governance_final.py 固化 (PIT #122+#123 模板化)

**实战触发**: V2.7 治理 2.0-2.4 实战踩坑 3 次, 必固化到可复用模板
**实战脚本** (`scripts/v27_governance_final.py`, 11350 字节):
- 头部 50 行 PIT #123+#124 铁律注释 (实战踩坑固化)
- 实战使用流程 + rollback 脚本
- 实战触发场景列表 (PIT #99/#104/#121/#124)

**实战 PIT #123 4 步铁律固化**:
1. 备份 (CREATE TABLE backup)
2. CSV→PG 账户映射 (显式 dict)
3. 单行单账户 INSERT
4. 短路求值禁

### 1.4 防御 4: tests/test_track_cron_task.py 回归 (PIT #120 防复发)

**实战触发**: PIT #119 修复 commit 966d676 装饰器位置错位, schedule_runner 反复重启 5 次 NameError
**实战测试** (`tests/test_track_cron_task.py`, 13.2 KB, 6 场景):

| # | 场景 | 验证 |
|---|---|---|
| 1 | decorator 基础行为 | track_cron_task(name) 包函数, return 透传 ✅ |
| 2 | success 状态 | items_processed=10, failed=0, status=success ✅ |
| 3 | partial → success | PIT #119 status 约束实战 (无 partial) ✅ |
| 4 | failed 状态 | processed=0, failed=5 → status=failed ✅ |
| 5 | exception 状态 | 抛错仍写表, error_message 含 PIT #120 标记 ✅ |
| 6 | 静态分析 (集成) | track_cron_task (line 279) < first_job (line 368) ✅, 34 个装饰器 ≥ 28 ✅ |

**实战测试通过率**: 6/6 (6/16 07:36) 🎉

**PIT #120 防复发实战铁律**:
- 装饰器位置错位 = NameError 反复重启, watchdog 死循环 (PIT #120 实战)
- 静态分析实战: `ast.parse(schedule_runner.py)` + 找 track_cron_task line + 找 first job_ line + assert <

## 二、3 实战新发现 (PIT #125 + #130 + #131)

### 2.1 PIT #125 (6/16 早盘实战新发现, 6/16 07:24 实战)

**实战真相**: Streamlit 跑 17h, 6/15 20:30 PIT #117 修复的 .pyc **没被加载** — PIT #114 实战铁律再次验证. **但更深入发现**: Streamlit 杀掉后, watchdog **不接管** (vs schedule_runner 杀后 watchdog 5s 内重启).

**PIT #125 实战新铁律**:
- 任何 .py 修复后必做 **5 步** (PIT #113+#114+#125 升级):
  1. commit
  2. push
  3. 重启 schedule_runner (watchdog 自动启)
  4. **手动 nohup 重启 streamlit** (PIT #125 实战新: watchdog 不管 streamlit)
  5. 5 步验证 (进程 + HTTP + 端口 + pyc 加载 + 手工同步入口测)
- `post_patch_reload.sh` 实战 PIT #125 防御: 脚本内置 `nohup streamlit` 重启逻辑
- Streamlit 实战限制: 不像 schedule_runner 有 fcntl.flock 自动检测, 必**显式 kill -9 + nohup**

### 2.2 PIT #130 (6/16 07:34 实战新发现)

**实战真相**: `holdings.encrypted_positions` 表有 `no_pos_modification` trigger 禁止 DELETE — **append-only 表**! 实战测试想 DELETE 清理测试行失败.

**PIT #130 实战新铁律**:
- 任何 encrypted_positions **测试** 必用 `UPDATE is_current = FALSE` 而非 DELETE
- 测试行必带 `code = 'TEST_xxx'` 前缀, 清理脚本 `UPDATE SET is_current=FALSE WHERE code LIKE 'TEST_%'`
- 真生产数据治理 (V2.7 治理) 用 `is_current=TRUE` 标记当前, `is_current=FALSE` 标记历史
- 不能 DELETE 实战数据 (append-only) = PIT #86 idempotent 沿用

### 2.3 PIT #131 (6/16 07:35 实战新发现)

**实战真相**: 任何测试 schedule_runner.py 必触发 fcntl.flock 单实例锁拦截, 测试 sys.exit(0) — 实战 6/16 07:35 测试 import schedule_runner 失败.

**PIT #131 实战新铁律**:
- schedule_runner.py **不能** 在测试环境直接 import (line 47-82 lock 检查 sys.exit)
- 实战方案: **ast 静态分析** (测试 6 实战) 或 **exec 单函数源码** (测试 1-5 实战) 绕过 lock 检查
- `tests/test_track_cron_task.py` 实战: 用 `ast.parse` 找 `track_cron_task` 函数 line + `extract_track_cron_task_from_source()` + `exec(func_src, namespace)` 在 sandbox 跑
- 实战 namespace 必含 `_tb` (traceback 别名) + `_safe_num` (P1-T6 helper) + `get_db_conn` (PIT #119 实战) + `_safe_error_alert`

## 三、跨项目 class-level 铁律 (PIT #124+#125+#130+#131 实战总结)

### 3.1 PIT #124 加密字段约束铁律

- 加密字段 (bytea + pgp_sym_encrypt) **不能用标准 CHECK**, 必用 trigger
- trigger 内 **硬编码 DB_KEY 兜底** (实战 session 无关)
- decrypt 失败 → 跳过 (兼容 PIT #121 空 bytea)
- decrypt 成功但值违规 → 抛错 + ERRCODE 23514 (check_violation)

### 3.2 PIT #125 Streamlit 不被 watchdog 接管铁律

- 任何 .py 修改后, **streamlit 必手动 nohup 重启** (PIT #125 实战新)
- `post_patch_reload.sh` 实战: 脚本内置 `nohup streamlit` 防御
- Streamlit kill -9 后**必等 5-8s** (kernel ps cache 延迟 per PIT #111)

### 3.3 PIT #130 Append-only 表铁律

- 任何 encrypted_positions 实战**禁止 DELETE**, 必用 `is_current = FALSE` 标记
- 测试必带 `code = 'TEST_xxx'` 前缀, 清理用 `UPDATE is_current=FALSE`
- 实战数据治理 (V2.7) 沿用 is_current 业务字段

### 3.4 PIT #131 Schedule_runner 测试铁律

- schedule_runner.py **不能** 直接 import 测试 (fcntl.flock sys.exit)
- 实战方案: ast 静态分析 + exec 单函数源码 + sandbox namespace
- namespace 必含 `_tb` / `_safe_num` / `get_db_conn` / `_safe_error_alert`

## 四、关联 PIT

- **PIT #86**: V25-C `upsert_position` bytea 占位 (PIT #124 实战反向: 负数 cost 防御)
- **PIT #104**: PARTIAL UNIQUE INDEX (V2.7 治理 2.5 已实战)
- **PIT #112**: pgcrypto decrypt 失败 None 兜底 (PIT #124 实战: decrypt 失败跳过)
- **PIT #113**: Python daemon .pyc 固化 (PIT #125 实战: 5 步重启)
- **PIT #114**: Streamlit 也是 daemon (PIT #125 实战新: 必手动 nohup)
- **PIT #119**: cron_task_metrics 自动写 (PIT #120 防御)
- **PIT #120**: 装饰器位置错位 NameError (PIT #124+#131 实战回归)
- **PIT #121**: 18 行 bytea 修复 + 131990 标准券 (PIT #124 实战 decrypt 失败跳过)
- **PIT #122**: V2.7 治理 4 账户重对位 (V2.7.1 防御改进基础)
- **PIT #123**: data-governance 4 步铁律 (V2.7.1 实战固化)
- **PIT #124**: **本文档** — V2.7.1 防御改进 (cost CHECK + reload.sh + final 模板 + test)
- **PIT #125**: Streamlit 不被 watchdog 接管 (6/16 早盘实战新发现)
- **PIT #130**: Append-only 表实战 (DELETE 失败)
- **PIT #131**: schedule_runner 测试不能直接 import (fcntl.flock sys.exit)

## 五、文件清单

- **修改**: PG `holdings.encrypted_positions` (id 10 cost 修复 + 1 trigger 函数 + 1 trigger)
- **新增**: `scripts/post_patch_reload.sh` (200 行, 5 步重启 + 5 步验证)
- **新增**: `scripts/v27_governance_final.py` (11350 字节, 头部 50 行 PIT 铁律注释)
- **新增**: `tests/test_track_cron_task.py` (13.2 KB, 6 场景 PIT #120 回归)
- **新增**: `hermes_coordination/references/pit-124-v271-defensive-improvements.md` (本文档)

## 六、验证

```
=== 防御 1 cost CHECK 实战 ===
  ✅ id 10 588290 cost -4.2041 → 4.2041 修复
  ✅ validate_cost_enc() trigger 创建
  ✅ trg_validate_cost_enc trigger 创建
  ✅ 正数 cost INSERT 成功
  ✅ 负数 cost INSERT 被拒 (PIT #124 防御触发)
  ✅ cost=0 INSERT 成功 (标准券)

=== 防御 2 post_patch_reload.sh 实战 ===
  ✅ 无新 .pyc → 自动退出
  ✅ 模拟新 .pyc → PIT #114+#125 防御检测出

=== 防御 3 v27_governance_final.py 实战 ===
  ✅ 头部 50 行 PIT #123+#124 铁律注释
  ✅ py_compile OK

=== 防御 4 tests/test_track_cron_task.py 实战 ===
  ✅ 6/6 全部通过 🎉
  ✅ track_cron_task (line 279) < first_job (line 368) PIT #120 实战防御
  ✅ 34 个 @track_cron_task 装饰器 PIT #119 实战有效
```

Author: Hermes Agent × aileo
Date: 2026-06-16 07:35
Version: V2.7.1 防御改进 (PIT #124+#125+#130+#131 实战)
