# PIT #132: quote_streamer timeout=300s 不够 (6/16 实战)

> 实战时间: 2026-06-16 15:00 行情拉取
> 根因: baostock 服务器 (114.94.20.73:10030) hang, 单次拉取 304s 触发 300s timeout
> 修复: timeout=300s → 600s (5min cron 周期 + buffer)

## 实战时间线

| 时间 | 实战 | cron_task_metrics |
|---|---|---|
| 6/16 14:30 | 正常, 236s | success p=39 |
| 6/16 14:35 | 正常, 184s | success p=39 |
| 6/16 14:40 | 正常, 132s | success p=39 |
| 6/16 14:45 | 正常, 186s | success p=39 |
| 6/16 14:50 | 正常, 188s | success p=39 |
| 6/16 14:55 | 正常, 189s | success p=39 |
| **6/16 15:00** | **304s 超时** | **failed p=0 f=1** (🔴 飞书告警) |
| 6/16 15:10 | 恢复, 196s | success p=39 |

## 根因分析

1. **baostock 服务器 114.94.20.73:10030 临时 hang** (TCP ESTAB + wchan=wait_woken)
2. **45 标的 / 28 stock + 11 fund, baostock `query_history_k_data_plus` 单次 hang**
3. **实战 14:30-14:55 平均 180-240s, 15:00 触发 304s** (> 300s timeout)
4. **15:10 触发 baostock 自愈, 196s 成功**
5. **PIT #119 decorator 实战有效**: failed 状态正确写入 cron_task_metrics

## 修复

```python
# schedule_runner.py:1642-1651
proc = _sp_qs.run(
    [str(venv_py), str(qs_script)],
    capture_output=True, text=True, timeout=600,  # PIT #132 6/16 实战: baostock hang 304s, 300s 不够, 5min cron 周期 + buffer
)
except _sp_qs.TimeoutExpired as e:
    logger.error(f"quote_streamer 超时 600s: {e}")
    _safe_error_alert("🔴 行情拉取超时", f"quote_streamer 跑 600s 还没结束, baostock hang")
    send_job_failure("行情拉取 (5min)", "timeout 600s")
    return {"processed": 0, "failed": 1, "error": f"timeout_600s: {e}"}
```

## 跨项目 class-level 铁律 (PIT #132)

1. **5min cron 周期的子进程 timeout 必 ≥ 5min (300s) + 100% buffer (600s)**
2. **实战平均 180-240s, P99 可能 300s+**, 5min cron 重叠风险 → timeout 必 ≥ 600s
3. **TimeoutExpired 必显式 except**: PIT #119 decorator 不会捕获 TimeoutExpired (subprocess 内部), 必显式处理
4. **fcntl.flock 锁是最后一道防线**: 实战 15:05 触发 (5min 后), quote_streamer 131264 仍占锁 304s, 实战 15:05 子进程拿不到锁直接 skip (line 3039)
5. **下游 cron 重叠时, fail-fast 优于 hang**: 实战 15:00 failed 但 15:05 5min 周期内锁 skip, 15:10 真正成功

## 验证 (6/16 实战)

- 6/16 15:14 触发: 154s success p=39 f=0 (新 timeout=600s 还没加载, 实战 304s 后 15:20 触发未到, 等下次实战)
- 实战 PIT #119 decorator 写 cron_task_metrics 实战正确 (failed + success 状态)
- PIT #124 cost CHECK trigger 实战 6/16 早盘 88 次 cron 成功 ✅
- 6/16 6/16 中盘 73 次行情拉取, 72 成功 + 1 failed (15:00 304s), 实战 98.6% 成功率

## 后续

- **V2.7.2 TODO**: 行情拉取每个标的加 timeout (单标 10s, 45 标 × 4s buffer = 180s)
- **V2.7.2 TODO**: baostock API 健康检查 cron (5min 触发, 1 login 验证), 提前告警
- **V2.7.2 TODO**: 飞书告警分级 (failed > 0 WARNING, 全失败 CRITICAL)
