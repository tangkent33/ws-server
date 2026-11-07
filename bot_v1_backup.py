#!/usr/bin/env python3
"""
Discord-Minecraft Bridge v7.4 (终极防熊版)
更新: 接收 TP_DETECT 事件，自动解析出玩家名字并在游戏内执行 kick 指令
"""

import os
import json
import asyncio
import logging
import uuid
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Callable, Set, Any

import discord
from discord import app_commands
import websockets
from websockets import serve
from websockets.legacy.server import WebSocketServerProtocol
from dotenv import load_dotenv

load_dotenv(".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
MIAOO_API_BASE = "https://api.miaaoo.com"

def parse_ids(env_str: str) -> List[int]:
    if not env_str: return []
    try: return [int(x.strip()) for x in env_str.split(",") if x.strip().isdigit() and int(x.strip()) != 0]
    except ValueError: return []

INIT_ALLOWED_GUILDS = parse_ids(os.getenv("ALLOWED_GUILD_ID", "0"))
INIT_ALLOWED_CHANNELS = parse_ids(os.getenv("ALLOWED_CHANNEL_ID", "0"))

MC_HOST = os.getenv("MC_SERVER_IP", "127.0.0.1")
MC_PORT = int(os.getenv("MC_WS_PORT", "8000"))
MC_PLAYER_NAME = os.getenv("MC_PLAYER_NAME", "tangkent3")
OWNER_ID = os.getenv("OWNER_ID", "tangkent3")

DEFAULT_AUTHORIZED = [x.strip() for x in os.getenv("DEFAULT_AUTHORIZED_USERS", "tangkent3").split(",") if x.strip()]
WHITELIST_FILE = os.path.expanduser("~/whitelist.json")

COMMAND_BLACKLIST = set(x.strip().lower() for x in os.getenv("COMMAND_BLACKLIST", "stop,op,deop,ban,pardon,kick,gamemode,give,clear,kill").split(",") if x.strip())

CHAT_FORMAT_DC_TO_MC = os.getenv("CHAT_FORMAT_DC_TO_MC", "§c[§6tangkent3 §a| §eDiscord] §b{username}§e: {message}")
CHAT_FORMAT_MC_TO_DC = os.getenv("CHAT_FORMAT_MC_TO_DC", "{message}")

ENABLE_JOIN_LEAVE = os.getenv("ENABLE_JOIN_LEAVE", "true").lower() == "true"
ENABLE_DEATH = os.getenv("ENABLE_DEATH", "true").lower() == "true"
ENABLE_DISCORD_TO_MC = os.getenv("ENABLE_DISCORD_TO_MC", "true").lower() == "true"
ENABLE_EXTRA_EVENTS = os.getenv("ENABLE_EXTRA_EVENTS", "false").lower() == "true"

DEFAULT_SUBSCRIBE_EVENTS = (
    "PlayerMessage,PlayerJoin,PlayerLeave,PlayerDied,PlayerBanned,"
    "BlockBroken,BlockPlaced,ItemUsed,ItemAcquired,ItemCrafted,"
    "ItemSmelted,ItemEquipped,ItemDestroyed"
)
SUBSCRIBE_EVENTS = [x.strip() for x in os.getenv("SUBSCRIBE_EVENTS", DEFAULT_SUBSCRIBE_EVENTS).split(",") if x.strip()]

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] [%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bridge")
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.server").setLevel(logging.WARNING)

class MiaaooAPI:
    def __init__(self):
        self.base_url = MIAOO_API_BASE
        self.xuid: Optional[str] = None
        self.addid: Optional[str] = None
        self._session = None

    async def start(self):
        if not self._session:
            import aiohttp
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def _get(self, endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        if not self._session: await self.start()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        async with self._session.get(url, params=params, timeout=10) as resp:
            return await resp.json()

    async def _post(self, endpoint: str, data: Dict) -> Dict[str, Any]:
        if not self._session: await self.start()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        async with self._session.post(url, json=data, timeout=10) as resp:
            return await resp.json()

    async def list_rooms(self) -> List[Dict]:
        try:
            result = await self._get("roominfo")
            return result.get("results") or result.get("data") or [] if isinstance(result, dict) else (result if isinstance(result, list) else [])
        except Exception: return []

    async def join_room_v2(self, roomid: str, sessionname: str, roomfrom: str) -> Dict[str, Any]:
        payload = {"version": "1.0.0", "joininformation": {"addid": self.addid, "roomfrom": roomfrom, "roomid": roomid, "sessionname": sessionname}, "invitecontrol": {"userxuid": self.xuid}}
        return await self._post("v2/join", payload)

    async def get_my_xuid_and_addid(self) -> tuple:
        xuid, addid = "", ""
        try:
            account = await self._get("v2/account")
            if isinstance(account, dict):
                addid = account.get("addid") or account.get("id") or ""
                gt = account.get("gamertag") or account.get("gt") or MC_PLAYER_NAME
                if gt:
                    profile = await self._get("profile", {"gt": gt})
                    if isinstance(profile, dict): xuid = profile.get("xuid") or profile.get("XUID") or ""
        except Exception as e: log.error(f"獲取 XUID/AddID 失敗: {e}")
        return xuid, addid

class WhitelistStore:
    def __init__(self, path: str, defaults: List[str]):
        self.path = path
        self.defaults = defaults
        self._data: List[str] = []
        self.load()

    def load(self) -> List[str]:
        if not os.path.exists(self.path):
            self._data = list(self.defaults)
            self.save()
        else:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._data = [str(x) for x in data] if isinstance(data, list) else list(self.defaults)
            except Exception: self._data = list(self.defaults)
        return self._data

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f: json.dump(self._data, f, indent=4, ensure_ascii=False)

    @property
    def users(self) -> List[str]: return self._data

class BridgeConfig:
    def __init__(self, default_guilds: List[int], default_channels: List[int]):
        self.guild_ids = list(default_guilds)
        self.channel_ids = list(default_channels)

def is_admin(msg: discord.Message) -> bool: return getattr(msg.author.guild_permissions, "administrator", False)
def normalize_cmd(raw: str) -> str: return raw.strip()[1:].strip() if raw.strip().startswith("/") else raw.strip()

def extract_players(raw: str) -> List[str]:
    if not raw: return []
    m = re.search(r"(?:\d+/\d+|玩家在線|Players online|在线)[^\n:：]*[\n:：](.+)", raw, re.IGNORECASE | re.DOTALL)
    tail = m.group(1).strip() if m else raw.strip().split("\n")[-1].strip()
    tail = re.sub(r"^\d+/\d+\s*", "", tail)
    if not tail or tail in ("0", "無", "No players online"): return []
    return [x.strip() for x in tail.split(",") if x.strip()]

@dataclass
class PendingCommand:
    future: asyncio.Future
    timestamp: float

class MCWebSocketServer:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._clients: Set[WebSocketServerProtocol] = set()
        self._pending: Dict[str, PendingCommand] = {}
        self._connected = asyncio.Event()
        self._server_task: Optional[asyncio.Task] = None
        self.on_chat: Optional[Callable] = None
        self.on_join: Optional[Callable] = None
        self.on_leave: Optional[Callable] = None
        self.on_death: Optional[Callable] = None
        self.on_extra_event: Optional[Callable] = None
        self.on_status_change: Optional[Callable] = None
        self.on_server_connected: Optional[Callable] = None
        self.on_server_disconnected: Optional[Callable] = None

    @property
    def connected(self) -> bool: return self._connected.is_set()

    async def _notify_status(self, connected: bool):
        if self.on_status_change:
            try: await self.on_status_change(connected)
            except Exception: pass

    async def start(self):
        if self._server_task and not self._server_task.done(): return
        self._server_task = asyncio.create_task(self._run_server())
        try: await asyncio.wait_for(self._connected.wait(), timeout=30); log.info("✅ Minecraft 已連線")
        except asyncio.TimeoutError: log.warning("⏳ 等待 Minecraft 連線逾時，仍在背景監聽")

    async def stop(self):
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            try: await self._server_task
            except asyncio.CancelledError: pass

    async def _run_server(self):
        async def process_request(path, request_headers):
            try: connection = request_headers.get("Connection", "").lower(); upgrade = request_headers.get("Upgrade", "").lower()
            except AttributeError:
                headers = getattr(request_headers, 'headers', {})
                connection = headers.get("Connection", "").lower(); upgrade = headers.get("Upgrade", "").lower()
            return (426, [("Content-Type", "text/plain")], b"Upgrade Required\n") if "upgrade" not in connection or "websocket" not in upgrade else None

        async def handler(websocket): await self._on_connect(websocket)

        async with serve(handler, self.host, self.port, ping_interval=20, ping_timeout=10, max_size=2**20, process_request=process_request):
            log.info(f"✅ WebSocket 伺服器啟動於 {self.host}:{self.port}")
            await asyncio.Future()

    async def _on_connect(self, ws: WebSocketServerProtocol):
        self._clients.add(ws)
        self._connected.set()
        log.info(f"🟢 Minecraft 已連線 ({ws.remote_address})")
        await self._notify_status(True)
        for evt in SUBSCRIBE_EVENTS:
            sub = {"header": {"version": 1, "requestId": str(uuid.uuid4()), "messageType": "commandRequest", "messagePurpose": "subscribe"}, "body": {"eventName": evt}}
            try: await ws.send(json.dumps(sub))
            except Exception: pass
        
        success_msg = "§l§c[§btangkent3§a] §6ws伺服器連接成功§f"
        tellraw_payload = {"header": {"version": 1, "requestId": str(uuid.uuid4()), "messageType": "commandRequest", "messagePurpose": "commandRequest"}, "body": {"commandLine": f'tellraw @a {{"rawtext":[{{"text":{json.dumps(success_msg)}}}]}}', "version": 1, "origin": {"type": "player"}}}
        try: await ws.send(json.dumps(tellraw_payload))
        except Exception: pass
        
        if self.on_server_connected:
            try: await self.on_server_connected()
            except Exception: pass

        try:
            async for raw in ws: await self._handle_message(raw)
        except websockets.ConnectionClosed: pass
        except Exception as e: log.error(f"接收迴圈錯誤: {e}")
        finally:
            self._clients.discard(ws)
            if not self._clients:
                self._connected.clear()
                log.warning("🔴 Minecraft 已完全斷線")
                await self._notify_status(False)
                if self.on_server_disconnected:
                    try: await self.on_server_disconnected()
                    except Exception: pass

    async def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except Exception:
            return
        
        try:
            header = data.get("header", {})
            event_name = header.get("eventName") or data.get("eventName") or data.get("type", "")
            purpose = header.get("messagePurpose", "")

            if purpose == "commandResponse":
                req_id = header.get("requestId")
                if req_id and req_id in self._pending:
                    pending = self._pending.pop(req_id)
                    if not pending.future.done():
                        body = data.get("body", {})
                        out = body.get("statusMessage") or body.get("output") or body.get("result") or "指令執行成功"
                        pending.future.set_result(str(out))
                return

            # 🌟 核心拦截处理：TP 平台防御
            if event_name == "TP_DETECT":
                full_msg = data.get("body", {}).get("message", "")
                log.warning(f"⚠️ 偵測到 TP 平台特徵！內容: {full_msg}")
                
                # 利用正則提取名字
                target_name = None
                match = re.search(r'(?:名稱:|\[\+\])\s*([a-zA-Z0-9_ ]+)', full_msg)
                if match: target_name = match.group(1).strip()

                if target_name:
                    kick_cmd = f'kick "{target_name}" §c系統攔截：禁止使用 TP 平台加入！'
                    kick_res = await self.send_command(kick_cmd, timeout=5)
                    log.info(f"🔨 嘗試踢出 {target_name} 結果: {kick_res}")
                    await self._send_to_discord("防禦系統", f"🛑 **TP 平台防禦觸發**\n目標: **{target_name}**\n訊息: `{full_msg}`\n已嘗試踢出。")
                else:
                    await self._send_to_discord("防禦系統", f"🛑 **TP 平台防禦觸發**\n訊息: `{full_msg}`\n(無法自動提取名字，請手動處理)")
                return

            log.info(f"📦 [原始封包]: {raw}")
            body = data.get("body", {})

            if event_name in ["PlayerMessage", "chat", "onChat", "message"]:
                sender = body.get("sender") or body.get("Sender") or body.get("player") or "未知"
                message = body.get("message") or body.get("Message") or ""
                if sender not in ("外部", "Server", "系統", "Unknown"):
                    msg = re.sub(r'§.', '', str(message)).strip()
                    if msg and not any(tag in msg for tag in ["[Discord]", "[機器人]", "[DC系統日誌]"]):
                        log.info(f"💬 收到聊天: {sender} -> {msg}")
                        if self.on_chat: await self.on_chat(sender, msg)
                return

            if event_name in ["PlayerJoin", "onPlayerJoin"]:
                p = body.get("player", {}).get("name") or body.get("player") or body.get("name")
                if p and ENABLE_JOIN_LEAVE and self.on_join: await self.on_join(p)
            elif event_name in ["PlayerLeave", "onPlayerLeave"]:
                p = body.get("player", {}).get("name") or body.get("player") or body.get("name")
                if p and ENABLE_JOIN_LEAVE and self.on_leave: await self.on_leave(p)
            elif event_name in ["PlayerDied", "onPlayerDied"]:
                p = body.get("player", {}).get("name") or body.get("player") or body.get("name")
                msg = body.get("message") or "死亡了"
                if p and ENABLE_DEATH and self.on_death: await self.on_death(p, str(msg))

        except Exception as e:
            log.debug(f"訊息解析錯誤: {e}")

    async def send_command(self, cmd: str, timeout: float = 10.0) -> Optional[str]:
        if not self._clients: return None
        req_id = str(uuid.uuid4())
        payload = {"header": {"version": 1, "requestId": req_id, "messageType": "commandRequest", "messagePurpose": "commandRequest"}, "body": {"commandLine": cmd, "version": 1, "origin": {"type": "player"}}}
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[req_id] = PendingCommand(future=fut, timestamp=loop.time())
        sent = False
        for ws in list(self._clients):
            try: await ws.send(json.dumps(payload)); sent = True; break
            except Exception: self._clients.discard(ws)
        if not sent: self._pending.pop(req_id, None); return None
        try: return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError: self._pending.pop(req_id, None); return "⚠️ 指令執行超時"
        except Exception: self._pending.pop(req_id, None); return None

    async def send_tellraw(self, text: str):
        cmd = f'tellraw @a {{"rawtext":[{{"text":{json.dumps(text)}}}]}}'
        await self.send_command(cmd, timeout=5.0)

class DCListener(discord.Client):
    def __init__(self, mc: MCWebSocketServer, whitelist: WhitelistStore, webhook_url: str, config: BridgeConfig, miaaoo: MiaaooAPI):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.mc = mc; self.whitelist = whitelist; self.webhook_url = webhook_url; self.config = config; self.miaaoo = miaaoo
        self._session = None
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.miaaoo.start()
        self.tree.clear_commands(guild=None)
        self.tree.add_command(self.slash_ping)
        try: await self.tree.sync()
        except Exception: pass

    async def on_ready(self):
        log.info(f"✅ Discord 已登入：{self.user}")
        import aiohttp
        self._session = aiohttp.ClientSession()

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.webhook_id is not None: return
        content = message.content.strip()
        if not content: return
        if self.config.guild_ids and (message.guild is None or message.guild.id not in self.config.guild_ids): return
        if self.config.channel_ids and message.channel.id not in self.config.channel_ids: return

        if content.startswith("!"): await self._handle_command(message); return
        if ENABLE_DISCORD_TO_MC and self.mc.connected:
            formatted = CHAT_FORMAT_DC_TO_MC.format(username=message.author.display_name, message=message.clean_content)
            await self.mc.send_tellraw(formatted)

    async def _send_to_discord(self, username: str, content: str):
        if self.config.channel_ids:
            for cid in self.config.channel_ids:
                channel = self.get_channel(cid)
                if channel:
                    try: await channel.send(content)
                    except Exception as e: log.error(f"發送到頻道 {cid} 失敗: {e}")
        if self.webhook_url and self._session:
            try:
                payload = {"username": username[:80], "content": content[:1900], "avatar_url": f"https://mc-heads.net/avatar/{username}/64"}
                async with self._session.post(self.webhook_url, json=payload, timeout=5) as resp: await resp.text()
            except Exception: pass

    async def on_mc_chat(self, sender: str, message: str): await self._send_to_discord(sender, CHAT_FORMAT_MC_TO_DC.format(sender=sender, message=message))
    async def on_mc_join(self, player: str):
        if ENABLE_JOIN_LEAVE: await self._send_to_discord("伺服器", f"✅ **{player}** 加入了遊戲")
    async def on_mc_leave(self, player: str):
        if ENABLE_JOIN_LEAVE: await self._send_to_discord("伺服器", f"❌ **{player}** 離開了遊戲")
    async def on_mc_death(self, player: str, msg: str):
        if ENABLE_DEATH: await self._send_to_discord("伺服器", msg or f"💀 **{player}** 死亡了")

    async def on_ws_connected(self): await self._send_to_discord("系統", "🟢 ws伺服器連接成功")
    async def on_ws_disconnected(self): await self._send_to_discord("系統", "🔴 ws伺服器已斷開連接")

    def _check_perm(self, msg: discord.Message) -> bool: return is_admin(msg) or self._authorized(msg.author)
    def _authorized(self, author: discord.Member) -> bool: return str(author.id) in self.whitelist.users or author.name in self.whitelist.users

    @app_commands.command(name="ping", description="測試機器人是否在線")
    async def slash_ping(self, interaction: discord.Interaction): await interaction.response.send_message("🏓 Pong！", ephemeral=True)

    async def _handle_command(self, message: discord.Message):
        cmd = message.content
        if cmd.startswith("!sync"):
            if not self._check_perm(message): return
            await message.reply("🔄 同步中...")
            self.tree.clear_commands(guild=None); self.tree.add_command(self.slash_ping)
            await self.tree.sync(); await message.reply("✅ 已全域同步")

async def main():
    if not DISCORD_TOKEN: return
    miaaoo = MiaaooAPI()
    config = BridgeConfig(INIT_ALLOWED_GUILDS, INIT_ALLOWED_CHANNELS)
    whitelist = WhitelistStore(WHITELIST_FILE, DEFAULT_AUTHORIZED)
    mc = MCWebSocketServer(MC_HOST, MC_PORT)
    bot = DCListener(mc, whitelist, WEBHOOK_URL, config, miaaoo)

    mc.on_chat = bot.on_mc_chat
    mc.on_join = bot.on_mc_join
    mc.on_leave = bot.on_mc_leave
    mc.on_death = bot.on_mc_death
    mc.on_server_connected = bot.on_ws_connected
    mc.on_server_disconnected = bot.on_ws_disconnected

    ws_task = asyncio.create_task(mc.start())
    try: await bot.start(DISCORD_TOKEN)
    finally:
        ws_task.cancel()
        try: await ws_task
        except asyncio.CancelledError: pass
        await mc.stop(); await miaaoo.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n🛑 已停止")