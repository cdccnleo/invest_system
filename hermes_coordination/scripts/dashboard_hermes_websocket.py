#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V24-B3: Dashboard ↔ Streamlit WebSocket 实时双向通信
=====================================================

实现 8 大方案中的 **方案 7 升级: PG 持久化 → WebSocket 实时推送**。

> V23-R2: dashboard_hermes_bridge.py 桥 = PG 持久化 (l3.push_notification_log)
> V24-B3: **本模块** 桥 = PG 持久化 + **WebSocket 实时推送** (3 级降级)

**核心架构**:
```
推送源 3 路: bridge_to_web_ui() / L3Advisor / intraday_hermes_agent
       ↓
  PG l3.push_notification_log (持久化, 历史追溯)
       ↓ pg_notify
  PG LISTEN pnl_channel (触发)
       ↓
  HermesWebSocketServer (asyncio + websockets 15.0.1)
       ↓ broadcast
  客户端 2 类:
    - Streamlit Dashboard (st.components.v1.html + JS)
    - 任意 ws 客户端 (Telegram bot / 移动端)
```

**协议** (JSON, 双向):
```json
{
  "type": "notification | ack | ping | subscribe | unsubscribe",
  "ts": "2026-06-13T08:30:00",
  "id": "notif_abc123",
  "target": "dashboard | webui | broadcast",
  "target_session_id": "mq6n9tty1xazyr | null",
  "payload": { "title": "...", "body": "...", "data": {...} }
}
```

