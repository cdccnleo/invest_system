# PIT #120 — @track_cron_task 装饰器位置错位 NameError (Python top-down 解析铁律)

**发现时间**: 2026-06-15 22:39 (PIT #119 实战 commit `966d676` push 后)
**触发场景**: schedule_runner 33941 kill + 等待重启 + 验证 cron_task_metrics
**影响范围**: 全局 — PIT #119 修复 commit `966d676` **完全没生效**, 36 个 cron 任务全部未 wrap
**修复版本**: P1-T7-7 (本次)

## 一、现象

PIT #119 修复 commit `966d676` push 后 (22:33), 重启 schedule_runner 33941, 看 schedule_runner.log 出现反复重启循环:

```
22:38:39 [INFO] schedule_runner 锁获取成功 PID=53518
22:38:39 [INFO] LLMFallbackChain 已加载 (v2.1 补丁7)
Traceback (most recent call last):
  File "/home/aileo/invest_system/scripts/schedule_runner.py", line 270, in <module>
    @track_cron_task("每日健康报告 (08:30)")
     ^^^^^^^^^^^^^^^
NameError: name 'track_cron_task' is not defined
[ERROR] schedule_runner 已在运行（lock 被占），当前进程退出。
```

每个重启都抛 NameError, schedule_runner 死了 → watchdog 重启 → 又死, 进入死循环. 36 个 cron 任务**全部未注册** (因为 `def job_health_report():` 这行抛错, 后面的 `add_job(...)` 都不跑).

## 二、根因

### 2.1 Python 装饰器运行时序 (铁律)

Python 装饰器**不是 lazy**:
```python
@track_cron_task("任务名")   # 这一行在 def 时立即执行
def job_health_report():     # job_health_report 拿到 wrapper
    ...
```

执行顺序:
1. Python 解析到 `@track_cron_task("...")`, 立即调用 `track_cron_task("...")`
2. `track_cron_task(...)` 返回 `decorator(func)` 包装后的 `wrapper`
3. `def job_health_report` 实际定义的是 `wrapper` 函数 (job_health_report 是 wrapper 的引用)
4. **第 1 步要求 `track_cron_task` 已在全局命名空间**

### 2.2 我的位置错位

PIT #119 commit `966d676` 把 `track_cron_task` 函数放在 **line 326** (logger 之后, "推送报告组装" 注释之前):

```
line 270:  @track_cron_task("每日健康报告 (08:30)")
line 271:  def job_health_report():
...
line 317:  logger = logging.getLogger(...)
line 320-411: track_cron_task 函数定义 (93 行)
```

Python 解析 line 270 时, line 320-411 还没执行, `track_cron_task` **未定义** → NameError.

### 2.3 之前没暴露的原因

之前 `track_cron_task` 函数定义**晚于**调用是常见 Python 习惯 (函数体不跑就不需要前置), 但装饰器不同:
- 普通函数 `def foo(): bar()` — def 时只记 body, 调用时再查 bar
- 装饰器 `@dec def foo():` — def 时**立即**查 dec, dec 必须先定义

我之前以为 logger + functools + traceback 都在 line 30 之前 import, **track_cron_task 放在 line 326 应该没问题**, 实际**错**了 — 装饰器函数定义必须在 def 装饰对象之前.

## 三、修复

把 `track_cron_task` 函数定义**移到第一个 `@track_cron_task` 装饰的 def 之前** (line 270 之前, 紧跟 `def _build_health_report_lines` 之后):

```
line 268:  return "\n".join(lines)
line 269:
line 270-360:  # track_cron_task 函数定义 (90 行)
line 361:
line 362:  @track_cron_task("每日健康报告 (08:30)")
line 363:  def job_health_report():
```

## 四、验证

### 4.1 py_compile

```bash
.venv/bin/python3.11 -m py_compile scripts/schedule_runner.py
# OK
```

### 4.2 启动验证

```bash
22:40:33 [INFO]   持仓风险告警 (周一 09:00 周报): 下次 2026-06-22 09:00:00+08:00
22:40:33 [INFO] 调度器守护进程运行中，按 Ctrl+C 退出
```

**36 个 cron 任务全部 schedule 成功** (没 NameError). schedule_runner PID 54952 22:40:31 < pyc 22:40 ✅.

### 4.3 装饰器 wrap 验证 (待 6/16 08:30 实战)

```sql
-- 6/16 08:30 盘前触发后查询
SELECT task_name, status, items_processed, items_failed, duration_seconds
FROM public.cron_task_metrics
WHERE start_time >= '2026-06-16 08:00:00+08'
ORDER BY start_time DESC;
-- 期望: 至少 1 行 "盘前工作流 (08:30)" status=success
```

## 五、经验教训 (跨项目 class-level 铁律)

### 5.1 Python 装饰器位置铁律

**任何 `@dec` 装饰器, `dec` 函数定义必须在 def 装饰对象之前**.

实战验证顺序:
1. 模块顶部 import (line 1-30) ✅
2. 模块顶部常量 (line 30-50) ✅
3. **装饰器函数定义** (line 50-150) ← **必须在这里**
4. 业务函数定义 (line 150+)
5. `if __name__ == "__main__":` 调用 (最后)

如果忘了, 立刻 NameError, 不会"延迟到调用时"才报.

### 5.2 装饰器位置不是"风格问题", 是"运行问题"

我之前觉得"`track_cron_task` 放 logger 之后"是代码组织问题, 实际**影响运行**. 修复必须做, 不能 "觉得" 没问题.

### 5.3 PIT #119 + #120 关联

- PIT #119: 设计缺口 (36 个 cron 任务不写表)
- PIT #120: 修复 PIT #119 时引入的 bug (装饰器位置错位)
- **PIT #113 防御**: 修复后必做 4 步 (commit + push + 重启 + 验证), 这次实战发现 NameError 后又杀 33941, 又启新, 又杀, 死循环 5 次 (restart #2-#5), 全是 PIT #113 实战数据

### 5.4 watchdog 死循环识别

watchdog 日志 "restart #N — previous exit code: 1" 反复出现, 实战表示**修复有 bug**, 不是"调度器问题". 立即查 schedule_runner.log 的 Traceback.

## 六、防御性改进

1. ✅ **track_cron_task 移到 def job_* 之前** (本次)
2. ⏳ **post_patch_reload.sh** (P1-T6 衍生): 修复 schedule_runner.py 后**先 dry-run 启动一次**, 看 [INFO] 调度器守护进程运行中 才算成功
3. ⏳ **CI 钩子**: 跑 `python -c 'import schedule_runner'` 验证模块可加载
4. ⏳ **PIT #120 文档加入 hermes-investpilot-coordination-v2/SKILL.md 索引**

## 七、关联 PIT

- **PIT #113**: .pyc 固化陷阱 — 修复 .py 后必重启 daemon
- **PIT #119**: cron_task_metrics 落库设计缺口 — 触发本次修复
- **PIT #120**: **本文档** — 装饰器位置错位

## 八、文件清单

- **修改**: `scripts/schedule_runner.py` (track_cron_task 函数从 line 326 移到 line 270 之前)
- **新增**: `hermes_coordination/references/pit-120-decorator-position-nameerror.md` (本文档)

Author: Hermes Agent × aileo
Date: 2026-06-15 22:42
Version: P1-T7-7 (PIT #120 修复)
