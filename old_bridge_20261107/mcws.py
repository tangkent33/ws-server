"""WebSocket 伺服器：接收橋接器事件、送出指令、去重、玩家狀態。"""
import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set

import websockets
from websockets import serve
from websockets.legacy.server import WebSocketServerProtocol

from .config import (ENABLE_BAN_ALERT, ENABLE_CHAT, ENABLE_DEATH, ENABLE_JOIN_LEAVE, ENABLE_TP_ALERT,
                     MC_PLAYER_NAME, ONE_WAY_MODE, log)
from .textutil import extract_text, is_ui_spam, strip_colors

DEDUP_WINDOW = 5.0          # 秒：兩個來源可能送同一件事


@dataclass
class PendingCommand:
    future: asyncio.Future
    timestamp: float


class MCWebSocketServer:
    """對外是「Minecraft 事件來源」，對內把事件轉給 handler。"""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

        self._clients: Set[WebSocketServerProtocol] = set()
        self._bridge_ws = None                  # 橋接器的連線（不是遊戲）
        self._pending: Dict[str, PendingCommand] = {}
        self._connected = asyncio.Event()
        self._server_task: Optional[asyncio.Task] = None
        self._seen: Dict[str, float] = {}

        self.players: Set[str] = set()

        # 事件回呼，由 app 接上
        self.on_chat: Optional[Callable] = None
        self.on_join: Optional[Callable] = None
        self.on_leave: Optional[Callable] = None
        self.on_death: Optional[Callable] = None
        self.on_tp: Optional[Callable] = None
        self.on_tp_join: Optional[Callable] = None
        self.on_ban: Optional[Callable] = None
        self.on_connected: Optional[Callable] = None
        self.on_disconnected: Optional[Callable] = None

    # ------------------------------------------------ 對外狀態
    @property
    def connected(self) -> bool:
        return bool(self._clients)

    def game_clients(self):
        """扣掉橋接器，剩下的才是遊戲本體那條連線。"""
        return [c for c in self._clients if c is not self._bridge_ws]

    # ------------------------------------------------ 生命週期
    async def start(self):
        if self._server_task and not self._server_task.done():
            return
        self._server_task = asyncio.create_task(self._run_server())
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=30)
            log.info("✅ Minecraft 橋接已連線")
        except asyncio.TimeoutError:
            log.info(f"⏳ 橋接器還沒連上，仍在監聽 {self.host}:{self.port}")

    async def stop(self):
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass

    async def _run_server(self):
        async with serve(self._on_connect, self.host, self.port,
                         ping_interval=20, ping_timeout=10, max_size=2 ** 20):
            log.info(f"✅ WebSocket 伺服器已啟動 {self.host}:{self.port}")
            await asyncio.Future()

    async def _on_connect(self, ws: WebSocketServerProtocol):
        self._clients.add(ws)
        self._connected.set()
        log.info(f"🟢 連線進入 {ws.remote_address}")
        await self._fire(self.on_connected)

        try:
            async for raw in ws:
                await self._handle_message(raw, ws)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            log.error(f"接收迴圈錯誤: {e}")
        finally:
            self._clients.discard(ws)
            if not self._clients:
                self._connected.clear()
                self.players.clear()
                log.warning("🔴 所有連線已斷開")
                await self._fire(self.on_disconnected)

    @staticmethod
    async def _fire(handler):
        if not handler:
            return
        try:
            await handler()
        except Exception:
            pass

    # ------------------------------------------------ 收訊息
    async def _handle_message(self, raw: str, ws=None):
        try:
            data = json.loads(raw)
        except Exception:
            return

        header = data.get("header", {}) or {}
        event = header.get("eventName") or data.get("eventName") or ""
        purpose = header.get("messagePurpose", "")
        body = data.get("body", {}) or {}

        if event == "BridgeHello":
            self._bridge_ws = ws
            return

        if purpose == "commandResponse":
            self._resolve_pending(header.get("requestId"), body)
            return

        if self._duplicated(event, body):
            return

        try:
            await self._dispatch(event, body)
        except Exception as e:
            log.error(f"事件處理錯誤 ({event}): {e}")

    def _resolve_pending(self, req_id, body):
        if not req_id or req_id not in self._pending:
            return
        pending = self._pending.pop(req_id)
        if not pending.future.done():
            pending.future.set_result(str(body.get("statusMessage") or "指令已送出"))

    def _duplicated(self, event: str, body: dict) -> bool:
        player = (body.get("player") or {}) if isinstance(body.get("player"), dict) else {}
        key = f"{event}|{body.get('message') or body.get('sender') or player.get('name') or ''}"
        now = asyncio.get_running_loop().time()

        if self._seen.get(key, 0) > now - DEDUP_WINDOW:
            return True
        self._seen[key] = now
        if len(self._seen) > 500:
            self._seen = {k: v for k, v in self._seen.items() if v > now - 60}
        return False

    async def _dispatch(self, event: str, body: dict):
        if event == "PlayerMessage":
            await self._on_player_message(body)

        elif event == "PlayerJoin":
            await self._on_player_join(body)

        elif event == "PlayerLeave":
            await self._on_player_leave(body)

        elif event == "PlayerDied":
            name = (body.get("player") or {}).get("name") or ""
            if name and ENABLE_DEATH and self.on_death:
                await self.on_death(str(name), str(body.get("message") or ""))

        elif event == "PlayerBan":
            msg = body.get("message") or ""
            if msg and ENABLE_BAN_ALERT and self.on_ban:
                await self.on_ban(str(msg))

        elif event == "TP_JOIN":
            name = strip_colors(body.get("player", {}).get("name", "")).strip() if isinstance(body.get("player"), dict) else ""
            if name and self.on_tp_join:
                await self.on_tp_join(name)

        elif event == "TP_DETECT":
            msg = body.get("message") or ""
            if msg and ENABLE_TP_ALERT and self.on_tp:
                await self.on_tp(str(msg))

        elif event == "BridgeStatus":
            log.info(f"ℹ️ 橋接狀態: {body.get('message')}")

    async def _on_player_message(self, body: dict):
        sender = strip_colors(body.get("sender") or body.get("player") or "").strip()
        message = extract_text(body.get("message") or "")
        if not ENABLE_CHAT or not sender or not message or is_ui_spam(message):
            return
        # 單向模式下自己的訊息也照樣轉發（方便確認聊天有通）
        if not ONE_WAY_MODE and sender.lower() == MC_PLAYER_NAME.lower():
            return
        if message.startswith("[Discord]"):
            return
        log.info(f"💬 {sender}: {message}")
        if self.on_chat:
            await self.on_chat(sender, message)

    async def _on_player_join(self, body: dict):
        name = str((body.get("player") or {}).get("name") or body.get("player") or "").strip()
        if not name:
            return
        self.players.add(name)
        log.info(f"➕ {name} 加入")
        if ENABLE_JOIN_LEAVE and self.on_join:
            await self.on_join(name)

    async def _on_player_leave(self, body: dict):
        name = str((body.get("player") or {}).get("name") or body.get("player") or "").strip()
        if not name:
            return
        self.players.discard(name)
        log.info(f"➖ {name} 離開")
        if ENABLE_JOIN_LEAVE and self.on_leave:
            await self.on_leave(name)

    # ------------------------------------------------ 送指令
    def _new_payload(self, cmd: str, purpose: str = "commandRequest") -> dict:
        return {
            "header": {"version": 1, "requestId": str(uuid.uuid4()),
                       "messageType": "commandRequest", "messagePurpose": purpose},
            "body": {"commandLine": cmd, "version": 1, "origin": {"type": "player"}},
        }

    async def _broadcast(self, payload: dict):
        for ws in list(self._clients):
            try:
                await ws.send(json.dumps(payload))
            except Exception:
                self._clients.discard(ws)

    async def send_command(self, cmd: str, timeout: float = 5.0) -> Optional[str]:
        """把指令送給所有連線，等第一個 commandResponse。"""
        if not self._clients:
            return None

        payload = self._new_payload(cmd)
        req_id = payload["header"]["requestId"]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[req_id] = PendingCommand(future=fut, timestamp=loop.time())

        await self._broadcast(payload)

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return "⚠️ 指令逾時"
        except Exception:
            self._pending.pop(req_id, None)
            return None

    async def inject_command(self, cmd: str):
        """請橋接器用 DLL 模擬鍵盤把指令打進遊戲（遊戲 WebSocket 沒連線時的備援）。"""
        payload = {
            "header": {"version": 1, "requestId": str(uuid.uuid4()),
                       "messageType": "event", "messagePurpose": "event", "eventName": "INJECT"},
            "body": {"commandLine": cmd},
        }
        await self._broadcast(payload)

    async def send_tellraw(self, text: str):
        cmd = 'tellraw @a {"rawtext":[{"text":%s}]}' % json.dumps(text)
        return await self.send_command(cmd, timeout=5.0)
