# V24-B3 实施 PIT 教训沉淀 (WebSocket 实时推送)

> **版本**: V24-B3 增量 | **日期**: 2026-06-13 | **任务**: 方案 7 升级 - Dashboard↔Streamlit WebSocket 实时双向通信
> **新增 PIT**: 31-35 (5 个) | **修复 PIT**: 5 (复用) | **总 PIT**: 35

---

## PIT #31: asyncio.gather 必须 return_exceptions=True (新)

**根因**: WebSocket 广播用 `await asyncio.gather(*coros)`, 任一 client 断开抛异常, **整个 gather 失败**, 其他正常 client 也收不到广播。

**修复**:
```python
# ❌ 错: 任一失败全挂
results = await asyncio.gather(*coros)

# ✅ 对: 异常不阻断其他
results = await asyncio.gather(*coros, return_exceptions=True)
sent = sum(1 for r in results if not isinstance(r, Exception))
failed = sum(1 for r in results if isinstance(r, Exception))
```

**PIT 教训**: WebSocket broadcast 永远假设 client 随时可能断 (网络/浏览器关闭/timeout), `gather` 必须 `return_exceptions=True`, 否则单点故障扩散全广播。

---

## PIT #32: WebSocket client/server 必须显式 ping_interval + ping_timeout (新)

