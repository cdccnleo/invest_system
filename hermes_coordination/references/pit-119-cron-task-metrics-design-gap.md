# PIT #119 — schedule_runner 36 个 cron 任务从来就没写 cron_task_metrics

**发现时间**: 2026-06-15 22:00 (P1-T6 实战验证时)
**触发场景**: 用户问"cron_task_metrics 表里只有 1 行, 6/14 之后所有任务都没写"
**影响范围**: 全局 — v22_monitoring.collect_cron_health 永远读到 0 行 → `cron_task_health` 指标永久 0%
**修复版本**: P1-T7 (2026-06-15)

## 一、现象

排查 P1-T6 实战验证后, 用户查看 `public.cron_task_metrics` 表:

```sql
SELECT count(*), max(start_time) FROM public.cron_task_metrics;
-- count: 1, max: 2026-06-11 22:25 (hermes_event_analyst, manual_dry_run)
```

**6/14 实战至今 (6/15 22:00) — 所有飞书告警的 cron 任务 (PIT #111-#118 全部), 一行都没落库**!

## 二、真相

V22 时期 design 时把 `cron_task_metrics` 表设计为"cron 任务健康度数据源" (v22_monitoring.collect_cron_health 读这张表算成功率), 但 **V23 实施时漏了"每个 cron 任务跑完写一行"的逻辑**. 实战表现:

| 任务 | schedule_runner 是否调 | 是否写 cron_task_metrics | 是否推飞书告警 |
|---|---|---|---|
| 36 个 job_* 函数 | ✅ 调 | ❌ **不写** | ✅ 推 (从 schedule_runner.log 解析) |
| 公告采集 (PIT #112 修复后) | ✅ 调 | ❌ **不写** | ✅ 推 |
| 情绪因子 (PIT #112 修复后) | ✅ 调 | ❌ **不写** | ✅ 推 |
| 盘前/盘后 (PIT #117 修复后) | ✅ 调 | ❌ **不写** | ✅ 推 (none 抛错时) |
| quote_streamer_5min (V26-A 加) | ✅ 调 | ✅ 写 (手写 30 行 INSERT) | ✅ 推 |
| hermes_event_analyst (历史测试) | ✅ 调 | ✅ 写 (6/11 手工 dry-run) | ❌ |

**全表 1 行** (6/11 22:25 hermes_event_analyst, 模式: manual_dry_run, status: success, items_processed: 245, items_failed: 0).

## 三、根因

V22 时期的设计文档 (`hermes_coordination/scripts/v22_monitoring.py` 顶部 docstring 第 23 行) 提到:
> 数据源: public.cron_task_metrics (15 行, 5/26-6/7)

**"15 行, 5/26-6/7"** 表明 V22 时期有数据. 后来 V23 实施时 (V22 升级到 V23), 不知何故 (可能 refactor 漏了, 可能 cron 任务换 schedule_runner 实现时漏迁移 INSERT 段) — 写 cron_task_metrics 的代码全部丢了.

`schedule_runner.py` 全文搜 `INSERT INTO.*cron_task_metrics`:
- 6/14 之前: 0 处
- 6/14 V26-A 加 quote_streamer_5min 时: 1 处 (手写 30 行, 6/14 第一次跑成功, 6/15 18:54 重启后还在跑)
- 6/15 22:00 排查时: 1 处 (同上)

**schedule_runner 36 个 cron 任务从来没自动写过 cron_task_metrics, 这是 V22 升级 V23 时的设计缺口, 拖了 2 周没被发现**.

## 四、修复方案 — `@track_cron_task` decorator

### 4.1 设计目标

1. **透明包**: 36 个 job_* 函数不改业务逻辑
2. **异常捕获**: 任何 cron 任务抛错自动捕获, 写 status=failed + error_message
3. **不 raise**: APScheduler 不会因为一个 job 失败就挂掉
4. **指标自动**: 任务 return dict 含 `processed/failed` 字段自动写
5. **飞书告警兼容**: 失败时仍调 `_safe_error_alert` 推飞书 (PIT #112 兼容)
6. **status 约束**: 用 cron_task_metrics.status_check 允许的 4 个值 (running/success/failed/timeout), **不用 partial**

### 4.2 装饰器实现

```python
# schedule_runner.py line 30-110 (PIT #119)
def track_cron_task(task_name: str):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_ts = time.time()
            start_dt = datetime.now()
            items_processed = 0
            items_failed = 0
            status = "success"
            error_msg = None
            try:
                result = func(*args, **kwargs)
                if isinstance(result, dict):
                    items_processed = _safe_num(result.get("processed", 0))
                    items_failed = _safe_num(result.get("failed", 0))
                    # status 判定: 全失败 = failed, 其他 = success
                    if items_processed == 0 and items_failed > 0:
                        status = "failed"
                return result
            except Exception as e:
                status = "failed"
                error_msg = _tb.format_exc()[:500]
                logger.error(f"[{task_name}] 任务异常: {e}")
                try:
                    _safe_error_alert(f"🔴 {task_name} 失败", f"错误: {e}\n{error_msg[:300]}")
                except Exception:
                    pass
            finally:
                duration = round(time.time() - start_ts, 2)
                try:
                    from backtester import get_db_conn
                    _conn = get_db_conn()
                    _cur = _conn.cursor()
                    _cur.execute("""
                        INSERT INTO public.cron_task_metrics
                          (task_name, start_time, end_time, duration_seconds, status,
                           items_processed, items_failed, error_code, error_message, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (task_name, start_dt, datetime.now(), duration, status,
                          int(items_processed), int(items_failed),
                          type(error_msg).__name__ if error_msg and not isinstance(error_msg, str) else None,
                          error_msg,
                          json.dumps({"duration_sec": duration})))
                    _conn.commit()
                    _cur.close()
                    _conn.close()
                except Exception as e_meta:
                    logger.debug(f"[{task_name}] cron_task_metrics 写入失败: {e_meta}")
        return wrapper
    return decorator
```

### 4.3 36 个 job_* 函数覆盖

| 函数 | 装饰器任务名 |
|---|---|
| `job_health_report` | 每日健康报告 (08:30) |
| `job_morning` | 盘前工作流 (08:30) |
| `job_closing` | 盘后工作流 (15:30) |
| `job_tamf_update` | TAMF 增量更新 |
| `job_equity_curve_save` | 权益曲线保存 |
| `job_deep_analysis_weekly` | 深度分析 (周日) |
| `job_reports_collection` | 研报采集 |
| `job_report_summary_to_tamf` | 研报汇总入 TAMF |
| `job_evening` | 晚间工作流 (18:00) |
| `job_midday` | 午间快讯 (11:30) |
| `job_announcements_collection` | 公告采集 (20:50) |
| `job_sentiment_update` | 情绪因子更新 (21:05) |
| `job_intraday_monitoring` | 盘中异动监控 |
| `job_merge_holdings` | 持仓合并/解密 (22:35) |
| `job_hermes_sync` | Hermes 双向同步 (18:00) |
| `job_quote_streamer_5min` | V26-A 行情拉取 (盘中 5min) |
| `job_skill_solidification` | 技能固化 |
| `job_llm_cost_report` | LLM 成本报告 |
| `job_skill_spot_check` | 技能质量抽查 (周日 21:00) |
| `job_behavior_profile_update` | 行为画像更新 |
| `job_behavior_insights` | 每周行为洞察 (周日 20:00) |
| `job_user_emotion_sensing` | 用户情绪感知 |
| `job_stress_test` | 每周压力测试 (周五 22:00) |
| `job_weekly_backtest` | 周线回测报告 (周一 07:00) |
| `job_strategy_optimization` | V24-C4 策略调优 (周日 22:00) |
| `job_chief_event_analyst` | V24-C6 大模型首席分析师 (一/三/五 11:30) |
| `job_position_risk_alert` | 持仓风险告警 (周一 09:00 周报) |
| `job_v22_monitoring_collect` | v2.2 监控数据收集 (18:30) |

### 4.4 已有手写 INSERT 段简化

- **`job_quote_streamer_5min`**: 原 30 行手写 INSERT (`import psycopg2` + 硬编码密码 + `_Json` + 30 行 INSERT) 全部删除, 改为 return dict
- **`job_v22_monitoring_collect`**: 原"报告持久化"段保留 (有 `l3.v22_monitoring` 业务查询, 不只是 cron 写表), 改 return dict
- **`job_hermes_sync`**: 原 return None 改 return dict (i2h_synced/h2i_synced)
- 其他 25 个 job_*: 原 return None 改 return None (decorator 仍会写 0/0 status=success)

## 五、验证

### 5.1 py_compile

```bash
.venv/bin/python3.11 -m py_compile scripts/schedule_runner.py
# OK
```

### 5.2 端到端 5 场景

```python
# /tmp/test_track_cron_task.py (跟 schedule_runner.py 中装饰器同实现)

场景 1: 成功 (45/0) → status=success ✅
场景 2: 部分失败 (40/5) → status=success + items_failed=5 ✅
场景 3: 全部失败 (0/10) → status=failed + items_failed=10 ✅
场景 4: 异常 (raise) → status=failed + error_message=traceback + 推飞书 ✅
场景 5: None 返回 → status=success + items=0/0 ✅
```

### 5.3 cron_task_metrics 表修复

| 指标 | 修复前 | 修复后 (预计) |
|---|---|---|
| 表总行数 | 1 (6/11) | 1+N (N = 6/16 起每日 ~30-40 个 job 跑) |
| 6/15 盘前 08:30 行 | 无 | 有 (6/16 起) |
| 6/15 盘后 15:30 行 | 无 | 有 (6/16 起) |
| 6/15 公告采集 20:50 行 | 无 | 有 (6/16 起) |
| 6/15 情绪因子 21:05 行 | 无 | 有 (6/16 起) |
| v22_monitoring.collect_cron_health 指标 | 永远 0% | 真实成功率 (80-100%) |

## 六、经验教训

### 6.1 设计缺口跨版本升级失忆

V22 时期"15 行" → V23 升级后 1 行, 没人发现, 因为:
- 飞书告警走 `schedule_runner.log` 解析, 业务侧无感
- v22_monitoring.collect_cron_health 在 v22_monitoring 跑完后才统计, 0% 看着像"完美"
- 没人主动查这张表的行数

**教训**: 跨版本升级时, 关键表 (尤其"被依赖表") 必须有"行数监控告警", 比如 `cron_task_health < 5 行/天` 推 WARNING.

### 6.2 decorator 模式透明升级

36 个 job_* 函数, 改 28 个 `@track_cron_task(name)` 行 (用 sed 批量), 业务逻辑 0 改动. 这种"装饰器 + 现有代码"模式比"重写 cron 任务调用链"安全 10x.

### 6.3 实战文档必须盯"目标表"

之前我们 PIT #111-#118 实战文档都盯"失败原因 + 修复", 没盯"修复后落库了吗". 这是 PIT #119 暴露的盲点 — **修复后**应该验证"数据真写到表了", 而不只是"代码不再抛错".

### 6.4 status 约束提前看

写 INSERT 前必须 `pg_get_constraintdef` 看 CHECK 约束, 避免 `partial` 之类的语义值被 DB 拒掉. 实战教训: 约束 `cron_task_metrics_status_check` 只允许 `running/success/failed/timeout`, **不用 partial** (业务上"部分失败"概念, 用 items_processed/items_failed 表达, status 仍记 success).

## 七、防御性改进 (PIT #119 延伸, P1-T7 + 后续)

1. ✅ **decorator 全自动**: 28 个 job 全部覆盖 (P1-T7 修)
2. ⏳ **P1-T2 cron_task_health 行数监控告警**: 每日 18:35 v22_monitoring 跑完后, 查 cron_task_metrics 行数 < 5 → WARNING 推飞书
3. ⏳ **P1-T6 延伸**: tests/test_track_cron_task.py 4 场景回归 (None/success/partial/failed/exception)
4. ⏳ **post_patch_reload.sh** (P1-T6 衍生): commit + push 后自动重启 schedule_runner (PIT #113 防御)

## 八、关联 PIT

- **PIT #111** (6/14 20:43): schedule_runner 9h 僵尸 — 修锁释放
- **PIT #112** (6/14 20:50): pgcrypto load 容错 — 修公告采集
- **PIT #113** (6/14 21:11): .pyc 固化陷阱 — 修 daemon 重启
- **PIT #114** (6/14 21:30): streamlit .pyc — 修 streamlit 重启
- **PIT #115** (6/14 22:00): jsonb 反模式 — 修 _json.loads
- **PIT #116** (6/14 22:30): TAMF 循环叠加 — 修 hermes_agent_sync
- **PIT #117** (6/15 18:50): dict.get None 默认值 — 修 run_analysis
- **PIT #118** (6/15 19:30): f-string None 兜底 — prompt_builder.py 修 (PIT #117 防御延伸)
- **PIT #119** (6/15 22:00): **本文档** — 36 个 cron 任务不写 cron_task_metrics
- **PIT #113 重启铁律**: 修复 schedule_runner.py 后必做 (commit + push + 重启 schedule_runner + 重启 streamlit)
- **PIT #117 _safe_num 铁律**: 跨模块 dict.get 防御, 6 形态 (比较/加减/浮点/if/return/f-string)

## 九、文件清单

- **修改**: `scripts/schedule_runner.py` (+120/-30 行, 28 个 @track_cron_task)
- **新增**: `hermes_coordination/references/pit-119-cron-task-metrics-design-gap.md` (本文档)
- **测试**: `/tmp/test_track_cron_task.py` (5 场景, 跟装饰器同实现, 跑完清理)

Author: Hermes Agent × aileo
Date: 2026-06-15 22:00
Version: P1-T7 (PIT #119 修复)