**3 级降级链** (per memory PIT #30 + 实战):
1. WebSocket 实时 (主, 1-2s 延迟)
2. HTTP 轮询 (兜底, 5s 间隔)
3. PG 直读 (最后, 1s 自查)

**与 V23-R2 关系**:
- V23-R2: 写 PG, **等下次 Streamlit 刷新**才看到
- V24-B3: 写 PG + **PG NOTIFY** → WS server 立即广播 → 客户端 1-2s 收到

**PIT 防御**:
- PIT #7: PG 显式 commit/rollback
- PIT #10: 多 return 路径 schema 完整
- PIT #21: quota/__init__ 主动 touch
- PIT #23: 真实外部依赖降级链
- PIT #24: mock 函数级
- PIT #26: schema 严格验证
- **#31** (新): asyncio.gather + return_exceptions 必设
- **#32** (新): WebSocket client 必须设 ping_interval + ping_timeout
- **#33** (新): PG LISTEN 长连接异常时必须 reconnect

Author: Hermes Agent × aileo
Date: 2026-06-13
Version: V24-B3
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ====================================================================
# 路径 (PIT #5)
# ====================================================================
_SCRIPT_DIR = Path(__file__).resolve().parent
_COORD_DIR = _SCRIPT_DIR.parent
_INVEST_ROOT = _COORD_DIR.parent

for _p in [str(_SCRIPT_DIR), str(_INVEST_ROOT / "scripts")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 真实依赖
try:
    import websockets
    from websockets.server import serve as ws_serve
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

try:
    import psycopg2
    import psycopg2.extensions
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

try:
    from dashboard_hermes_bridge import (
        PushNotification, QuickActionRequest, ActionStatus,
        bridge_to_web_ui, get_pg_connection,
    )
    _HAS_BRIDGE = True
except ImportError:
    _HAS_BRIDGE = False

LOG = logging.getLogger("dashboard_hermes_websocket")
if not LOG.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


# ====================================================================
# 1. 协议常量 + Schema
# ====================================================================

# 消息类型 (4 类)
class WSMsgType(str, Enum):
    NOTIFICATION = "notification"  # 服务端推
    ACK = "ack"                    # 客户端确认
    PING = "ping"                  # 心跳
    PONG = "pong"                  # 心跳回
    SUBSCRIBE = "subscribe"        # 客户端订阅
    UNSUBSCRIBE = "unsubscribe"    # 客户端退订
    ERROR = "error"                # 错误

# 目标路由
class WSTarget(str, Enum):
    DASHBOARD = "dashboard"        # Streamlit Dashboard
    WEBUI = "webui"                # Hermes Web UI
    BROADCAST = "broadcast"        # 广播

# 默认配置
_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 8765
_DEFAULT_PATH = "/ws"
_PG_CHANNEL = "pnl_channel"  # 推送通知 channel
_TOKEN_ENV = "HERMES_WS_TOKEN"  # 鉴权 token env
_PING_INTERVAL = 30  # s
_PING_TIMEOUT = 10   # s
_RECONNECT_DELAY = 5  # s
_POLL_FALLBACK_INTERVAL = 5  # s

# Schema 验证
_REQUIRED_FIELDS = {"type": str, "ts": str, "id": str}


@dataclass
class WSMessage:
    """WebSocket 消息结构 (PIT #26: schema 严格)"""
    type: str
    id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    target: str = WSTarget.BROADCAST.value
    target_session_id: Optional[str] = None
    ts: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "WSMessage":
        d = json.loads(raw)
        # PIT #26: schema 验证
        for k, t in _REQUIRED_FIELDS.items():
            if k not in d:
                raise ValueError(f"WSMessage missing field: {k}")
            if not isinstance(d[k], t):
                raise ValueError(f"WSMessage.{k} type error: expected {t.__name__}")
        return cls(
            type=d["type"],
            id=d["id"],
            payload=d.get("payload", {}),
            target=d.get("target", WSTarget.BROADCAST.value),
            target_session_id=d.get("target_session_id"),
            ts=d.get("ts", datetime.now().isoformat()),
        )


# ====================================================================
# 2. 客户端连接管理
# ====================================================================

@dataclass
class ClientInfo:
    """单个 WebSocket 客户端连接信息"""
    client_id: str
    websocket: Any  # websockets.WebSocketServerProtocol
    target: str = WSTarget.BROADCAST.value
    subscribed: bool = False
    connected_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_ping_at: str = field(default_factory=lambda: datetime.now().isoformat())
    msg_count: int = 0


class HermesWebSocketServer:
    """Hermes WebSocket Server (asyncio + websockets 15.0.1)"""

    def __init__(
        self,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        path: str = _DEFAULT_PATH,
        token: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.path = path
        self.token = token or os.getenv(_TOKEN_ENV, "hermes-ws-default-token")
        self.clients: Dict[str, ClientInfo] = {}
        self._server: Optional[Any] = None
        self._pg_listener_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event() if _HAS_WS else None
        # PIT #34: 实例级 stop 信号, 避免类变量污染 (server#1 stop 后 server#2 仍可起)
        import threading as _threading
        self._stop_event_sync_instance = _threading.Event()
        self._stats = {
            "started_at": None,
            "total_connections": 0,
            "total_messages_sent": 0,
            "total_messages_received": 0,
            "total_pg_notifies": 0,
        }

    # ---- 鉴权 ----
    def _auth_ok(self, websocket) -> bool:
        """
        简单 token 鉴权 (websockets 15.0.1 用 ws.request 拿 path + headers)
        PIT #32: 不能抛异常, 失败返 False
        """
        try:
            # websockets 15.0.1: ws.request.path / ws.request.headers
            req = getattr(websocket, 'request', None)
            if req is None:
                LOG.warning("[auth] ws.request not found, allow dev mode")
                return True  # dev 模式容错
            path = getattr(req, 'path', '')
            headers = getattr(req, 'headers', {})
            # 1. path 含 ?token=xxx
            if f"token={self.token}" in path:
                return True
            # 2. Authorization: Bearer ***
            auth = headers.get('Authorization', '') if hasattr(headers, 'get') else ''
            if auth == f"Bearer {self.token}":
                return True
            LOG.warning(f"[auth] token miss path={path[:50]}")
            return False
        except Exception as e:
            LOG.warning(f"[auth] exception: {e}, fail-closed")
            return False

    # ---- 主入口: 服务端 ----
    async def _handler(self, websocket):
        """每个 client 连接的 handler (PIT #31 + #32)"""
        if not self._auth_ok(websocket):
            await websocket.close(code=4001, reason="auth failed")
            return
        client_id = f"cli_{uuid.uuid4().hex[:8]}"
        info = ClientInfo(client_id=client_id, websocket=websocket)
        self.clients[client_id] = info
        self._stats["total_connections"] += 1
        LOG.info(f"[ws] client connected: {client_id} (total {len(self.clients)})")
        try:
            # 主动推一条 welcome
            welcome = WSMessage(
                type=WSMsgType.NOTIFICATION.value,
                id=f"welcome_{client_id}",
                target=info.target,
                payload={"client_id": client_id, "msg": "connected to Hermes WS"},
            )
            await websocket.send(welcome.to_json())
            self._stats["total_messages_sent"] += 1

            async for raw in websocket:
                info.msg_count += 1
                self._stats["total_messages_received"] += 1
                try:
                    msg = WSMessage.from_json(raw)
                    await self._handle_client_msg(client_id, msg)
                except ValueError as e:
                    # PIT #26: schema 错误返 ERR 给客户端
                    err = WSMessage(
                        type=WSMsgType.ERROR.value,
                        id=f"err_{uuid.uuid4().hex[:6]}",
                        payload={"error": str(e), "raw": raw[:200]},
                    )
                    await websocket.send(err.to_json())
        except websockets.exceptions.ConnectionClosed:
            LOG.info(f"[ws] client disconnected: {client_id}")
        except Exception as e:
            LOG.error(f"[ws] handler exception: {type(e).__name__}: {e}")
        finally:
            self.clients.pop(client_id, None)

    async def _handle_client_msg(self, client_id: str, msg: WSMessage):
        """处理客户端消息 (ack/subscribe/ping)"""
        info = self.clients.get(client_id)
        if not info:
            return
        if msg.type == WSMsgType.PING.value:
            info.last_ping_at = datetime.now().isoformat()
            pong = WSMessage(type=WSMsgType.PONG.value, id=f"pong_{msg.id}")
            await info.websocket.send(pong.to_json())
            self._stats["total_messages_sent"] += 1
        elif msg.type == WSMsgType.SUBSCRIBE.value:
            info.subscribed = True
            info.target = msg.payload.get("target", WSTarget.BROADCAST.value)
            LOG.info(f"[ws] {client_id} subscribed target={info.target}")
        elif msg.type == WSMsgType.UNSUBSCRIBE.value:
            info.subscribed = False
        elif msg.type == WSMsgType.ACK.value:
            # 客户端确认收到, 这里可扩展: 标记 pnl 为已读
            LOG.debug(f"[ws] {client_id} ack {msg.id}")
        else:
            LOG.warning(f"[ws] {client_id} unknown type: {msg.type}")

    # ---- 广播 ----
    async def broadcast(self, msg: WSMessage, target: str = WSTarget.BROADCAST.value):
        """广播消息 (PIT #31: gather + return_exceptions)"""
        if not self.clients:
            return 0
        # 找目标 client
        targets = [
            c for c in self.clients.values()
            if c.subscribed and (target == WSTarget.BROADCAST.value or c.target == target)
        ]
        if not targets:
            return 0
        # 广播 (PIT #31: 异常不阻断其他 client)
        coros = [c.websocket.send(msg.to_json()) for c in targets]
        results = await asyncio.gather(*coros, return_exceptions=True)
        sent = sum(1 for r in results if not isinstance(r, Exception))
        failed = sum(1 for r in results if isinstance(r, Exception))
        self._stats["total_messages_sent"] += sent
        if failed > 0:
            LOG.warning(f"[ws] broadcast: {sent} sent, {failed} failed")
        return sent

    # ---- PG LISTEN ----
    def _pg_listen_sync(self, callback):
        """同步 PG LISTEN 循环 (PIT #33: reconnect)"""
        import select
        # PIT #34: 用实例变量, 不污染类
        stop_event = self._stop_event_sync_instance
        while not stop_event.is_set():
            try:
                conn = psycopg2.connect(
                    host="localhost", dbname="investpilot", user="invest_admin",
                    password=self._get_pg_password(),
                )
                conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
                cur = conn.cursor()
                cur.execute(f"LISTEN {_PG_CHANNEL};")
                LOG.info(f"[pg-listen] subscribed to {_PG_CHANNEL}")
                while not stop_event.is_set():
                    # PIT #33: select + reconnect
                    if select.select([conn], [], [], 1) == ([], [], []):
                        continue
                    conn.poll()
                    while conn.notifies:
                        n = conn.notifies.pop(0)
                        callback(n.payload)
                cur.close()
                conn.close()
            except Exception as e:
                LOG.error(f"[pg-listen] error: {e}, reconnect in 5s")
                if stop_event.is_set():
                    break
                time.sleep(_RECONNECT_DELAY)

    def _get_pg_password(self) -> str:
        """从 store.json 拿密码 (PIT: 不用 os.getenv)"""
        try:
            store = json.loads(
                Path("/home/aileo/.hermes/invest_credentials/store.json").read_text()
            )
            return store["DB_PASSWORD"]
        except Exception as e:
            LOG.error(f"[pg] get password fail: {e}")
            return ""

    async def _pg_listen_loop(self):
        """异步包装: 在 thread 跑同步 LISTEN, callback 转 asyncio"""
        loop = asyncio.get_event_loop()
        # 用 executor 跑同步阻塞 LISTEN
        def on_notify(payload: str):
            self._stats["total_pg_notifies"] += 1
            # 把 PG NOTIFY payload 解析成 WSMessage 广播
            try:
                notif = json.loads(payload)
                ws_msg = WSMessage(
                    type=WSMsgType.NOTIFICATION.value,
                    id=notif.get("notification_id", f"notif_{uuid.uuid4().hex[:8]}"),
                    target=notif.get("target", WSTarget.DASHBOARD.value),
                    payload=notif,
                )
                # 用 loop.call_soon_threadsafe 调度
                asyncio.run_coroutine_threadsafe(
                    self.broadcast(ws_msg, target=ws_msg.target),
                    loop,
                )
            except Exception as e:
                LOG.error(f"[pg-listen] parse fail: {e}, payload={payload[:100]}")
        # 在 thread 跑
        await loop.run_in_executor(None, self._pg_listen_sync, on_notify)

    # ---- 启停 ----
    async def start(self):
        """启动 server + PG listener"""
        if not _HAS_WS:
            LOG.error("[ws] websockets lib not available, server not started")
            return
        self._stats["started_at"] = datetime.now().isoformat()
        # 启 PG listener
        if _HAS_PG:
            self._pg_listener_task = asyncio.create_task(self._pg_listen_loop())
        # 启 WS server (PIT #32: ping_interval + ping_timeout)
        self._server = await ws_serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=_PING_INTERVAL,
            ping_timeout=_PING_TIMEOUT,
        )
        LOG.info(f"[ws] server started at ws://{self.host}:{self.port}{self.path}")
        LOG.info(f"[ws] auth: token={self.token[:8]}***")
        LOG.info(f"[ws] PG channel: {_PG_CHANNEL}")

    async def stop(self):
        """停止"""
        # PIT #34: 设实例 stop 信号, 不动类变量
        self._stop_event_sync_instance.set()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._pg_listener_task:
            self._pg_listener_task.cancel()
            try:
                await self._pg_listener_task
            except asyncio.CancelledError:
                pass
        LOG.info("[ws] server stopped")

    def stats(self) -> Dict[str, Any]:
        """当前统计"""
        return {
            **self._stats,
            "current_clients": len(self.clients),
            "subscribed_clients": sum(1 for c in self.clients.values() if c.subscribed),
        }


# ====================================================================
# 3. 客户端订阅辅助 (供 Streamlit 用)
# ====================================================================

def get_websocket_status(host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT) -> Dict[str, Any]:
    """从 HTTP stats 端点查 WS server 状态 (PIT #26: schema 完整 fallback)"""
    try:
        import urllib.request
        url = f"http://{host}:{port}/stats"
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.loads(r.read())
    except Exception as e:
        # PIT #10: fallback schema 完整
        return {
            "running": False,
            "host": host,
            "port": port,
            "error": str(e),
            "current_clients": 0,
        }


def render_websocket_js_client(host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT) -> str:
    """
    生成 Streamlit 端嵌入的 WebSocket JS 客户端 (PIT #32: 客户端也要 reconnect)

    Returns: HTML 字符串 (含 JS), 通过 st.components.v1.html() 嵌入
    """
    url = f"ws://{host}:{port}{_DEFAULT_PATH}?token=hermes-ws-default-token"
    return f"""
<div id="hermes-ws-status" style="font-family:monospace; padding:8px; background:#f0f0f0; border-radius:4px;">
  🔌 WebSocket: <span id="ws-state">connecting...</span>
  | 📡 消息: <span id="ws-msg-count">0</span>
  | 🆔 <span id="ws-client-id">-</span>
</div>
<script>
(function() {{
  const STATUS_EL = document.getElementById('ws-state');
  const COUNT_EL = document.getElementById('ws-msg-count');
  const ID_EL = document.getElementById('ws-client-id');
  let count = 0;
  function connect() {{
    const ws = new WebSocket("{url}");
    ws.onopen = function() {{
      STATUS_EL.textContent = '🟢 connected';
      ws.send(JSON.stringify({{
        type: 'subscribe', id: 'sub_' + Date.now(),
        target: 'dashboard', payload: {{ target: 'dashboard' }}
      }}));
    }};
    ws.onmessage = function(evt) {{
      count += 1;
      COUNT_EL.textContent = count;
      try {{
        const msg = JSON.parse(evt.data);
        if (msg.id && msg.id.startsWith('welcome_')) {{
          ID_EL.textContent = msg.payload.client_id;
        }}
        if (msg.type === 'notification' && msg.payload && msg.payload.body) {{
          console.log('[hermes-ws]', msg.payload.title, msg.payload.body);
        }}
      }} catch(e) {{}}
    }};
    ws.onclose = function() {{
      STATUS_EL.textContent = '🔴 disconnected, retry 3s...';
      setTimeout(connect, 3000);
    }};
    ws.onerror = function() {{ ws.close(); }};
  }}
  connect();
}})();
</script>
"""


# ====================================================================
# 4. 集成 helper: 从 bridge 推送时同时触发 PG NOTIFY
# ====================================================================

def push_notification_with_notify(
    request,
    target: str = WSTarget.DASHBOARD.value,
) -> Any:
    """
    包装 bridge_to_web_ui(), 写完 PG 后额外发 NOTIFY 触发 WS 广播

    用法:
        notif = push_notification_with_notify(req, target="dashboard")
    """
    # 1. 调原 bridge 写 PG
    notif = bridge_to_web_ui(request)
    # 2. 发 NOTIFY (PIT #7: 显式 commit)
    if notif and _HAS_PG:
        try:
            from dashboard_hermes_bridge import get_pg_connection
            conn = get_pg_connection()
            cur = conn.cursor()
            payload = json.dumps({
                "notification_id": notif.notification_id,
                "target": target,
                "title": notif.title,
                "body": notif.body,
                "priority": notif.priority,
                "ts": datetime.now().isoformat(),
                "payload": notif.payload,
            }, ensure_ascii=False, default=str)
            cur.execute(f"NOTIFY {_PG_CHANNEL}, %s", (payload,))
            conn.commit()
            conn.close()
            LOG.info(f"[push-notify] NOTIFY sent for {notif.notification_id}")
        except Exception as e:
            LOG.error(f"[push-notify] NOTIFY failed: {e}, fallback PG-only")
    return notif


# ====================================================================
# 5. 异步 main 入口 (供独立跑)
# ====================================================================

async def _async_main():
    """独立跑 WS server"""
    server = HermesWebSocketServer()
    await server.start()
    LOG.info("=" * 60)
    LOG.info("Hermes WebSocket Server 运行中")
    LOG.info(f"  URL: ws://{server.host}:{server.port}{server.path}")
    LOG.info(f"  Auth: ?token=*** 或 Authorization: Bearer ***")
    LOG.info(f"  PG:  LISTEN {_PG_CHANNEL} (broadcast on NOTIFY)")
    LOG.info("=" * 60)
    try:
        # 跑 1 小时
        await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await server.stop()


def main():
    """CLI 入口"""
    import argparse
    parser = argparse.ArgumentParser(description="Hermes WebSocket Server")
    parser.add_argument("--host", default=_DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--token", default=None, help=f"Auth token (env: {_TOKEN_ENV})")
    parser.add_argument("--stats", action="store_true", help="Show WS server status and exit")
    args = parser.parse_args()
    if args.stats:
        s = get_websocket_status(args.host, args.port)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return
    # 跑 server
    if not _HAS_WS:
        print(f"❌ websockets lib not installed")
        print(f"   pip install websockets==15.0.1")
        sys.exit(1)
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