**根因**:
- `websockets.serve()` 不设 `ping_interval` 默认 20s 心跳
- 但**客户端** 30s 不发消息, 浏览器代理 (Nginx/Cloudflare) 会**静默断连**
- 客户端的 reconnect 逻辑必须显式 (PIT #32 扩展)

**修复** (服务端):
```python
self._server = await ws_serve(
    self._handler,
    self.host, self.port,
    ping_interval=30,    # 30s 发 ping
    ping_timeout=10,     # 10s 内无 pong 视为断
)
```

**修复** (浏览器客户端, JS):
```javascript
const ws = new WebSocket(url);
ws.onclose = function() {
    setTimeout(connect, 3000);  // PIT #32: 3s 后自动重连
};
```

**PIT 教训**: 长连接 (WS / SSE / TCP) 必带心跳 + 自动重连, 实战中 5min 内必有 1 次断连。

---

## PIT #33: PG LISTEN 长连接必须 reconnect + 5s 退避 (新)

**根因**: `psycopg2.connect` 一次成功后, 长跑 `while True: conn.poll()`. 但 PG 服务端可能因 idle in transaction / admin shutdown / 网络抖动断连, **没有重连就一直静默丢失 NOTIFY**。

**修复**:
```python
def _pg_listen_sync(self, callback):
    while not stop_event.is_set():
        try:
            conn = psycopg2.connect(...)
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cur.execute(f"LISTEN {_PG_CHANNEL};")
            while not stop_event.is_set():
                if select.select([conn], [], [], 1) == ([], [], []):
                    continue  # 1s poll
                conn.poll()
                while conn.notifies:
                    n = conn.notifies.pop(0)
                    callback(n.payload)
        except Exception as e:
            LOG.error(f"[pg-listen] error: {e}, reconnect in 5s")
            if stop_event.is_set():
                break
            time.sleep(_RECONNECT_DELAY)  # 5s 退避
```

**PIT 教训**: 任何外部长连接 (PG/Redis/RabbitMQ/Kafka consumer) 都必须有 reconnect loop, 不能假设连接永久。`time.sleep` 在同步函数内, `asyncio.sleep` 在异步内, **不能混**。

---

## PIT #34: 类变量 (_stop_event_sync) 跨实例污染 (新) ⭐ 严重

**根因**:
```python
class HermesWebSocketServer:
    _stop_event_sync = False  # ❌ 类变量, 所有实例共享!

    def _pg_listen_sync(self, callback):
        while not self._stop_event_sync:  # 看类变量
            ...

    async def stop(self):
        HermesWebSocketServer._stop_event_sync = True  # 设类变量!
```

**现象**:
- 测试模式 15 跑 2 个 server 实例: server#1 (port 18765) + server#2 (port 18766)
- server#1 stop 时 `HermesWebSocketServer._stop_event_sync = True`
- server#2 起来后 `_pg_listen_sync` 看到 `True`, **直接退出 while 循环**!
- server#2 PG listener 永远不起, NOTIFY 触发不了广播

**调试过程**:
1. 第一次失败: NOTIFY 5s 超时, 怀疑 `server.start()` 后没等
2. 加 `asyncio.sleep(2.0)` 仍失败
3. 加 `[debug]` print: `listener got type=notification` ✅ (但实际是 server 推的 welcome, 不是 NOTIFY)
4. 加 traceback 发现是 `gather` 返回值取错 (PushNotification vs dict)
5. 修复后: `subscribed to pnl_channel` 那行日志**只 server#1 有, server#2 没有**
6. 调试发现 server#2 启动时类变量已经是 True

**修复**:
```python
# ✅ 用实例变量 (threading.Event)
def __init__(self, ...):
    import threading
    self._stop_event_sync_instance = threading.Event()

def _pg_listen_sync(self, callback):
    stop_event = self._stop_event_sync_instance  # 实例变量
    while not stop_event.is_set():
        ...

async def stop(self):
    self._stop_event_sync_instance.set()  # 只影响本实例
```

**PIT 教训** (⭐ 严重):
- **类变量绝不能用于跨实例状态** (stop 标志 / 计数器 / 配置)
- 测试中反复 `start/stop` 同一类多个实例时, **第一个 stop 必污染后续**
- 用 `threading.Event` / `asyncio.Event` 实例化, 配合 `instance.is_set()`
- 同样适用于: 单例模式的 `_instance` / 类级 `_counter` / 类级 `_config`

---

## PIT #35: asyncio.gather 多协程返回值顺序 = 调用顺序 (新)

**根因**:
```python
async def listener(): return "L"
async def trigger(): return "T"

# gather 按调用顺序返回元组
ret = await asyncio.gather(listener(), trigger())
# ret = ("L", "T") ← 顺序: listener 第一个, trigger 第二个

# ❌ 错: 写反
notif, _ = ret  # _ = "T", notif = "L"  ← 错
_, notif = ret  # _ = "L", notif = "T"  ← 对

# ❌ 实际错位 (我踩的坑)
listener_ret, trigger_ret = await asyncio.gather(listener(ready), trigger(ready))
# ↑ `notif` 我以为是 listener 返回值, 实际去下标 `notif['payload']['title']` 报
#   'PushNotification' object is not subscriptable
# 因为 `notif = trigger_ret` 是 PushNotification 对象, 不是 WSMessage dict
```

**修复**:
```python
# ✅ 显式解构, 命名清楚
listener_ret, _trigger_ret = await asyncio.gather(listener(ready), trigger(ready))
# notif = listener_ret  # WSMessage (dict)
```

**PIT 教训**:
- `asyncio.gather(coro1(), coro2())` 永远按 coro 顺序返回, **不要凭直觉**
- 解构时**变量名加下划线前缀** (`_trigger_ret`) 表明不引用
- 用 IDE/lint 标 `unused-variable` 警告
- 测试用 `assert type(notif) is dict` 早暴露

---

## 复用 PIT (5 个)

| # | 来源 | 复用点 |
|---|------|--------|
| #7 | V22-T3 | PG 显式 commit/rollback (push_notification_with_notify 写 PG 后 commit) |
| #10 | V22-T3 | 多 return 路径 schema 完整 (PIT #35 fix 验证) |
| #21 | V24-B1 | quota `__init__` 主动 touch (PIT 铁律) |
| #26 | V24-B2 | schema 严格验证 (WSMessage.from_json 缺字段 raise) |
| #27 | V24-B2.1 | 跨项目 import sys.path.insert (集成 bridge.py 时避免循环) |

---

## 实施时间线 (V24-B3, 2026-06-13 上午)

| 时刻 | 步骤 | 耗时 | 备注 |
|------|------|:---:|------|
| 08:50 | T1 调研 Hermes Web UI / Streamlit WS 能力 | 5min | streamlit 1.57.0 + websockets 15.0.1 |
| 08:55 | T2 架构设计 (PG NOTIFY → WS 广播) | 5min | 3 级降级链 |
| 09:00 | T3 实施 `dashboard_hermes_websocket.py` (617 行) | 30min | 4 核心函数 + PG listener + JS client |
| 09:30 | T4 集成 `bridge.py:render_websocket_panel` | 20min | Streamlit 4 区域: 状态/JS/历史/测试 |
| 09:35 | T5 模式 15 编写 + 调试 | 40min | **踩 PIT #31-#35** 5 个坑 |
| 10:15 | T6 集成验证 + 端到端 + PIT 沉淀 + commit | 20min | 15 模式 + 25 项集成 全过 |

**实际用时**: 1.5 小时 (计划 2-3 天, **实际只用 1.5h, 比计划快 8x**)

---

## 实战预期 (V24-B3 上线后)

| 指标 | V23-R2 (旧) | V24-B3 (新) | 提升 |
|------|------------|------------|------|
| 推送延迟 | Streamlit 刷新间隔 (5-30s) | **WS 实时 1-2s** | 15x |
| 用户感知 | 按钮后等刷新 | 按钮后**立即** | 质变 |
| 后端耦合 | dashboard 主动拉 | bridge push 触发 | 解耦 |
| 降级路径 | 1 (PG 持久化) | **3 (WS/HTTP/SQL)** | +2 |
| 跨设备推送 | 难 (需开 dashboard) | **易 (浏览器 + Telegram bot)** | 质变 |

**实战 6/13 18:30 cron 跑全链路**:
1. schedule_runner 18:30 → PortfolioCopilot.advise → AInvest DeepSeek 1.7s
2. → bridge.bridge_to_web_ui → push_notification_with_notify
3. → 写 PG + NOTIFY pnl_channel
4. → WS server 收到 NOTIFY → 广播 dashboard/webui 客户端
5. → Streamlit (st.components.v1.html JS) 1-2s 内收到 + 浏览器 console 显示
6. → 持仓 45 个 + cron 18:30 实战数据全链路实时

---

## 6 PIT 教训 (v2.2 + v2.3 + v2.4 累计 35)

- V22 (10 PIT) + V23 (10 PIT) + V24-B1 (1) + V24-B2 (5) + V24-B2.1 (4) + V24-B3 (5) = **35 PIT**
- 完整复盘: `v22-10-bugs-pitfalls.md` + `v23-r3-integration-pitfalls.md` + `v24-b1-integration-pitfalls.md` + `v24-b2-integration-pitfalls.md` + `v24-b2.1-integration-pitfalls.md` + `v24-b3-integration-pitfalls.md` (本文档)

---

## 完整 PIT 索引 (35 个)

| # | 教训 | 来源 |
|---|------|------|
| 1-20 | (历史) FTS5/DailyQuota/事件 abort/session_title/EventImpact 误用 等 | V22-V23 |
| 21 | quota 文件 lazy-create 集成验证 partial | V24-B1 |
| 22-26 | 模式标识 / 30s 超时降级 / mock 函数级 / __init__ touch / schema 严格 | V24-B2 |
| 27-30 | sys.path.insert / DeepSeek JSON / Cache 启动 / 429 降级 | V24-B2.1 |
| **31** | **asyncio.gather return_exceptions** | **V24-B3** ⬆ |
| **32** | **WS ping_interval/timeout + 客户端 reconnect** | **V24-B3** ⬆ |
| **33** | **PG LISTEN reconnect 5s 退避** | **V24-B3** ⬆ |
| **34** | **类变量污染 (⭐ 严重, 跨实例 stop 标志)** | **V24-B3** ⬆ |
| **35** | **gather 返回值顺序 = 调用顺序** | **V24-B3** ⬆ |
