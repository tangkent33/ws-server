#!/usr/bin/env python3
r"""Discord ⇄ Minecraft 橋接程式（單檔版）。

原本拆在 bridge/ 套件裡的模組（config / jsonstore / textutil / mcws /
banapi / miaaoo / discordbot / legacy_commands / app）已全部合併到這支檔案，
執行只需要 bot.py + .env。

啟動：
    python bot.py          （或雙擊 run_bot.bat / start.bat）

資料流：
    Minecraft.Windows.exe
        └─ 827dll_<id>.dll      掃描記憶體：聊天 / 加入 / 離開 / 死亡 / TP / 封禁
              └─ \\.\pipe\mc_chat_<id>
                    └─ MC-WS.exe      轉成 WebSocket 事件
                          └─ ws://127.0.0.1:8000
                                └─ 本程式 → Discord

目錄：
    1  設定與日誌
    2  文字工具
    3  JSON 檔案存放（白名單 / 封禁 / 頻道設定）
    4  WebSocket 伺服器
    5  HTTP 封禁 API
    6  Miaaoo 聯機大廳 API
    7  Discord 客戶端（事件轉發 + 指令）
    8  組裝與進入點
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import aiohttp
import discord
import websockets
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv
from websockets import serve
from websockets.legacy.server import WebSocketServerProtocol

__version__ = "9.1.0"

# ============================================================ 1. 設定與日誌
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ Discord
DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
WEBHOOK_URL = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()

# ------------------------------------------------------------------ Minecraft
MC_HOST = os.getenv("MC_SERVER_IP", "127.0.0.1")
MC_PORT = _env_int("MC_WS_PORT", 8000)
MC_PLAYER_NAME = os.getenv("MC_PLAYER_NAME", "tangkent3")

# ------------------------------------------------------------------ 檔案
WHITELIST_FILE = os.path.expanduser(os.getenv("WHITELIST_FILE", "~/whitelist.json"))
BLACKLIST_FILE = str(BASE_DIR / os.getenv("BLACKLIST_FILE", "blacklist.json"))
CONFIG_FILE = os.path.expanduser(os.getenv("BRIDGE_CONFIG_FILE", "~/bridge_config.json"))
MIAOO_API_BASE = os.getenv("MIAOO_API_BASE", "https://api.miaaoo.com")
OWNER_ID = os.getenv("OWNER_ID", "tangkent3")

# ------------------------------------------------------------------ 封禁
TP_BAN_MINUTES = _env_int("TP_BAN_MINUTES", 0)              # 0 = 永久
TP_AUTO_BLACKLIST = _env_bool("TP_AUTO_BLACKLIST")
ENFORCE_BAN_ON_JOIN = _env_bool("ENFORCE_BAN_ON_JOIN", "true")

# 偵測到 TP 平台拉人時：直接踢出（預設開）
TP_KICK = _env_bool("TP_KICK", "true")
TP_KICK_REASON = os.getenv("TP_KICK_REASON", "偵測到 TP 平台加入")
TP_KICK_COOLDOWN = _env_int("TP_KICK_COOLDOWN", 20)         # 同一人幾秒內不重複踢

BAN_API_PORT = _env_int("BAN_API_PORT", 3001)
BAN_API_KEY = os.getenv("BAN_API_KEY", "my-secret-123")

# ------------------------------------------------------------------ 事件開關
ENABLE_JOIN_LEAVE = _env_bool("ENABLE_JOIN_LEAVE", "true")
ENABLE_DEATH = _env_bool("ENABLE_DEATH", "true")
ENABLE_TP_ALERT = _env_bool("ENABLE_TP_ALERT", "true")
ENABLE_BAN_ALERT = _env_bool("ENABLE_BAN_ALERT", "true")
ENABLE_CHAT = _env_bool("ENABLE_CHAT", "true")
ENABLE_COMMAND = _env_bool("ENABLE_COMMAND", "true")

# 單向模式：只把遊戲事件送到 Discord，不嘗試送聊天回遊戲
ONE_WAY_MODE = _env_bool("ONE_WAY_MODE", "true")

# ------------------------------------------------------------------ 格式
COMMAND_BLACKLIST = {
    x.strip().lower()
    for x in os.getenv("COMMAND_BLACKLIST", "stop,op,deop,ban,pardon,kick,gamemode,give,clear,kill").split(",")
    if x.strip()
}

CHAT_FORMAT_MC_TO_DC = os.getenv("CHAT_FORMAT_MC_TO_DC", "{message}")
CHAT_FORMAT_DC_TO_MC = os.getenv("CHAT_FORMAT_DC_TO_MC", "§c[§6Discord] §b{username}§e: {message}")


def _parse_ids(env_str: str) -> List[int]:
    """把 "123, 456" 轉成 [123, 456]，忽略 0 與非數字。"""
    out: List[int] = []
    for chunk in (env_str or "").split(","):
        chunk = chunk.strip()
        if chunk.isdigit() and int(chunk) != 0:
            out.append(int(chunk))
    return out


INIT_ALLOWED_GUILDS = _parse_ids(os.getenv("ALLOWED_GUILD_ID", "0"))
INIT_ALLOWED_CHANNELS = _parse_ids(os.getenv("ALLOWED_CHANNEL_ID", "0"))
DEFAULT_AUTHORIZED = [
    x.strip() for x in os.getenv("DEFAULT_AUTHORIZED_USERS", "tangkent3").split(",") if x.strip()
]

# ------------------------------------------------------------------ 執行期常數
DEDUP_WINDOW = 5.0            # 秒：兩個來源可能送同一件事
MAX_MESSAGE = 1900            # Discord 單則訊息上限 2000，留點邊界
LOCK_PORT = 27999             # 單一實例鎖
COMMAND_PREFIX = "!"


def setup_logging() -> logging.Logger:
    """日誌：主控台 + 會輪替的 bot.log（避免無限長大）。"""
    fmt = logging.Formatter("[%(levelname)s] [%(asctime)s] %(message)s", datefmt="%H:%M:%S")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            BASE_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass

    for noisy in ("websockets", "discord.gateway", "discord.client", "asyncio", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("bridge")


log = setup_logging()


def _pretty(template: str, **kwargs) -> str:
    """格式字串容錯：.env 打錯 {變數} 也不會讓事件處理爆掉。"""
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def _clip(text: str, limit: int = MAX_MESSAGE) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ============================================================ 2. 文字工具
COLOR_CODE = re.compile(r"§.")

# 伺服器 UI 廣播（tellraw 狀態面板之類），不要往 Discord 送
UI_SPAM_KEYWORDS = ("系統狀態", "掃地倒數", "你的錢包", "VIP等級", "當前人數", "你的名字")


def strip_colors(text: str) -> str:
    """移除 §a §l 這類色碼。"""
    return COLOR_CODE.sub("", str(text))


def extract_text(raw: str) -> str:
    """把 Minecraft 的 § 色碼與 rawtext JSON 拆成純文字。"""
    text = strip_colors(raw).strip()

    if text.startswith("{") and "rawtext" in text:
        try:
            data = json.loads(text)
        except Exception:
            return ""
        parts = data.get("rawtext")
        if isinstance(parts, list):
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if isinstance(parts, str):
            return parts
    return text


def is_ui_spam(text: str) -> bool:
    return any(k in text for k in UI_SPAM_KEYWORDS)


def normalize_cmd(raw: str) -> str:
    """`/list` → `list`（玩家常在前面多打一個斜線）。"""
    raw = (raw or "").strip()
    return raw[1:].strip() if raw.startswith("/") else raw


def first_token(cmd: str) -> str:
    parts = cmd.split()
    return parts[0].lower() if parts else ""


def extract_players(raw: str) -> List[str]:
    """從 `list` 指令的回覆抓玩家名。"""
    if not raw:
        return []
    match = re.search(r"(?:玩家在線|Players online)\s*[:：]?\s*\n?\s*(.+)", raw, re.IGNORECASE)
    tail = match.group(1).strip() if match else raw.strip()
    if not tail or tail in ("0", "無"):
        return []
    return [x.strip() for x in tail.split(",") if x.strip()]


# ============================================================ 3. JSON 檔案存放
def _atomic_write_json(path: str, data: Any, indent: int = 4):
    """先寫暫存檔再 rename：避免斷電 / 強制關閉把 json 寫成半截。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=indent, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return default


class WhitelistStore:
    """授權使用者（Discord ID 或名稱）。"""

    def __init__(self, path: str, defaults: List[str]):
        self.path = path
        self.defaults = list(defaults)
        self._data: List[str] = []
        self.load()

    def load(self) -> List[str]:
        data = _read_json(self.path, None)
        if isinstance(data, list):
            self._data = [str(x) for x in data]
        else:
            self._data = list(self.defaults)
            self.save()
        return self._data

    def save(self):
        try:
            _atomic_write_json(self.path, self._data)
        except OSError as exc:
            log.error(f"寫入白名單失敗: {exc}")

    @property
    def users(self) -> List[str]:
        return self._data

    def add(self, name: str) -> bool:
        if name in self._data:
            return False
        self._data.append(name)
        self.save()
        return True

    def remove(self, name: str) -> bool:
        if name not in self._data:
            return False
        self._data.remove(name)
        self.save()
        return True


class BanStore:
    """封禁名單：{名字: {name, reason, until(ms), by}}。

    until 為 None 代表永久；查詢時順手清掉過期的。
    """

    def __init__(self, path: str):
        self.path = path
        self._data: Dict[str, dict] = {}
        self.load()

    # ------------------------------------------------ 讀寫
    def load(self):
        data = _read_json(self.path, None)
        if isinstance(data, list):          # 舊格式（純陣列）→ 轉成永久封禁
            self._data = {
                str(x).lower(): {"name": str(x), "reason": "黑名單", "until": None, "by": "legacy"}
                for x in data
            }
        elif isinstance(data, dict):
            self._data = {str(k).lower(): v for k, v in data.items()}
        else:
            self._data = {}

    def save(self):
        try:
            _atomic_write_json(self.path, self._data)
        except OSError as exc:
            log.error(f"寫入封禁名單失敗: {exc}")

    # ------------------------------------------------ 查詢
    @staticmethod
    def _key(name: str) -> str:
        return str(name).strip().lower()

    def get(self, name: str) -> Optional[dict]:
        key = self._key(name)
        rec = self._data.get(key)
        if not rec:
            return None
        until = rec.get("until")
        if until and time.time() * 1000 > until:
            del self._data[key]
            self.save()
            log.info(f"⏰ {name} 封禁到期，自動解封")
            return None
        return rec

    def contains(self, name: str) -> bool:
        return self.get(name) is not None

    def items(self) -> List[dict]:
        return [rec for rec in (self.get(k) for k in list(self._data)) if rec]

    # ------------------------------------------------ 異動
    def ban(self, name: str, reason: str = "", minutes: int = 0, by: str = "api") -> dict:
        rec = {
            "name": name,
            "reason": reason or "未填理由",
            "until": int(time.time() * 1000) + minutes * 60000 if minutes else None,
            "by": by,
        }
        self._data[self._key(name)] = rec
        self.save()
        return rec

    def unban(self, name: str) -> bool:
        key = self._key(name)
        if key in self._data:
            del self._data[key]
            self.save()
            return True
        return False


class BridgeConfig:
    """允許的 Discord 伺服器 / 頻道。

      - .env 的 ALLOWED_GUILD_ID / ALLOWED_CHANNEL_ID（可多個）
      - bridge_config.json 的單一值（!setguild / !setchannel 寫入）
    兩者是聯集；都沒設定就全開。
    """

    def __init__(self, path: str, default_guilds: List[int], default_channels: List[int]):
        self.path = path
        self.guild_ids = list(default_guilds)
        self.channel_ids = list(default_channels)
        self.guild_id = 0
        self.channel_id = 0
        self.load()

    def load(self):
        data = _read_json(self.path, None)
        if not isinstance(data, dict):
            return
        try:
            self.guild_id = int(data.get("guild") or 0)
            self.channel_id = int(data.get("channel") or 0)
        except (TypeError, ValueError):
            pass

    def save(self):
        try:
            _atomic_write_json(self.path, {"guild": self.guild_id, "channel": self.channel_id})
        except OSError as exc:
            log.error(f"儲存 bridge_config.json 失敗: {exc}")

    # ------------------------------------------------ 判斷
    def allowed_guilds(self) -> Set[int]:
        return set(self.guild_ids) | ({self.guild_id} if self.guild_id else set())

    def allowed_channels(self) -> Set[int]:
        return set(self.channel_ids) | ({self.channel_id} if self.channel_id else set())


# ============================================================ 4. WebSocket 伺服器
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

        # 事件回呼，由 main() 接上
        self.on_chat: Optional[Callable] = None
        self.on_join: Optional[Callable] = None
        self.on_leave: Optional[Callable] = None
        self.on_death: Optional[Callable] = None
        self.on_tp: Optional[Callable] = None
        self.on_tp_join: Optional[Callable] = None
        self.on_ban: Optional[Callable] = None
        self.on_kick_result: Optional[Callable] = None
        self.on_connected: Optional[Callable] = None
        self.on_disconnected: Optional[Callable] = None

    # ------------------------------------------------ 對外狀態
    @property
    def connected(self) -> bool:
        return bool(self._clients)

    def game_clients(self) -> List[WebSocketServerProtocol]:
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
        """serve 若啟動失敗（port 被占用等）會自動退避重試，不會整支程式靜默失聯。"""
        backoff = 1
        while True:
            try:
                async with serve(self._on_connect, self.host, self.port,
                                 ping_interval=20, ping_timeout=10, max_size=2 ** 20):
                    log.info(f"✅ WebSocket 伺服器已啟動 {self.host}:{self.port}")
                    backoff = 1
                    await asyncio.Future()
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                log.error(f"❌ WebSocket 伺服器無法啟動 {self.host}:{self.port}（port 被占用？）: {exc}")
            except Exception as exc:
                log.error(f"❌ WebSocket 伺服器異常: {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

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
        except Exception as exc:
            log.error(f"接收迴圈錯誤: {exc}")
        finally:
            self._clients.discard(ws)
            if ws is self._bridge_ws:
                self._bridge_ws = None
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
        if not isinstance(data, dict):
            return

        header = data.get("header") or {}
        event = header.get("eventName") or data.get("eventName") or ""
        purpose = header.get("messagePurpose", "")
        body = data.get("body") or {}
        if not isinstance(body, dict):
            body = {}

        if event == "BridgeHello":
            self._bridge_ws = ws
            log.debug("橋接器握手完成")
            return

        if event == "BridgeStatus":
            log.debug(f"橋接狀態: {body.get('message')}")
            return

        if purpose == "commandResponse":
            self._resolve_pending(header.get("requestId"), body)
            return

        if self._duplicated(event, body):
            return

        try:
            await self._dispatch(event, body)
        except Exception as exc:
            log.error(f"事件處理錯誤 ({event}): {exc}")

    def _resolve_pending(self, req_id, body):
        if not req_id:
            return
        pending = self._pending.pop(req_id, None)
        if pending and not pending.future.done():
            pending.future.set_result(str(body.get("statusMessage") or "指令已送出"))

    @staticmethod
    def _dedup_key(event: str, body: dict) -> str:
        """同一件事可能從兩個來源各送一次，用「事件+對象+內容」當指紋。

        注意：玩家加入 / 離開的 body 只有 player 物件、沒有 message，
        舊版把 player 物件丟掉後指紋全變成 "PlayerJoin|"，5 秒內的第二個人
        會被誤判成重複而消失。
        """
        player = body.get("player")
        if isinstance(player, dict):
            player = player.get("name") or ""
        payload = body.get("message") or body.get("sender") or body.get("commandLine") or ""
        return f"{event}|{player or ''}|{payload}"

    def _duplicated(self, event: str, body: dict) -> bool:
        key = self._dedup_key(event, body)
        now = asyncio.get_running_loop().time()

        if self._seen.get(key, 0) > now - DEDUP_WINDOW:
            return True
        self._seen[key] = now

        if len(self._seen) > 500:                       # 順手清理舊指紋
            cutoff = now - 60
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        return False

    async def _dispatch(self, event: str, body: dict):
        if event == "PlayerMessage":
            await self._on_player_message(body)

        elif event == "PlayerJoin":
            await self._on_player_join(body)

        elif event == "PlayerLeave":
            await self._on_player_leave(body)

        elif event == "PlayerDied":
            if not ENABLE_DEATH:
                return
            name = self._player_name(body)
            if name and self.on_death:
                await self.on_death(name, str(body.get("message") or ""))

        elif event == "PlayerBan":
            msg = str(body.get("message") or "")
            if msg and ENABLE_BAN_ALERT and self.on_ban:
                await self.on_ban(msg)

        elif event == "TP_JOIN":
            name = strip_colors(self._player_name(body)).strip()
            if name and self.on_tp_join:
                await self.on_tp_join(name)

        elif event == "TP_DETECT":
            msg = str(body.get("message") or "")
            if msg and ENABLE_TP_ALERT and self.on_tp:
                await self.on_tp(msg)

        # 鍵盤注入的執行結果（DLL 回報：ok|injected 或 fail|game not focused）
        elif event == "KickResult":
            text = str(body.get("message") or "")
            if text and self.on_kick_result:
                await self.on_kick_result(text)

    @staticmethod
    def _player_name(body: dict) -> str:
        player = body.get("player")
        if isinstance(player, dict):
            return str(player.get("name") or "")
        return str(player or "")

    async def _on_player_message(self, body: dict):
        sender = strip_colors(body.get("sender") or self._player_name(body)).strip()
        message = extract_text(body.get("message") or "")
        if not sender or not message or is_ui_spam(message):
            return
        if not ENABLE_CHAT:
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
        name = self._player_name(body).strip()
        if not name:
            return
        self.players.add(name)
        log.info(f"➕ {name} 加入")
        if ENABLE_JOIN_LEAVE and self.on_join:
            await self.on_join(name)

    async def _on_player_leave(self, body: dict):
        name = self._player_name(body).strip()
        if not name:
            return
        self.players.discard(name)
        log.info(f"➖ {name} 離開")
        if ENABLE_JOIN_LEAVE and self.on_leave:
            await self.on_leave(name)

    # ------------------------------------------------ 送指令
    @staticmethod
    def _new_payload(cmd: str, purpose: str = "commandRequest") -> dict:
        return {
            "header": {"version": 1, "requestId": str(uuid.uuid4()),
                       "messageType": "commandRequest", "messagePurpose": purpose},
            "body": {"commandLine": cmd, "version": 1, "origin": {"type": "player"}},
        }

    async def _broadcast(self, payload: dict) -> int:
        if not self._clients:
            return 0
        data = json.dumps(payload)
        sent = 0
        for ws in list(self._clients):
            try:
                await ws.send(data)
                sent += 1
            except Exception:
                self._clients.discard(ws)
        return sent

    def _prune_pending(self, now: float):
        """指令回應可能永遠不來，太久沒回應的條目要清掉，不然會慢慢累積。"""
        if len(self._pending) < 32:
            return
        for req_id, pending in list(self._pending.items()):
            if now - pending.timestamp > 60:
                self._pending.pop(req_id, None)

    async def send_command(self, cmd: str, timeout: float = 5.0) -> Optional[str]:
        """把指令送給所有連線，等第一個 commandResponse。"""
        if not self._clients:
            return None

        payload = self._new_payload(cmd)
        req_id = payload["header"]["requestId"]
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self._prune_pending(loop.time())
        self._pending[req_id] = PendingCommand(future=future, timestamp=loop.time())

        if not await self._broadcast(payload):
            self._pending.pop(req_id, None)
            return None

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return "⚠️ 指令逾時"
        except Exception:
            return None
        finally:
            self._pending.pop(req_id, None)

    async def inject_command(self, cmd: str):
        """請橋接器用 DLL 模擬鍵盤把指令打進遊戲（遊戲 WebSocket 沒連線時的備援）。"""
        await self._broadcast({
            "header": {"version": 1, "requestId": str(uuid.uuid4()),
                       "messageType": "event", "messagePurpose": "event", "eventName": "INJECT"},
            "body": {"commandLine": cmd},
        })

    async def send_tellraw(self, text: str):
        cmd = 'tellraw @a {"rawtext":[{"text":%s}]}' % json.dumps(text, ensure_ascii=False)
        return await self.send_command(cmd, timeout=5.0)


# ============================================================ 5. HTTP 封禁 API
_bg_tasks: Set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """背景任務要留引用，否則可能被 GC 回收、例外也看不到。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _api_authorized(request: web.Request) -> bool:
    # header 或瀏覽器友善的 ?key=
    return request.headers.get("x-api-key") == BAN_API_KEY or request.query.get("key") == BAN_API_KEY


def _unauthorized() -> web.Response:
    return web.json_response({"error": "無效 API key"}, status=401)


async def start_ban_api(bot: "DCListener", store: BanStore) -> web.AppRunner:
    """啟動 Ban API（只綁 127.0.0.1）；回傳 runner，呼叫端負責 cleanup。"""

    async def is_banned(request: web.Request):
        name = request.match_info["name"]
        rec = store.get(name)
        return web.json_response({"userId": name, "banned": bool(rec), **(rec or {})})

    async def list_banned(request: web.Request):
        if not _api_authorized(request):
            return _unauthorized()
        items = store.items()
        return web.json_response({"count": len(items), "list": items})

    async def ban(request: web.Request):
        if not _api_authorized(request):
            return _unauthorized()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body 必須是 JSON"}, status=400)

        name = (data or {}).get("userId")
        if not name:
            return web.json_response({"error": "要填 userId"}, status=400)

        try:
            minutes = int((data or {}).get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0

        rec = store.ban(name, (data or {}).get("reason", ""), minutes, "api")
        _spawn(bot.kick_player(name, rec["reason"]))
        log.info(f"🔨 API 封禁 {name}: {rec['reason']}")
        return web.json_response({"ok": True, "userId": name, **rec})

    async def unban(request: web.Request):
        if not _api_authorized(request):
            return _unauthorized()
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body 必須是 JSON"}, status=400)

        name = (data or {}).get("userId")
        if not name:
            return web.json_response({"error": "要填 userId"}, status=400)

        ok = store.unban(name)
        return web.json_response({"ok": ok, "userId": name}, status=200 if ok else 404)

    app = web.Application()
    app.add_routes([
        web.get("/is-banned/{name}", is_banned),
        web.get("/banned", list_banned),
        web.post("/ban", ban),
        web.post("/unban", unban),
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", BAN_API_PORT)
    await site.start()
    log.info(f"✅ Ban API 已啟動 http://127.0.0.1:{BAN_API_PORT} (x-api-key: {BAN_API_KEY})")
    return runner


# ============================================================ 6. Miaaoo 聯機大廳 API
class MiaaooAPI:
    def __init__(self):
        self.base_url = MIAOO_API_BASE
        self.xuid: Optional[str] = None
        self.addid: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self._session:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": f"mc-bridge/{__version__}"},
            )

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------ 基本請求
    async def _get(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        if not self._session:
            await self.start()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        async with self._session.get(url, params=params) as resp:
            return await resp.json(content_type=None)

    async def _post(self, endpoint: str, data: Dict) -> Any:
        if not self._session:
            await self.start()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        async with self._session.post(url, json=data) as resp:
            return await resp.json(content_type=None)

    # ------------------------------------------------------------ 端點
    async def get_account_info(self) -> Dict[str, Any]:
        return await self._get("v2/account")

    async def get_profile(self, gt: Optional[str] = None, xuid: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, str] = {}
        if gt:
            params["gt"] = gt
        if xuid:
            params["xuid"] = xuid
        return await self._get("profile", params)

    async def get_room_info(self, session: Optional[str] = None, roomfrom: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, str] = {}
        if session:
            params["session"] = session
        if roomfrom:
            params["roomfrom"] = roomfrom
        return await self._get("roominfo", params)

    async def join_room_v2(self, roomid: str, sessionname: str, roomfrom: str,
                           addid: Optional[str] = None, xuid: Optional[str] = None) -> Dict[str, Any]:
        payload = {
            "version": "1.0.0",
            "joininformation": {
                "addid": addid or self.addid,
                "roomfrom": roomfrom,
                "roomid": roomid,
                "sessionname": sessionname,
            },
            "invitecontrol": {"userxuid": xuid or self.xuid},
        }
        return await self._post("v2/join", payload)

    async def refresh_followers(self) -> Dict[str, Any]:
        return await self._post("refreshFollowers", {})

    # ------------------------------------------------------------ 組合查詢
    async def list_rooms(self) -> List[Dict]:
        try:
            result = await self.get_room_info()
            if isinstance(result, dict):
                rooms = result.get("results") or result.get("data") or []
                if isinstance(rooms, list):
                    return rooms
                if "room" in result:
                    return [result]
                return []
            if isinstance(result, list):
                return result
            return []
        except Exception as exc:
            log.error(f"獲取房間列表失敗: {exc}")
            return []

    async def get_my_xuid_and_addid(self):
        xuid, addid = "", ""
        account: Any = None
        try:
            account = await self.get_account_info()
            if isinstance(account, dict):
                addid = account.get("addid") or account.get("addId") or account.get("id") or ""
                if not addid and isinstance(account.get("data"), dict):
                    addid = account["data"].get("addid") or account["data"].get("id") or ""
            elif isinstance(account, list) and account and isinstance(account[0], dict):
                addid = account[0].get("addid") or account[0].get("addId") or account[0].get("id") or ""
        except Exception as exc:
            log.error(f"獲取 AddID 失敗: {exc}")

        try:
            gt = MC_PLAYER_NAME
            if isinstance(account, dict):
                gt = account.get("gamertag") or account.get("gt") or MC_PLAYER_NAME
                if not gt and isinstance(account.get("data"), dict):
                    gt = account["data"].get("gamertag") or MC_PLAYER_NAME
            profile = await self.get_profile(gt=gt)
            if isinstance(profile, dict):
                xuid = profile.get("xuid") or profile.get("XUID") or ""
                if not xuid and isinstance(profile.get("data"), dict):
                    xuid = profile["data"].get("xuid") or ""
        except Exception as exc:
            log.error(f"獲取 XUID 失敗: {exc}")

        return xuid, addid


# ============================================================ 7. Discord 客戶端
def is_admin(message: discord.Message) -> bool:
    perms = getattr(message.author, "guild_permissions", None)
    return bool(perms and getattr(perms, "administrator", False))


class DCListener(discord.Client):
    """Discord 端：把遊戲事件送到頻道，並提供管理指令。"""

    def __init__(self, mc: MCWebSocketServer, whitelist: WhitelistStore,
                 blacklist: BanStore, webhook_url: str, config: BridgeConfig,
                 miaaoo: MiaaooAPI):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self.mc = mc
        self.whitelist = whitelist
        self.blacklist = blacklist
        self.webhook_url = webhook_url
        self.config = config
        self.miaaoo = miaaoo
        self._session: Optional[aiohttp.ClientSession] = None
        self.tree = app_commands.CommandTree(self)

        self._synced_guilds: Set[int] = set()      # 已經同步過斜線指令的伺服器
        self._empty_warned: Dict[int, float] = {}  # 空白訊息告警節流
        self._tp_kicked: Dict[str, float] = {}     # TP 玩家最近一次被踢的時間
        self._handlers: Dict[str, Callable] = {
            "!sync": self._cmd_sync,
            "!status": self._cmd_status,
            "!players": self._cmd_players_list,
            "!ban": self._cmd_ban,
            "!unban": self._cmd_unban,
            "!banned": self._cmd_banned,
            "!check": self._cmd_check,
            "!cmd": self._cmd_mccommand,
            "!invite": self._cmd_invite,
            "!settings": self._cmd_settings,
            "!setguild": self._cmd_setguild,
            "!setchannel": self._cmd_setchannel,
            "!reload": self._cmd_reload,
            "!wl": self._cmd_wl,
            "!help": self._cmd_help,
        }

    # ------------------------------------------------ 生命週期
    async def setup_hook(self):
        # 先把「全域指令」清空：舊版本殘留的全域 /ping 等會造成
        # CommandSignatureMismatch，而且全域指令更新要等 Discord 快取，
        # 所以一律改用 guild 指令。
        self.tree.clear_commands(guild=None)
        try:
            await self.tree.sync()
            log.info("🧹 已清空全域斜線指令，改用 guild 指令")
        except Exception as exc:
            log.error(f"清空全域指令失敗: {exc}")

        for cmd in (self.slash_status, self.slash_ping, self.slash_lianji, self.slash_join):
            self.tree.add_command(cmd)

    async def on_disconnect(self):
        log.warning("⚠️ Discord 連線中斷，準備重連")

    async def on_resumed(self):
        log.info("✅ Discord 連線已恢復")

    async def on_ready(self):
        log.info(f"✅ Discord 已登入：{self.user}")
        if not self._session:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": f"DiscordBot (mc-bridge {__version__})"},
            )
        # 只同步還沒同步過的伺服器：重連時 on_ready 會再觸發一次，
        # 每次都全量 sync 很容易撞上 Discord 的 rate limit。
        pending = [g for g in self.guilds if g.id not in self._synced_guilds]
        if pending:
            await self._sync_guild_commands(pending)

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None
        await super().close()

    async def _sync_guild_commands(self, guilds=None):
        for guild in (guilds if guilds is not None else self.guilds):
            try:
                obj = discord.Object(id=guild.id)
                self.tree.copy_global_to(guild=obj)
                await self.tree.sync(guild=obj)
                self._synced_guilds.add(guild.id)
                log.info(f"🧹 已同步 {guild.name} 的斜線指令")
            except Exception as exc:
                log.warning(f"同步 {guild.name} 的斜線指令失敗: {exc}")

    # ------------------------------------------------ Discord → 遊戲
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.webhook_id is not None:
            return

        content = message.content.strip()
        if not content:
            self._warn_empty_content(message)
            return

        # 指令不受頻道白名單限制（權限在各指令內部檢查），避免「打了沒反應」
        if content.startswith(COMMAND_PREFIX):
            await self._handle_command(message)
            return

        if not self._channel_allowed(message):
            return

        if not ONE_WAY_MODE and self.mc.connected:
            formatted = _pretty(CHAT_FORMAT_DC_TO_MC,
                                username=message.author.display_name,
                                message=message.clean_content)
            await self.mc.send_tellraw(formatted)

    def _warn_empty_content(self, message: discord.Message):
        """空白訊息通常是少了 Message Content Intent，但也可能只是純貼圖/附件。

        以前每則都印 WARNING，log 一下子就被洗掉；現在同一人 10 分鐘只提醒一次。
        """
        now = time.monotonic()
        author_id = getattr(message.author, "id", 0)
        if now - self._empty_warned.get(author_id, 0) > 600:
            self._empty_warned[author_id] = now
            if len(self._empty_warned) > 200:
                self._empty_warned = {author_id: now}
            log.info(f"ℹ️ 收到空白內容訊息（可能是貼圖/附件，或缺 Message Content Intent）: "
                     f"{message.author} channel={message.channel.id}")
        else:
            log.debug(f"空白內容訊息: {message.author} channel={message.channel.id}")

    def _channel_allowed(self, message: discord.Message) -> bool:
        """.env 清單與 bridge_config.json 單一值取聯集；都沒設定就全開。"""
        guild_ids = self.config.allowed_guilds()
        if guild_ids and (message.guild is None or message.guild.id not in guild_ids):
            return False
        channel_ids = self.config.allowed_channels()
        if channel_ids and message.channel.id not in channel_ids:
            return False
        return True

    # ------------------------------------------------ 指令分派
    async def _handle_command(self, message: discord.Message):
        cmd = message.content.strip().split()[0].lower()
        handler = self._handlers.get(cmd)
        if handler is None:
            await message.reply(f"❓ 未知指令 `{cmd}`，打 `!help` 看全部指令")
            return
        try:
            await handler(message)
        except Exception as exc:
            log.error(f"指令 {cmd} 執行失敗: {exc}")
            try:
                await message.reply(f"❌ 執行失敗：{exc}")
            except Exception:
                pass

    # ------------------------------------------------ 權限
    def _check_perm(self, message: discord.Message) -> bool:
        return is_admin(message) or self._authorized(message.author)

    def _authorized(self, author) -> bool:
        users = self.whitelist.users
        return str(getattr(author, "id", "")) in users or getattr(author, "name", "") in users

    def _require_admin(self, message: discord.Message) -> bool:
        return bool(getattr(getattr(message.author, "guild_permissions", None), "administrator", False))

    # ------------------------------------------------ 遊戲 → Discord
    async def _send_to_discord(self, username: str, content: str):
        text = _clip(content)
        for cid in dict.fromkeys(self.config.channel_ids):
            channel = self.get_channel(cid)
            if not channel:
                continue
            try:
                await channel.send(text)
            except Exception as exc:
                log.error(f"發送到頻道 {cid} 失敗: {exc}")

        if self.webhook_url:
            if not self._session:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": f"DiscordBot (mc-bridge {__version__})"},
                )
            try:
                payload = {
                    "username": _clip(username, 80),
                    "content": text,
                    "avatar_url": f"https://mc-heads.net/avatar/{username}/64",
                    "allowed_mentions": {"parse": []},
                }
                async with self._session.post(self.webhook_url, json=payload) as resp:
                    await resp.text()
            except Exception as exc:
                log.debug(f"Webhook 發送失敗: {exc}")

    async def on_mc_chat(self, sender: str, message: str):
        await self._send_to_discord(sender, _pretty(CHAT_FORMAT_MC_TO_DC, sender=sender, message=message))

    async def on_mc_join(self, player: str):
        if ENABLE_JOIN_LEAVE:
            await self._send_to_discord("伺服器", f"✅ **{player}** 加入了遊戲")
        await self.on_player_joined(player)

    async def on_mc_leave(self, player: str):
        if ENABLE_JOIN_LEAVE:
            await self._send_to_discord("伺服器", f"❌ **{player}** 離開了遊戲")

    async def on_mc_death(self, player: str, message: str):
        text = message.strip() or f"**{player}** 死亡了"
        await self._send_to_discord("伺服器", f"💀 {text}")

    async def on_mc_ban(self, text: str):
        if ENABLE_BAN_ALERT:
            await self._send_to_discord("防禦系統", f"🔨 **封禁事件**\n`{_clip(text, 500)}`")

    async def on_mc_tp(self, text: str):
        match = re.search(r"(?:名稱|名称|xuid|玩家)\s*[:：]?\s*([A-Za-z0-9_ ]{2,24})", text)
        lines = ["🛑 **偵測到 TP 平台特徵**"]
        if match:
            lines.append(f"目標：**{match.group(1).strip()}**")
        lines.append(f"內容：`{_clip(text, 500)}`")
        if ONE_WAY_MODE:
            lines.append("（單向模式：未執行 kick）")
        await self._send_to_discord("防禦系統", "\n".join(lines))

    async def on_mc_tp_join(self, player: str):
        """偵測到 TP 平台拉人：報告 + 直接踢出（TP_KICK 可關）。"""
        now = time.monotonic()
        last = self._tp_kicked.get(player.lower(), 0)
        if now - last < TP_KICK_COOLDOWN:
            log.debug(f"TP 玩家 {player} 在冷卻期內，略過重複處理")
            return

        log.warning(f"🚨 TP 平台加入者: {player}")
        rec = self.blacklist.get(player)

        if TP_KICK and TP_AUTO_BLACKLIST and not rec:
            rec = self.blacklist.ban(player, "TP 平台加入", TP_BAN_MINUTES, "auto")
            log.info(f"📝 已自動寫入封禁名單: {player}")

        status = f"已在封禁名單（{rec.get('reason')}）" if rec else "不在封禁名單"
        lines = [f"🛑 **偵測到 TP 平台拉人**", f"玩家: **{player}**", status]

        if TP_KICK:
            reason = TP_KICK_REASON or (rec or {}).get("reason") or "TP 平台加入"
            result = await self.kick_player(player, reason)
            self._tp_kicked[player.lower()] = now
            lines.append(f"👢 已送出踢出指令（{reason}）")
            lines.append(f"結果: `{result}`")
        else:
            lines.append("（TP_KICK=false，只提示不踢）")
            lines.append(f"要處理請用 `!ban {player}`")

        await self._send_to_discord("防禦系統", "\n".join(lines))

    async def on_player_joined(self, player: str):
        """有人加入時檢查封禁名單（每次重讀檔案，手改 json 不用重啟）。"""
        self.blacklist.load()
        rec = self.blacklist.get(player)
        if not rec:
            return
        if not ENFORCE_BAN_ON_JOIN:
            await self._send_to_discord("防禦系統", f"⚠️ **封禁名單玩家上線**\n玩家: **{player}**\n（未開啟自動踢）")
            return

        result = await self.kick_player(player, rec.get("reason", "黑名單"))
        await self._send_to_discord(
            "防禦系統",
            f"👢 **封禁攔截**\n玩家: **{player}**\n理由: {rec.get('reason')}\n指令結果: `{result}`",
        )

    async def on_bridge_connected(self):
        log.info("🟢 橋接程式已連線")

    async def on_bridge_disconnected(self):
        log.info("🔴 橋接程式已斷線")

    # ------------------------------------------------ 踢人
    async def kick_player(self, player: str, reason: str = "黑名單") -> str:
        if not ENABLE_COMMAND:
            log.warning("ENABLE_COMMAND=false，略過 kick")
            return "（指令傳送已停用）"
        cmd = f'kick "{player}" §c{reason}'
        if self.mc.game_clients():
            result = await self.mc.send_command(cmd, timeout=5)
        else:
            await self.mc.inject_command(cmd)
            result = "已改用鍵盤注入（遊戲 WebSocket 未連線）"
        log.info(f"👢 kick {player} → {result}")
        return result or "（無回應）"

    async def on_kick_result(self, text: str) -> None:
        """DLL 回報的執行結果：ok|injected 或 fail|game not focused。"""
        status, _, detail = text.partition("|")
        if status == "ok":
            log.info(f"✅ 踢出指令已在遊戲內執行（{detail}）")
            return
        log.warning(f"⚠️ 自動踢出失敗：{detail}")
        await self._send_to_discord(
            "防禦系統",
            f"⚠️ **自動踢出沒有成功**\n原因: `{detail}`\n"
            f"鍵盤注入需要遊戲視窗在前景，請切回遊戲或手動踢人。",
        )

    # ------------------------------------------------ 斜線指令
    @app_commands.command(name="status", description="顯示橋接狀態")
    async def slash_status(self, interaction: discord.Interaction):
        await interaction.response.send_message(self._status_text(), ephemeral=True)

    @app_commands.command(name="ping", description="測試機器人是否在線")
    async def slash_ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("🏓 Pong！機器人運作正常！", ephemeral=True)

    @app_commands.command(name="lianji", description="查詢 Miaaoo 聯機大廳的房間列表")
    async def slash_lianji(self, interaction: discord.Interaction):
        if not self._authorized(interaction.user):
            await interaction.response.send_message("❌ 你不在白名單中", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            rooms = await self.miaaoo.list_rooms()
            if not rooms:
                await interaction.followup.send("📋 目前沒有可用的房間")
                return

            lines = ["**📋 可加入的房間列表：**", ""]
            for i, room in enumerate(rooms[:20], 1):
                props = room.get("customProperties", {}) or {}
                joinable = "🟢 可加入" if props.get("BroadcastSetting", 0) == 3 else "🔒 不可加入"
                lines.append(
                    f"**{i}. {props.get('worldName', '未知地圖')}**\n"
                    f"  🏠 房主: {props.get('hostName', '未知房主')}\n"
                    f"  👥 {props.get('MemberCount', 0)}/{props.get('MaxMemberCount', 0)} 人\n"
                    f"  📌 模式: {props.get('worldType', '未知模式')}\n"
                    f"  📦 版本: {props.get('version', '未知版本')}\n"
                    f"  🆔 ID: `{room.get('id', 'N/A')}`\n"
                    f"  🔗 {joinable}\n"
                )
            lines.append(f"\n共 {len(rooms)} 個房間")
            lines.append("使用 `/join <ID>` 加入房間")
            await interaction.followup.send(_clip("\n".join(lines)))
        except Exception as exc:
            log.error(f"Lianji 錯誤: {exc}")
            await interaction.followup.send(f"❌ 查詢失敗：{exc}")

    @app_commands.command(name="join", description="加入指定的 Miaaoo 房間")
    async def slash_join(self, interaction: discord.Interaction, roomid: str):
        if not self._authorized(interaction.user):
            await interaction.response.send_message("❌ 你不在白名單中", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            if not self.miaaoo.xuid or not self.miaaoo.addid:
                await interaction.followup.send("🔑 正在獲取 XUID 和 AddID...")
                xuid, addid = await self.miaaoo.get_my_xuid_and_addid()
                if not xuid or not addid:
                    await interaction.followup.send("❌ 無法獲取 XUID 或 AddID")
                    return
                self.miaaoo.xuid, self.miaaoo.addid = xuid, addid

            rooms = await self.miaaoo.list_rooms()
            target = next((r for r in rooms if r.get("id") == roomid), None)
            if not target:
                await interaction.followup.send(f"❌ 找不到房間 ID: {roomid}")
                return

            sessionname = (target.get("sessionRef") or {}).get("name", "")
            roomfrom = target.get("roomfrom", "6")
            props = target.get("customProperties", {}) or {}
            world_name = props.get("worldName", "未知地圖")
            host_name = props.get("hostName", "未知房主")

            await interaction.followup.send(f"🎮 正在加入房間 **{world_name}**...")
            result = await self.miaaoo.join_room_v2(roomid, sessionname, roomfrom)

            status_str = "" if result.get("status") is None else str(result.get("status"))
            code_str = "" if result.get("code") is None else str(result.get("code"))
            msg = result.get("message_zh_CN", result.get("message", ""))
            invite_url = f"https://lian.gt.tc/zh/share.html?name={roomid}"

            if status_str == "200":
                if code_str == "2":
                    embed = discord.Embed(title="✅ 加入成功！",
                                          description=f"已成功加入房間 **{world_name}**",
                                          color=discord.Color.green())
                    embed.add_field(name="🏠 房主", value=host_name, inline=True)
                    embed.add_field(name="🆔 房間 ID", value=roomid, inline=True)
                    embed.add_field(name="🔗 邀請連結", value=f"[點擊加入]({invite_url})", inline=False)
                    embed.add_field(name="📋 邀請網址", value=f"`{invite_url}`", inline=False)
                    await interaction.followup.send(embed=embed)
                elif code_str == "1":
                    await interaction.followup.send(f"⚠️ **部分成功**\n{msg}\n\n🔗 邀請連結：{invite_url}")
                else:
                    await interaction.followup.send(f"✅ {msg}\n\n🔗 邀請連結：{invite_url}")
            elif status_str == "400":
                await interaction.followup.send(f"❌ 參數錯誤：{msg}")
            elif status_str == "404":
                await interaction.followup.send(f"❌ 房間不存在：{msg}")
            else:
                await interaction.followup.send(f"❌ 伺服器錯誤：{msg}")
        except Exception as exc:
            log.error(f"Join 錯誤: {exc}")
            await interaction.followup.send(f"❌ 加入失敗：{exc}")

    # ------------------------------------------------ 文字指令
    def _status_text(self) -> str:
        mode = "單向（遊戲 → Discord）" if ONE_WAY_MODE else "雙向"
        state = "🟢 已連線" if self.mc.connected else "🔴 未連線"
        return f"**橋接狀態**：{state}\n**模式**：{mode}\n**玩家數**：{len(self.mc.players)}"

    async def _cmd_help(self, message: discord.Message):
        await message.reply(
            "**斜線指令**\n"
            "`/status` 橋接狀態　`/ping` 測試\n"
            "`/lianji` Miaaoo 房間列表　`/join <ID>` 加入房間\n\n"
            "**查詢**\n"
            "`!status` 橋接狀態　`!players` 線上玩家\n"
            "`!check` 伺服器狀態（時間/天氣/種子）　`!settings` 當前設定\n\n"
            "**封禁**\n"
            "`!ban 名字 [分鐘] [理由]`　`!unban 名字`　`!banned`\n\n"
            "**執行**\n"
            "`!cmd <指令>` 執行 Minecraft 指令（有黑名單）\n"
            "`!invite` 邀請房主\n\n"
            "**管理（管理員）**\n"
            "`!setguild` `!setchannel` `!reload` `!wl add/del/list` `!sync`"
        )

    async def _cmd_status(self, message: discord.Message):
        await message.reply(self._status_text())

    async def _cmd_sync(self, message: discord.Message):
        if not self._check_perm(message):
            await message.reply("❌ 你沒有權限")
            return
        status = await message.reply("🔄 同步中...")
        self._synced_guilds.clear()
        await self._sync_guild_commands()
        await status.edit(content="✅ 已同步斜線指令")

    async def _cmd_ban(self, message: discord.Message):
        if not self._check_perm(message):
            await message.reply("❌ 你沒有權限")
            return

        parts = message.content.strip().split(maxsplit=3)
        if len(parts) < 2:
            await message.reply("用法：`!ban 名字 [分鐘] [理由]`")
            return

        name = parts[1]
        rest = parts[2:]
        minutes, reason = 0, ""
        if rest and rest[0].isdigit():
            minutes = int(rest[0])
            reason = " ".join(rest[1:]).strip()
        elif rest:
            reason = " ".join(rest).strip()

        rec = self.blacklist.ban(name, reason, minutes, str(message.author))
        result = await self.kick_player(name, rec["reason"])
        await message.reply(f"🔨 已封禁 **{name}**（{rec['reason']}）\n{result}")

    async def _cmd_unban(self, message: discord.Message):
        if not self._check_perm(message):
            await message.reply("❌ 你沒有權限")
            return
        parts = message.content.strip().split()
        if len(parts) < 2:
            await message.reply("用法：`!unban 名字`")
            return
        ok = self.blacklist.unban(parts[1])
        await message.reply("✅ 已解封" if ok else "⚠️ 他本來就不在名單")

    async def _cmd_banned(self, message: discord.Message):
        items = self.blacklist.items()
        if not items:
            await message.reply("封禁名單是空的")
            return

        lines = []
        for item in items[:20]:
            until = item.get("until")
            if until:
                expiry = time.strftime("%m-%d %H:%M", time.localtime(until / 1000))
                lines.append(f"`{item['name']}` — {item.get('reason', '')}（到 {expiry}）")
            else:
                lines.append(f"`{item['name']}` — {item.get('reason', '')}（永久）")
        suffix = f"\n...共 {len(items)} 筆" if len(items) > 20 else ""
        await message.reply(_clip("**封禁名單**\n" + "\n".join(lines) + suffix))

    async def _cmd_players_list(self, message: discord.Message):
        if not self._authorized(message.author):
            await message.reply("❌ 你不在白名單中")
            return
        if not self.mc.connected:
            await message.reply("❌ Minecraft 未連線")
            return

        raw = await self.mc.send_command("list", timeout=10)
        players = extract_players(self._clean_result(raw))
        if players:
            await message.reply(f"**👥 線上玩家 ({len(players)})**\n```\n{', '.join(players)}\n```")
        else:
            await message.reply("📋 目前沒有玩家在線")

    async def _cmd_check(self, message: discord.Message):
        if not self._authorized(message.author):
            await message.reply("❌ 你不在白名單中")
            return
        if not self.mc.connected:
            await message.reply("❌ Minecraft 未連線")
            return

        status = await message.reply("🔍 檢查中...")
        # 四條指令互相獨立，一次併發送出；以前逐條等 = 最慢 40 秒，現在一個 timeout 就結束
        results = await asyncio.gather(
            self.mc.send_command("list", timeout=10),
            self.mc.send_command("time query daytime", timeout=10),
            self.mc.send_command("weather query", timeout=10),
            self.mc.send_command("seed", timeout=10),
            return_exceptions=True,
        )
        list_raw, time_raw, weather_raw, seed_raw = (self._clean_result(r) for r in results)

        players = extract_players(list_raw)
        lines = [
            f"🎮 連線: {'🟢' if self.mc.connected else '🔴'}",
            f"👑 房主: {OWNER_ID}",
            f"👥 線上: {len(players)} 人" + (f" ({', '.join(players)})" if players else " (無)"),
        ]
        if time_raw:
            lines.append(f"⏰ 時間: {time_raw}")
        if weather_raw:
            lines.append(f"🌤️ 天氣: {weather_raw}")
        if seed_raw:
            lines.append(f"🌍 種子: {seed_raw}")
        await status.edit(content=_clip("**📊 伺服器狀態**\n" + "\n".join(lines)))

    @staticmethod
    def _clean_result(value: Any) -> str:
        """把逾時 / 例外統一成空字串，避免把「⚠️ 指令逾時」當成真的回覆顯示。"""
        if value is None or isinstance(value, BaseException):
            return ""
        text = str(value).strip()
        return "" if text.startswith("⚠️") else text

    async def _cmd_mccommand(self, message: discord.Message):
        if not self._authorized(message.author):
            await message.reply("❌ 你不在白名單中")
            return

        raw = message.content[len("!cmd"):].strip()
        if not raw:
            await message.reply("❌ 用法: `!cmd <指令>`")
            return

        cmd = normalize_cmd(raw)
        token = first_token(cmd)
        if token in COMMAND_BLACKLIST:
            await message.reply(f"❌ 指令 `{token}` 被禁止")
            return
        if not self.mc.connected:
            await message.reply("❌ Minecraft 未連線")
            return

        status = await message.reply("⏳ 執行中...")
        result = await self.mc.send_command(cmd, timeout=15)
        if not result or result.startswith("⚠️"):
            await status.edit(content="⚠️ 執行失敗（遊戲未連線或指令無回應？）")
        else:
            await status.edit(content=f"📋 結果：\n```\n{_clip(result, 1800)}\n```")

    async def _cmd_invite(self, message: discord.Message):
        if not self._authorized(message.author):
            await message.reply("❌ 你不在白名單中")
            return
        if not self.mc.connected:
            await message.reply("❌ Minecraft 未連線")
            return

        status = await message.reply(f"🎮 邀請 {OWNER_ID}...")
        result = self._clean_result(await self.mc.send_command(f"invite {OWNER_ID}", timeout=12))
        if result:
            await status.edit(content=f"**邀請結果**\n```\n{_clip(result, 1800)}\n```")
        else:
            await status.edit(content="⚠️ 邀請失敗")

    async def _cmd_settings(self, message: discord.Message):
        guild = self.get_guild(self.config.guild_id) if self.config.guild_id else None
        channel = self.get_channel(self.config.channel_id) if self.config.channel_id else None
        lines = [
            f"📢 伺服器: {self._describe_guild(guild)}",
            f"📢 頻道: {self._describe_channel(channel)}",
            f"🎮 Minecraft: {'🟢 已連線' if self.mc.connected else '🔴 已斷線'}",
            f"👑 房主: {OWNER_ID}",
            f"🔑 XUID: {self.miaaoo.xuid or '未獲取（會自動獲取）'}",
            f"🔑 AddID: {self.miaaoo.addid or '未獲取（會自動獲取）'}",
        ]
        await message.reply("**⚙️ 當前設定**\n" + "\n".join(lines))

    def _describe_guild(self, guild) -> str:
        if guild:
            return f"{guild.name} (`{guild.id}`)"
        return f"ID {self.config.guild_id}（未找到）" if self.config.guild_id else "未設定"

    def _describe_channel(self, channel) -> str:
        if channel:
            return f"{channel.mention} (`{channel.id}`)"
        return f"ID {self.config.channel_id}（未找到）" if self.config.channel_id else "未設定"

    async def _cmd_setguild(self, message: discord.Message):
        if not self._require_admin(message):
            await message.reply("❌ 需要管理員權限")
            return
        if message.guild is None:
            await message.reply("❌ 此指令只能在伺服器中使用")
            return
        self.config.guild_id = message.guild.id
        self.config.save()
        await message.reply(f"✅ 已設定允許的伺服器：{message.guild.name} (`{message.guild.id}`)")

    async def _cmd_setchannel(self, message: discord.Message):
        if not self._require_admin(message):
            await message.reply("❌ 需要管理員權限")
            return
        self.config.channel_id = message.channel.id
        self.config.save()
        await message.reply(f"✅ 已設定允許的頻道：{message.channel.mention} (`{message.channel.id}`)")

    async def _cmd_reload(self, message: discord.Message):
        if not self._require_admin(message):
            await message.reply("❌ 需要管理員權限")
            return
        self.whitelist.load()
        self.blacklist.load()
        await message.reply("🔄 白名單與封禁名單已重新載入")

    async def _cmd_wl(self, message: discord.Message):
        if not self._require_admin(message):
            await message.reply("❌ 需要管理員權限")
            return

        parts = message.content.split()
        if len(parts) < 2:
            await message.reply("❌ 用法: `!wl add|del|list <名稱>`")
            return

        action = parts[1].lower()

        if action in ("add", "del"):
            if len(parts) < 3:
                await message.reply("❌ 請提供名稱（建議用 Discord ID）")
                return
            target = parts[2]
            if action == "add":
                ok = self.whitelist.add(target)
                await message.reply(f"✅ 已新增 `{target}`" if ok else f"⚠️ `{target}` 已在白名單中")
            else:
                ok = self.whitelist.remove(target)
                await message.reply(f"✅ 已移除 `{target}`" if ok else f"❌ `{target}` 不在白名單中")

        elif action == "list":
            users = self.whitelist.users
            if not users:
                await message.reply("📋 白名單為空")
                return
            await message.reply(_clip("📋 白名單：\n```\n" + ", ".join(users) + "\n```", 1800))
        else:
            await message.reply("❌ 未知子指令，可用：`add` / `del` / `list`")


# ============================================================ 8. 組裝與進入點
def _wire(mc: MCWebSocketServer, bot: DCListener):
    mc.on_chat = bot.on_mc_chat
    mc.on_join = bot.on_mc_join
    mc.on_leave = bot.on_mc_leave
    mc.on_death = bot.on_mc_death
    mc.on_tp = bot.on_mc_tp
    mc.on_tp_join = bot.on_mc_tp_join
    mc.on_ban = bot.on_mc_ban
    mc.on_kick_result = bot.on_kick_result
    mc.on_connected = bot.on_bridge_connected
    mc.on_disconnected = bot.on_bridge_disconnected


async def _run():
    if not DISCORD_TOKEN:
        log.error("缺少 DISCORD_TOKEN，請檢查 .env")
        return

    whitelist = WhitelistStore(WHITELIST_FILE, DEFAULT_AUTHORIZED)
    blacklist = BanStore(BLACKLIST_FILE)
    config = BridgeConfig(CONFIG_FILE, INIT_ALLOWED_GUILDS, INIT_ALLOWED_CHANNELS)

    miaaoo = MiaaooAPI()
    await miaaoo.start()

    mc = MCWebSocketServer(MC_HOST, MC_PORT)
    bot = DCListener(mc, whitelist, blacklist, WEBHOOK_URL, config, miaaoo)
    _wire(mc, bot)

    # 啟動時把「事件會送到哪」講清楚：沒頻道又沒 webhook 就等於靜默丟棄
    targets = config.channel_ids
    log.info(f"📤 事件轉發目標：{len(targets)} 個頻道 {targets if targets else '（無！）'}"
             f" + {'Webhook' if WEBHOOK_URL else '無 Webhook'}")
    log.info(f"🎮 MC WebSocket：ws://{MC_HOST}:{MC_PORT}　（模式：{'單向' if ONE_WAY_MODE else '雙向'}）")

    ws_task = asyncio.create_task(mc.start())
    api_runner = None
    try:
        api_runner = await start_ban_api(bot, blacklist)
    except OSError as exc:
        # 3001 被別的程式占用時，只是沒有 HTTP 封禁 API，不該拖垮整個 bot
        log.warning(f"⚠️ Ban API 啟動失敗（port {BAN_API_PORT} 被占用？）: {exc}")

    try:
        await bot.start(DISCORD_TOKEN)
    except discord.LoginFailure:
        log.error("❌ Discord 登入失敗：DISCORD_TOKEN 無效或已被重置，請到 Developer Portal 重新複製")
        await bot.close()
        return
    except discord.PrivilegedIntentsRequired:
        log.error("❌ 缺少特權 Intent：請到 Developer Portal → Bot → 開啟 MESSAGE CONTENT INTENT")
        await bot.close()
        return
    finally:
        log.info("🛑 正在關閉...")
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        await mc.stop()
        if api_runner is not None:
            await api_runner.cleanup()
        await miaaoo.close()


def _acquire_single_instance() -> Optional[socket.socket]:
    """用一個本機 port 當鎖，避免同時跑兩份（會互搶 Discord 連線）。"""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)
    except OSError:
        lock.close()
        print(f"另一個 bot.py 已在執行（port {LOCK_PORT} 被占用），本實例結束。")
        return None
    return lock


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    lock = _acquire_single_instance()
    if lock is None:
        return

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n🛑 已停止")
    except Exception:
        log.exception("💥 未預期的錯誤，程式結束")
        raise
    finally:
        lock.close()


if __name__ == "__main__":
    main()
