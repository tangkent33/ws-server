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
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import aiohttp
import discord
import websockets
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv
from websockets import serve
from websockets.legacy.server import WebSocketServerProtocol

__version__ = "9.7.14"

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
def _data_path(name: str, default: str) -> str:
    """資料檔路徑：支援 ~ 與絕對路徑；相對路徑一律「相對於專案目錄」。

    以前 WHITELIST_FILE 預設是 ~/whitelist.json，而 blacklist.json 在專案目錄，
    結果專案裡那份 whitelist.json 根本不會被讀到 —— 改了也沒反應，很難排查。
    """
    raw = os.path.expanduser((os.getenv(name) or default).strip())
    path = Path(raw)
    return str(path if path.is_absolute() else BASE_DIR / path)

WHITELIST_FILE = _data_path("WHITELIST_FILE", "whitelist.json")
BLACKLIST_FILE = _data_path("BLACKLIST_FILE", "blacklist.json")
CONFIG_FILE = os.path.expanduser(os.getenv("BRIDGE_CONFIG_FILE", "~/bridge_config.json"))
# 記錄「每個伺服器上次成功同步的指令樹簽名」，重啟時簽名沒變就跳過 sync，
# 避免每次啟動都 Bulk Overwrite（PUT）整份指令，撞 Discord 的覆蓋限流（429）。
COMMAND_SYNC_FILE = _data_path("COMMAND_SYNC_FILE", "command_sync.json")
# 這個檔案存在代表「這次是使用者主動停的」，run_bot.bat 看到就不要自動重啟
STOP_FLAG = BASE_DIR / "bot_stop.flag"
# 舊變數名 MIAOO_ 少一個 A，兩種拼法都接受，避免 .env 改了名字反而失效
MIAOO_API_BASE = (os.getenv("MIAAOO_API_BASE")
                  or os.getenv("MIAOO_API_BASE")
                  or "https://api.miaaoo.com").rstrip("/")
OWNER_ID = os.getenv("OWNER_ID", "tangkent3")

# 自己的 XUID：填了就完全跳過 /profile 查詢（換過 Gamertag 時很有用）
MC_XUID = (os.getenv("MC_XUID") or "").strip()

# miaaoo 官方用來發 Xbox 邀請的帳號。你按「加入房間」後，平台是用這批號加你好友、
# 再發邀請，所以你會在 Xbox 邀請列表看到它們 —— **它們不是入侵者，任何情況都不踢**。
# 後端會輪換，所以除了下面這幾個，/lianji me 拿到的 addid 帳號（如 MCLobby26）也會自動豁免。
PLATFORM_INVITERS = {
    x.strip().lower()
    for x in (os.getenv("PLATFORM_INVITERS")
              or "Happy2026MC,HelloMC25,HelloMC25a,HelloMC25b5988,MCLobby26").split(",")
    if x.strip()
}

# ------------------------------------------------------------------ 狀態欄
# 一條常駐訊息，定時改寫成「2/20 個玩家在線 | ... 伺服器已運行 30 分鐘」這種一行式
STATUS_FILE = _data_path("STATUS_FILE", "statusboard.json")
STATUS_INTERVAL = max(20, _env_int("STATUS_INTERVAL", 60))   # 刷新間隔（秒）
MC_MAX_PLAYERS = max(1, _env_int("MC_MAX_PLAYERS", 20))      # 讀不到 list 回顯時的上限
STATUS_AUTO_START = _env_bool("STATUS_AUTO_START", "true")  # 啟動時自動接續上次的狀態欄
BOT_STARTED_AT = time.time()                                # 讀不到遊戲進程時的備援基準

# ------------------------------------------------------------------ 封禁
TP_BAN_MINUTES = _env_int("TP_BAN_MINUTES", 0)              # 0 = 永久
TP_AUTO_BLACKLIST = _env_bool("TP_AUTO_BLACKLIST")
ENFORCE_BAN_ON_JOIN = _env_bool("ENFORCE_BAN_ON_JOIN", "true")
# 外部封禁名單（BAN_RECORDS_API_BASE）命中時是否直接踢出（預設開）
ENFORCE_EXTERNAL_BAN_ON_JOIN = _env_bool("ENFORCE_EXTERNAL_BAN_ON_JOIN", "true")
# 每秒主動掃描：list 拿到在線玩家後，用緩存的 acz.cc.cd 黑名單比對，命中就踢。
# 這跟「加入時檢查」互補：DLL 抓不到加入事件（多人世界常發生）時，靠每秒 list 補抓。
EXTERNAL_BAN_SCAN = _env_bool("EXTERNAL_BAN_SCAN", "true")
EXTERNAL_BAN_SCAN_SECONDS = max(1, _env_int("EXTERNAL_BAN_SCAN_SECONDS", 1))  # 掃描週期
EXTERNAL_BAN_SCAN_COOLDOWN = _env_int("EXTERNAL_BAN_SCAN_COOLDOWN", 30)        # 同一人幾秒內不重複踢
# 踢人用 @a[name="玩家名"] 選擇器（true）還是 kick "玩家名"（false）
EXTERNAL_BAN_KICK_SELECTOR = _env_bool("EXTERNAL_BAN_KICK_SELECTOR", "true")

# 偵測到 TP 平台拉人時：直接踢出（預設開）
TP_KICK = _env_bool("TP_KICK", "true")
TP_KICK_REASON = os.getenv("TP_KICK_REASON", "偵測到 TP 平台加入")
TP_KICK_COOLDOWN = _env_int("TP_KICK_COOLDOWN", 20)         # 同一人幾秒內不重複踢
TP_ALERT_COOLDOWN = _env_int("TP_ALERT_COOLDOWN", 180)      # 同一類 TP 提示幾秒內只發一次

# 「玩家嘗試加入」要不要同時打在遊戲公屏上（走遊戲自己的 WebSocket，不用鍵盤注入）
TP_ANNOUNCE = _env_bool("TP_ANNOUNCE", "true")
TP_ANNOUNCE_TEXT = os.getenv("TP_ANNOUNCE_TEXT", "§e[TP] §f玩家嘗試加入：§c{player}")

# 遊戲 WebSocket 沒連線時，指令排隊等連上再送（不再退回鍵盤注入）
KICK_QUEUE_TTL = _env_int("KICK_QUEUE_TTL", 600)            # 排隊多久還沒連上就作廢（秒）
KICK_QUEUE_MAX = _env_int("KICK_QUEUE_MAX", 50)             # 佇列上限，避免爆量

# 斜線指令「常駐」Discord：bot 關閉時**不主動刪除**，重啟後直接刷新覆蓋。
# （舊版曾做過「離線就刪指令」，但 Discord 對指令覆蓋限流每 10 分鐘只 2 次，
#   退出清空 + 重啟註冊連續消耗額度，撞 429 後指令就消失、註冊不回來。）
BAN_API_PORT = _env_int("BAN_API_PORT", 3001)
BAN_API_KEY = os.getenv("BAN_API_KEY", "my-secret-123")

# 網頁控制面板（HTML GUI，exe 也連這裡）：http://127.0.0.1:8080
GUI_PORT = _env_int("GUI_PORT", 8080)
GUI_HTML = BASE_DIR / "gui.html"

# ------------------------------------------------------------------ 事件開關
ENABLE_JOIN_LEAVE = _env_bool("ENABLE_JOIN_LEAVE", "true")
ENABLE_DEATH = _env_bool("ENABLE_DEATH", "true")
ENABLE_TP_ALERT = _env_bool("ENABLE_TP_ALERT", "true")
ENABLE_BAN_ALERT = _env_bool("ENABLE_BAN_ALERT", "true")
ENABLE_CHAT = _env_bool("ENABLE_CHAT", "true")
ENABLE_COMMAND = _env_bool("ENABLE_COMMAND", "true")

# 單向模式：只把遊戲事件送到 Discord，不嘗試送聊天回遊戲
ONE_WAY_MODE = _env_bool("ONE_WAY_MODE", "true")

# ------------------------------------------------------------------ 遊戲內指令可開關的功能
# 房主在遊戲聊天打「!ws」開頭的指令，就能即時開/關這些功能（不用重啟 bot）。
# 預設值從上面的 .env 常數來；運行時只改 FEATURE_STATE，不碰 .env。
_FEATURE_DEFAULTS = {
    "join":   ENABLE_JOIN_LEAVE,
    "death":  ENABLE_DEATH,
    "tp":     ENABLE_TP_ALERT,
    "tpkick": TP_KICK,
    "tpann":  TP_ANNOUNCE,
    "ban":    ENABLE_BAN_ALERT,
    "mcdc":   ENABLE_CHAT,          # 遊戲聊天 → Discord
    "dcmc":   (not ONE_WAY_MODE),    # Discord → 遊戲聊天
}
FEATURE_ALIASES = {
    "join": "join", "joinmsg": "join", "jl": "join",
    "death": "death", "die": "death",
    "tp": "tp", "tpalert": "tp",
    "tpkick": "tpkick", "kick": "tpkick",
    "tpann": "tpann", "announce": "tpann", "tpannounce": "tpann",
    "ban": "ban", "banalert": "ban",
    "mcdc": "mcdc", "mc2dc": "mcdc", "mctodc": "mcdc", "gametodc": "mcdc",
    "dcmc": "dcmc", "dc2mc": "dcmc", "dctomc": "dcmc", "dctogame": "dcmc",
}
FEATURE_NAMES = {
    "join": "加入/離開提示",
    "death": "死亡提示",
    "tp": "TP 偵測提示",
    "tpkick": "TP 自動踢出",
    "tpann": "TP 公屏播報",
    "ban": "封禁事件提示",
    "mcdc": "遊戲→DC 聊天",
    "dcmc": "DC→遊戲 聊天",
}
FEATURE_STATE: Dict[str, bool] = dict(_FEATURE_DEFAULTS)


def feature_state(key: str) -> bool:
    """功能目前是開還是關（會被遊戲內 !ws 指令即時改動）。"""
    return bool(FEATURE_STATE.get(key, _FEATURE_DEFAULTS.get(key, True)))


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
DC_BUFFER_TTL = 180.0         # 秒：Discord 還沒登入時暫存的事件最多留 3 分鐘
DC_BUFFER_MAX = 40            # 暫存上限，避免剛開機就被洗版
MAX_MESSAGE = 1900            # Discord 單則訊息上限 2000，留點邊界
LOCK_PORT = 27999             # 單一實例鎖
LIST_POLL_SECONDS = max(15, _env_int("LIST_POLL_SECONDS", 30))  # list 對帳週期
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

# TP Multiplayer 外掛自己的橫幅。遊戲一載入、每次廣播都會印，
# 例如「* 外部 在 TP Multiplayer 上發現全球所有xbox好友的世界 …」
# 那不是有人用 TP 進來，只是外掛在講話 → 不能當成入侵告警。
TP_BANNER_MARKERS = (
    "發現全球", "发现全球", "世界列表", "网站查看", "網站查看",
    "无需添加好友", "無需添加好友", "xbox好友", "Xbox好友",
    # 遊戲自己印的橫幅本身就帶這個網址（tpmultiplayer.xyz），
    # 所以只要出現就一定不是「有人透過 TP 加入」——真正的加入事件只會給玩家名。
    "tp multiplayer", "tpmultiplayer",
)

PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

TP_DOMAIN_RE = re.compile(r"tpmultiplayer\.([a-z0-9]+)", re.IGNORECASE)

def is_tp_banner(text: str) -> bool:
    """是不是 TP 外掛自己印的說明文字 / 記憶體噪聲（不是真的有人加入）。"""
    lowered = str(text or "")
    if any(marker in lowered for marker in TP_BANNER_MARKERS):
        return True
    # "TP Multiplayer (tpmultiplayer.d)"：網域被截斷成噪聲，只有 .xyz 才算數
    domain = TP_DOMAIN_RE.search(lowered)
    if domain and domain.group(1).lower() != "xyz":
        return True
    return False

def valid_player_name(name: str) -> bool:
    """合法的基岩版玩家名：3~16 位英數或底線。

    記憶體碎片會混進 "tpmultiplayer.d"、"x8" 這種東西，
    拿去 kick 只會讓遊戲回「沒有與選擇器匹配的目標」。
    """
    name = str(name or "").strip()
    if not PLAYER_NAME_RE.match(name):
        return False
    if "tpmultiplayer" in name.lower():
        return False
    return True

def looks_like_player_name(name: str) -> bool:
    """加入 / 離開事件用的寬鬆版（允許含空格的 gamertag）。

    基岩版的 gamertag 最少 3 個字元，所以 "gx"、"ys"、"c_"、"gZs"
    這種 2~3 字元的片段一定是掃描切壞的記憶體碎片，不該當成有人加入。
    但 "Bad Tooth4274" 這種帶空格的正常名字要放行。
    """
    name = str(name or "").strip()
    if not name or "tpmultiplayer" in name.lower():
        return False
    if PLAYER_NAME_RE.match(name):
        return True
    if "  " in name:
        return False                      # 連續空白 → 碎片
    parts = name.split(" ")
    if not 1 <= len(parts) <= 3 or any(len(p) < 2 for p in parts):
        return False
    if not all(re.fullmatch(r"[A-Za-z0-9_]+", p) for p in parts):
        return False
    return 3 <= sum(len(p) for p in parts) <= 16

def is_platform_inviter(name: str) -> bool:
    """是不是 miaaoo 官方發邀請的帳號（Happy2026MC / HelloMC25 系列）。

    這批號是你按「加入房間」後、平台派來加你好友並發 Xbox 邀請的，
    屬於正常流程。所有「踢人 / 封禁」的地方都要先問過這一關，
    否則會把自己加入房間的邀請方踢掉。
    """
    return str(name or "").strip().lower() in PLATFORM_INVITERS

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
    if not raw or looks_like_no_world(raw):
        return []
    match = re.search(r"(?:玩家在線|線上玩家|Players online)[^:：\n]*[:：]\s*(.+)",
                      raw, re.IGNORECASE)
    tail = match.group(1).strip() if match else raw.strip()
    if not tail or tail in ("0", "無"):
        return []
    return [x.strip() for x in tail.split(",") if x.strip()]


_NO_WORLD_MARKERS = (
    "未知的命令", "未知的指令", "無法辨識的命令", "命令不存在",
    "unknown command", "unknown or incomplete command",
    "syntax error: unexpected",
)


def looks_like_no_world(text: str) -> bool:
    """遊戲回「未知的命令」＝現在不在任何世界裡（房間已關 / 還沒進世界）。

    這是目前唯一可靠的判準：MC-WS.exe 只要還開著，WebSocket 就一直連著，
    所以「橋接器有連線」≠「人在世界裡」。離開世界後所有指令都會被遊戲
    以「未知的命令」拒絕（連 `list` / `time query` 都一樣）。
    """
    low = str(text or "").lower()
    return any(m in low for m in _NO_WORLD_MARKERS)


# ------------------------------------------------------------------ 狀態列用的格式化
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

def cn_stamp(moment: Optional[datetime] = None) -> str:
    """周六, 07. 11月 2026 14:05:12 CST（照 `%a` 會被系統語系影響，所以自己排）。"""
    t = moment or datetime.now()
    return (f"{_WEEKDAYS[t.weekday()]}, {t.day:02d}. {t.month}月 {t.year} "
            f"{t.strftime('%H:%M:%S')} CST")

def humanize_duration(seconds: float) -> str:
    """90 → 1 分鐘；5400 → 1 小時 30 分鐘；200000 → 2 天 7 小時。"""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小時"
    if hours:
        return f"{hours} 小時 {minutes} 分鐘"
    if minutes:
        return f"{minutes} 分鐘"
    return f"{secs} 秒"

def parse_list_counts(raw: str) -> Tuple[int, int]:
    """從 `list` 回顯抓 (在線, 上限)。

    基岩版常見：`There are 2/8 players online:` 或
    `There are 2 of a max of 8 players online:`；中文可能是「在線 2/8」「共 5 名，最多 12 名」。
    """
    raw = raw or ""
    m = re.search(r"(\d+)\s*/\s*(\d+)", raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.search(r"(\d+)\s+of\s+a\s+max\s+of\s+(\d+)", raw, re.IGNORECASE)
    if m2:
        return int(m2.group(1)), int(m2.group(2))
    on = re.search(r"(\d+)\s*(?:名玩家|個玩家|名玩家|人在線)", raw)
    cap = re.search(r"(?:最多|上限|max)\s*[:：]?\s*(\d+)", raw, re.IGNORECASE)
    return (int(on.group(1)) if on else 0), (int(cap.group(1)) if cap else 0)

def game_process_started_at() -> Optional[float]:
    """本機 Minecraft.Windows.exe 的啟動時間（世界運行時長的近似值）。

    讀不到就回 None：遊戲不在這台機器、或已經關掉。
    """
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (0, -1):
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if entry.szExeFile.lower() == "minecraft.windows.exe":
                handle = k32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID)
                if not handle:
                    return None
                try:
                    created = FILETIME()
                    exited = FILETIME()
                    kernel = FILETIME()
                    user = FILETIME()
                    if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                               ctypes.byref(kernel), ctypes.byref(user)):
                        return None
                    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    return ticks / 1e7 - 11644473600        # FILETIME → Unix 時間
                finally:
                    k32.CloseHandle(handle)
            if not k32.Process32NextW(snapshot, ctypes.byref(entry)):
                return None
    finally:
        k32.CloseHandle(snapshot)

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
            # .env 的 DEFAULT_AUTHORIZED_USERS 一律有效。
            # 否則白名單檔被人清空／改壞之後，連自己都會被鎖在外面。
            for d in self.defaults:
                if d not in self._data:
                    self._data.append(d)
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
    acked: bool = False        # 只收到橋接器的「已送出」ACK，還沒等到遊戲回顯

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
        self.on_room_change: Optional[Callable] = None   # 自己加入/離開＝換房間

    # ------------------------------------------------ 對外狀態
    @property
    def connected(self) -> bool:
        return bool(self._clients)

    def game_clients(self) -> List[WebSocketServerProtocol]:
        """扣掉橋接器，剩下的才是遊戲本體那條連線。"""
        return [c for c in self._clients if c is not self._bridge_ws]

    async def _subscribe_game_events(self, ws):
        """向遊戲訂閱 PlayerMessage 等事件。

        記憶體掃描抓聊天不穩定（尤其聊天格式改版後），而遊戲自己的 WebSocket
        本來就支援 subscribe；兩條來源同時來的話由 5 秒去重擋掉重複。
        """
        await asyncio.sleep(1.5)
        if ws not in self._clients or ws is self._bridge_ws:
            return
        for event in ("PlayerMessage", "PlayerJoin", "PlayerLeave"):
            payload = {
                "header": {
                    "version": 1,
                    "requestId": str(uuid.uuid4()),
                    "messageType": "commandRequest",
                    "messagePurpose": "subscribe",
                },
                "body": {"eventName": event},
            }
            try:
                await ws.send(json.dumps(payload))
            except Exception:
                return
            await asyncio.sleep(0.05)
        log.info("📡 已向遊戲訂閱 PlayerMessage / PlayerJoin / PlayerLeave")

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
        # 遊戲端連上時主動訂閱聊天/加入事件（比掃記憶體可靠）；
        # 延遲一下是為了先讓橋接器送 BridgeHello，別把訂閱發給橋接器
        _spawn(self._subscribe_game_events(ws))

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
        body = data.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        # PlayerMessage 事件的 eventName 有兩種位置：
        #   舊版 / DLL 橋接器 → header.eventName
        #   新版 Bedrock → body.eventName（header 只剩 messagePurpose: event）
        # 兩種都要吃，否則新版遊戲本體送的聊天會被當成「沒事件」直接丟掉。
        event = (header.get("eventName")
                 or body.get("eventName")
                 or data.get("eventName") or "")
        purpose = header.get("messagePurpose", "")

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
        """收指令回應。

        橋接器（MC-WS）收到 commandRequest 後會立刻回一則「sent」ACK，那不是
        遊戲的真正回顯；以前把第一個回應就當答案，於是 /check、/players 全部顯示
        `sent`。這裡把 ACK 只當「已送出」，繼續等遊戲自己那則回應。
        """
        if not req_id:
            return
        pending = self._pending.get(req_id)
        if not pending or pending.future.done():
            return

        text = str(body.get("statusMessage") or body.get("output") or "").strip()
        if text.lower() in ("sent", "empty command"):
            pending.acked = True
            return

        self._pending.pop(req_id, None)
        pending.future.set_result(text or "（遊戲沒有回顯示任何內容）")

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
        props = body.get("properties")
        props = props if isinstance(props, dict) else {}
        payload = (body.get("message") or body.get("sender")
                   or props.get("Message") or props.get("Sender")
                   or body.get("commandLine") or "")
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
            if not feature_state("death"):
                return
            name = self._player_name(body)
            if name and self.on_death:
                await self.on_death(name, str(body.get("message") or ""))

        elif event == "PlayerBan":
            msg = str(body.get("message") or "")
            if msg and feature_state("ban") and self.on_ban:
                await self.on_ban(msg)

        elif event == "TP_JOIN":
            name = strip_colors(self._player_name(body)).strip()
            if not name:
                return
            if not valid_player_name(name):
                log.info(f"🚫 忽略不像玩家名的 TP 事件: {name[:32]!r}")
                return
            if self.on_tp_join:
                await self.on_tp_join(name)

        elif event == "TP_DETECT":
            msg = str(body.get("message") or "")
            if not msg or not feature_state("tp"):
                return
            if is_tp_banner(msg):
                log.debug(f"忽略 TP 外掛橫幅: {_clip(msg, 60)}")
                return
            if self.on_tp:
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
        # PlayerMessage 有兩種 body 格式：
        #   舊版 / DLL 橋接器 → body.sender / body.message
        #   新版 Bedrock       → body.properties.Sender / body.properties.Message
        # 新版還可能把 message 包成 rawtext JSON，這裡一併兼容。
        props = body.get("properties")
        props = props if isinstance(props, dict) else {}
        sender = strip_colors(
            body.get("sender") or props.get("Sender") or self._player_name(body)).strip()
        raw_message = body.get("message") or props.get("Message") or ""
        message = extract_text(raw_message)
        # TP 外掛的橫幅會被 DLL 當成一般聊天送過來（例如「外部: 在TP Multiplayer上發現…」），
        # 那不是玩家發言，一起擋掉。
        if not sender or not message or is_ui_spam(message) or is_tp_banner(message):
            return
        # 房主在遊戲聊天打的控制指令（!ws ...）：開/關功能，不轉發到 Discord
        if sender.lower() == MC_PLAYER_NAME.lower() and message.lstrip().lower().startswith("!ws"):
            await self._handle_game_command(message)
            return
        # 房主在遊戲聊天打「!eat 玩家名」：切回預設模式後 kill 該玩家
        if sender.lower() == MC_PLAYER_NAME.lower() and message.lstrip().lower().startswith("!eat"):
            await self._handle_game_eat(message)
            return
        if not feature_state("mcdc"):
            return
        # 單向模式下自己的訊息也照樣轉發（方便確認聊天有通）
        if not ONE_WAY_MODE and sender.lower() == MC_PLAYER_NAME.lower():
            return
        if message.startswith("[Discord]"):
            return
        log.info(f"💬 {sender}: {message}")
        if self.on_chat:
            await self.on_chat(sender, message)

    async def _handle_game_command(self, message: str) -> None:
        """房主在遊戲聊天打「!ws ...」開/關功能、看目前狀態。

        回覆直接打在遊戲公屏（tellraw），不用開 Discord。
        只改 FEATURE_STATE（記憶體），不寫 .env → 重啟後回到 .env 的預設值。
        """
        def reply(text: str) -> None:
            _spawn(self.tellraw_players(f"§a[WS] §f{text}"))

        tokens = message.strip().split()

        # !ws ／ !ws status → 列出每個功能現在開還關
        if len(tokens) == 1 or (len(tokens) == 2 and tokens[1].lower() == "status"):
            parts = [f"{label}: {'§a開' if feature_state(key) else '§c關'}"
                     for key, label in FEATURE_NAMES.items()]
            reply("　".join(parts))
            return

        toggle = None
        targets: List[str] = []
        for tok in tokens[1:]:
            t = tok.lower()
            if t in ("on", "開", "開啟", "enable"):
                toggle = True
            elif t in ("off", "關", "關閉", "disable"):
                toggle = False
            elif t in ("all", "全部"):
                targets = list(FEATURE_NAMES)
            elif t in FEATURE_ALIASES:
                targets.append(FEATURE_ALIASES[t])

        if toggle is None:
            reply("用法：!ws 功能 on/off（例：!ws tp off）｜ !ws 看目前狀態")
            return
        if not targets:
            reply("看不懂功能名。可用：join / death / tp / tpkick / tpann / ban / all")
            return

        changed = []
        for key in dict.fromkeys(targets):
            FEATURE_STATE[key] = toggle
            changed.append(FEATURE_NAMES[key])
        log.info(f"🎛️ 遊戲內指令切換: {', '.join(changed)} → {'開' if toggle else '關'}")
        reply(f"{'、'.join(changed)} 已{'開' if toggle else '關'}")

    async def _handle_game_eat(self, message: str) -> None:
        """房主在遊戲聊天打「!eat 玩家名」→ 切回預設模式後 kill 該玩家。

        遊戲裡沒有 Discord 的 @ 機制，所以直接打玩家名（`!eat Steve`）。
        一次可以打多個名字（`!eat Steve Alex`），回覆打在公屏。
        """
        def reply(text: str) -> None:
            _spawn(self.tellraw_players(f"§c[eat] §f{text}"))

        tokens = message.strip().split()[1:]
        if not tokens:
            reply("用法：!eat 玩家名（例：!eat Steve）")
            return

        names = [t for t in tokens if t.strip()]
        # 玩家可能照 Discord 習慣多打 @，去掉它
        names = [n[1:] if n.startswith("@") else n for n in names]
        names = [n for n in names if n]

        if not names:
            reply("用法：!eat 玩家名（例：!eat Steve）")
            return

        for name in names:
            if not valid_player_name(name):
                reply(f"{name}：名字不像玩家，已忽略")
                continue
            result = await self.kill_player(name)
            reply(f"{name} → {result}")

    async def _on_player_join(self, body: dict):
        name = self._player_name(body).strip()
        if not name:
            return
        # 名字只是記憶體裡的片段（"gx"、"tpmultiplayer1mj"）就不要當成有人加入。
        # 基岩版 gamertag 最少 3 個字元，2 個字元一定是掃描切壞的碎片。
        if not looks_like_player_name(name):
            log.info(f"🚫 忽略不像玩家名的加入事件: {name[:32]!r}")
            return
        if name.lower() == MC_PLAYER_NAME.lower():
            # 自己（重新）進入世界＝換房間：在線名單先清空，之後的統計跟著新房間走
            self.players.clear()
            log.info("🏠 偵測到自己加入 → 視為切換房間，統計重置")
            if self.on_room_change:
                await self.on_room_change(True)
        self.players.add(name)
        log.info(f"➕ {name} 加入")
        if feature_state("join") and self.on_join:
            await self.on_join(name)

    async def _on_player_leave(self, body: dict):
        name = self._player_name(body).strip()
        if not name:
            return
        if not looks_like_player_name(name):
            log.info(f"🚫 忽略不像玩家名的離開事件: {name[:32]!r}")
            return
        if name.lower() == MC_PLAYER_NAME.lower():
            self.players.clear()
            log.info("🏠 偵測到自己離開 → 視為離開房間，統計重置")
            if self.on_room_change:
                await self.on_room_change(False)
        self.players.discard(name)
        log.info(f"➖ {name} 離開")
        if feature_state("join") and self.on_leave:
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
            # 只收到橋接器的 ACK（遊戲端沒回顯 / 沒連線）
            return "（已送出，但遊戲沒有回顯示內容）" if self._pending.get(req_id) and self._pending[req_id].acked else "⚠️ 指令逾時"
        except Exception:
            return None
        finally:
            self._pending.pop(req_id, None)

    async def send_tellraw(self, text: str):
        cmd = 'tellraw @a {"rawtext":[{"text":%s}]}' % json.dumps(text, ensure_ascii=False)
        return await self.send_command(cmd, timeout=5.0)

    async def tellraw_players(self, text: str) -> bool:
        """把一行字打在遊戲公屏上（`tellraw @a`），只走「遊戲本體」那條 WebSocket。

        刻意不用 send_command：
          - 播報不需要等回顯，等 5 秒只會拖住 TP 事件的處理
          - send_command 會廣播給**所有**連線，包含橋接器 → 橋接器再讓 DLL 注入鍵盤，
            同一句話會在公屏出現兩次
        """
        if not text:
            return False
        clients = self.game_clients()
        if not clients:
            return False

        command = 'tellraw @a {"rawtext":[{"text":%s}]}' % json.dumps(text, ensure_ascii=False)
        data = json.dumps(self._new_payload(command), ensure_ascii=False)

        sent = 0
        for ws in list(clients):
            try:
                await ws.send(data)
                sent += 1
            except Exception:
                self._clients.discard(ws)
        return sent > 0

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

    async def shutdown(request: web.Request):
        """讓 bot 優雅下線：先移除斜線指令，再斷開 Discord。

        stop.bat / `python bot.py --stop` 就是打這支；直接用 taskkill 不會走到這裡。
        """
        if not _api_authorized(request):
            return _unauthorized()
        log.info("🛑 收到 HTTP 關閉請求")
        _spawn(bot.request_shutdown())
        return web.json_response({"ok": True, "message": "正在關閉（會一併移除斜線指令）"})

    app = web.Application()
    app.add_routes([
        web.get("/is-banned/{name}", is_banned),
        web.get("/banned", list_banned),
        web.post("/ban", ban),
        web.post("/unban", unban),
        web.post("/shutdown", shutdown),
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", BAN_API_PORT)
    await site.start()
    log.info(f"✅ Ban API 已啟動 http://127.0.0.1:{BAN_API_PORT} (x-api-key: {BAN_API_KEY})")
    return runner


async def start_gui(bot: "DCListener", blacklist: BanStore) -> web.AppRunner:
    """網頁控制面板（HTML GUI）：http://127.0.0.1:GUI_PORT

    只綁 127.0.0.1，給本機瀏覽器 / 打包好的 exe 用，不另外要求 API key。
    頁面本體讀專案根目錄的 gui.html（改了存檔、重新整理就生效，不用重啟 bot）。
    """
    _GUI_FALLBACK = ("<!doctype html><meta charset=\"utf-8\"><title>MC 橋接面板</title>"
                     "<body style=\"font-family:system-ui;padding:2rem;background:#111;color:#eee\">"
                     "<h1>gui.html 遺失</h1><p>請把 gui.html 放回專案根目錄再重新整理。</p>")

    def _tail(path: Path, n: int) -> list:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            return [ln.rstrip("\n") for ln in lines[-n:]]
        except OSError:
            return []

    async def index(_req: web.Request):
        try:
            html = GUI_HTML.read_text(encoding="utf-8")
        except OSError:
            html = _GUI_FALLBACK
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def state(_req: web.Request):
        ban_records = None
        try:
            ban_records, _note = await bot.ban_records.count()
        except Exception:
            pass
        board_text = ""
        try:
            board_text = bot.board.render()
        except Exception:
            pass
        return web.json_response({
            "version": __version__,
            "discord_ready": bot.is_ready(),
            "in_world": bot._in_world,
            "mc_connected": bool(bot.mc.game_clients()),
            "players": sorted(bot.mc.players),
            "online": len(bot.mc.players),
            "max_players": getattr(bot.board, "_max_players", None),
            "seen": len(bot._seen_players),
            "uptime": int(bot.board.uptime_seconds()),
            "banned": len(blacklist.items()),
            "ban_records": ban_records,
            "channels": len(bot.config.channel_ids),
            "statusboard": board_text,
            "deferred": len(bot._deferred),
        })

    async def log_tail(request: web.Request):
        n = max(1, min(int(request.query.get("n", "120")), 500))
        return web.json_response({"lines": _tail(BASE_DIR / "bot.log", n)})

    async def cmd(request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body 必須是 JSON"}, status=400)
        c = str((data or {}).get("cmd") or "").strip()
        if not c:
            return web.json_response({"error": "要填 cmd"}, status=400)
        result = await bot.send_game_command(c, f"[GUI] {c}", timeout=12)
        return web.json_response({"ok": True, "result": result})

    async def kick(request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body 必須是 JSON"}, status=400)
        player = str((data or {}).get("player") or "").strip()
        reason = str((data or {}).get("reason") or "由面板踢出").strip()
        if not player:
            return web.json_response({"error": "要填 player"}, status=400)
        result = await bot.kick_player(player, reason)
        return web.json_response({"ok": True, "result": result})

    async def ban(request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body 必須是 JSON"}, status=400)
        name = str((data or {}).get("player") or "").strip()
        reason = str((data or {}).get("reason") or "未填理由").strip()
        if not name:
            return web.json_response({"error": "要填 player"}, status=400)
        try:
            minutes = int((data or {}).get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        rec = blacklist.ban(name, reason, minutes, "gui")
        _spawn(bot.kick_player(name, rec["reason"]))
        return web.json_response({"ok": True, "player": name, **rec})

    async def unban(request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body 必須是 JSON"}, status=400)
        name = str((data or {}).get("player") or "").strip()
        if not name:
            return web.json_response({"error": "要填 player"}, status=400)
        return web.json_response({"ok": blacklist.unban(name), "player": name})

    async def shutdown(_req: web.Request):
        _spawn(bot.request_shutdown())
        return web.json_response({"ok": True, "message": "正在關閉"})

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/state", state),
        web.get("/log", log_tail),
        web.post("/cmd", cmd),
        web.post("/kick", kick),
        web.post("/ban", ban),
        web.post("/unban", unban),
        web.post("/shutdown", shutdown),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", GUI_PORT)
    await site.start()
    log.info(f"🖥️ 控制面板已啟動 http://127.0.0.1:{GUI_PORT}")
    return runner

# ============================================================ 6. Miaaoo 聯機大廳 API
# 依 https://docs.miaaoo.com 的 v2 介面實作（文件版 v2.0.0）：
#   GET  /v2/list                       房間列表（lang 可重複，如 ?lang=zh&lang=en）
#   GET  /v2/account                    中繼帳號清單 → addid 來源（canaddfriends=true 的 id）
#   GET  /profile?gt=|xuid=             查 XUID / Gamertag（gt 與 xuid 不可同時傳）
#   GET  /roominfo?session=&roomfrom=   單一房間詳情 + 成員名單
#   POST /v2/join                       apiversion=2（userxuid 必填、16 位數字）
#   GET  /refreshFollowers              刷新粉絲快取（10 秒內重複呼叫只回 206）
#
# 舊版有三個致命偏差，導致 /lianji 永遠空、/join 必定失敗：
#   1. 房間列表去打 /roominfo（缺 session/roomfrom → 400）→ 應該是 /v2/list
#   2. addid 從 /v2/account 的元素裡找 "addid" 欄位 → 實際欄位叫 id
#   3. XUID 從 profile["xuid"] 取 → 實際在 profileUsers[0].id

JOIN_CODE_TEXT = {
    "0": "查不到房間（房主的多人遊戲可能沒開）",
    "1": "參數不完整",
    "2": "addid / roomfrom 不被允許（先用 /lianji me 檢查）",
    "3": "XUID 缺失或格式錯誤（需 16 位數字）",
    "4": "你不在該帳號的粉絲清單中，請先在 Xbox 關注它",
    "5": "自動加入關注失敗（粉絲可能已滿 1000）",
}

WORLD_TYPE_TEXT = {
    "Survival": "生存", "Creative": "創造", "Spectator": "旁觀",
    "Adventure": "冒險", "Unknown": "未知",
}

@dataclass
class Room:
    """把 /v2/list 的巢狀結構壓平成好用的樣子。"""

    id: str
    session: str
    roomfrom: str
    host: str
    world: str
    mode: str
    version: str
    players: int
    max_players: int
    broadcast: int
    lang: str
    owner_xuid: str
    created: str
    hardcore: bool = False

    @property
    def joinable(self) -> bool:
        """BroadcastSetting=3（好友的好友）才能被外部玩家加入。"""
        return self.broadcast == 3

    @property
    def mode_text(self) -> str:
        return WORLD_TYPE_TEXT.get(self.mode, self.mode or "未知")

    @classmethod
    def from_api(cls, raw: Dict[str, Any]) -> "Room":
        props = raw.get("customProperties") or {}
        session_ref = raw.get("sessionRef") or {}
        related = raw.get("relatedInfo") or {}

        def _int(value, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        return cls(
            id=str(raw.get("id") or ""),
            session=str(session_ref.get("name") or ""),
            roomfrom=str(raw.get("roomfrom") or ""),
            host=str(props.get("hostName") or "?"),
            world=str(props.get("worldName") or "?").strip() or "?",
            mode=str(props.get("worldType") or "Unknown"),
            version=str(props.get("version") or "?"),
            players=_int(props.get("MemberCount") or related.get("membersCount")),
            max_players=_int(props.get("MaxMemberCount") or related.get("maxMembersCount")),
            broadcast=_int(props.get("BroadcastSetting")),
            lang=str(props.get("worldNameLang") or ""),
            owner_xuid=str(props.get("ownerId") or raw.get("ownerXuid") or ""),
            created=str(raw.get("createTime") or ""),
            hardcore=bool(props.get("isHardcore")),
        )

    def label(self) -> str:
        """autocomplete 用的短標籤（Discord 選項名上限 100 字）。"""
        lock = "" if self.joinable else "🔒"
        return _clip(f"{lock}{self.world} ｜ {self.host} ｜ {self.players}/{self.max_players}", 95)

@dataclass
class MiaaooResult:
    ok: bool
    message: str
    status: int = 0
    code: str = ""
    data: Any = None

class MiaaooAPI:
    """聯機大廳 API 封裝。任何失敗都回傳結構化結果，不外拋例外打斷 bot。"""

    def __init__(self):
        self.base_url = MIAOO_API_BASE
        self.xuid: Optional[str] = None
        self.addid: Optional[str] = None
        self.gamertag: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._rooms: List[Room] = []
        self._rooms_at: float = 0.0
        self._accounts: List[Dict[str, Any]] = []     # /v2/account 快取（限流很兇）
        self._accounts_at: float = 0.0
        self._accounts_note = ""                       # 取不到時的原因，給 /lianji me 顯示

    async def start(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=8),
                headers={
                    "User-Agent": f"mc-bridge/{__version__}",
                    "Accept": "application/json",
                },
            )

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------ 基本請求
    async def _request(self, method: str, endpoint: str, *,
                       params=None, json_body=None) -> Tuple[int, Any]:
        """回傳 (http_status, payload)。連線或解析失敗時 status=0。"""
        await self.start()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            async with self._session.request(method, url, params=params, json=json_body) as resp:
                status = resp.status
                text = await resp.text()
        except asyncio.TimeoutError:
            return 0, {"message_zh_CN": "請求逾時（api.miaaoo.com 沒回應）"}
        except aiohttp.ClientError as exc:
            return 0, {"message_zh_CN": f"連線失敗：{exc}"}

        try:
            return status, json.loads(text)
        except ValueError:
            # 限流時 nginx 回的是 HTML，不是 JSON
            hint = "（被限流，稍後再試）" if status == 429 else ""
            return status, {"message_zh_CN": f"HTTP {status} 非 JSON 回應{hint}"}

    async def _get(self, endpoint: str, params=None) -> Tuple[int, Any]:
        return await self._request("GET", endpoint, params=params)

    async def _post(self, endpoint: str, payload: Dict) -> Tuple[int, Any]:
        return await self._request("POST", endpoint, json_body=payload)

    # ------------------------------------------------------------ 房間
    async def list_rooms(self, lang: str = "", *, max_age: float = 25.0) -> List[Room]:
        """GET /v2/list。lang 可用逗號分隔多個語言代碼。"""
        if not lang and self._rooms and (time.monotonic() - self._rooms_at) < max_age:
            return self._rooms

        params = None
        wanted = [x.strip() for x in (lang or "").split(",") if x.strip()]
        if wanted:
            # 文件支援重複 key：?lang=zh&lang=en，所以傳 list of tuple
            params = [("lang", code) for code in wanted]

        status, data = await self._get("v2/list", params)
        if status != 200 or not isinstance(data, dict):
            msg = (data or {}).get("message_zh_CN") if isinstance(data, dict) else ""
            log.error(f"取得房間列表失敗: HTTP {status} {msg or data}")
            return [] if lang else self._rooms

        rooms = [Room.from_api(r) for r in (data.get("results") or []) if isinstance(r, dict)]
        if not lang:
            self._rooms = rooms
            self._rooms_at = time.monotonic()
        log.debug(f"房間列表: {len(rooms)} 間（lang={lang or '全部'}）")
        return rooms

    def cached_rooms(self) -> List[Room]:
        return self._rooms

    async def fetch_rooms_if_stale(self, max_age: float = 60.0) -> List[Room]:
        """autocomplete 用：沒有快取或太舊才真的打 API，避免打字就刷爆。"""
        if self._rooms and (time.monotonic() - self._rooms_at) < max_age:
            return self._rooms
        return await self.list_rooms()

    async def room_detail(self, room: Room) -> MiaaooResult:
        """GET /roominfo：房間詳情 + 成員名單。"""
        if not room.session or not room.roomfrom:
            return MiaaooResult(False, "缺少 session 或 roomfrom，無法查詢房間")
        status, data = await self._get("roominfo", {"session": room.session, "roomfrom": room.roomfrom})
        if status != 200 or not isinstance(data, dict):
            msg = (data or {}).get("message_zh_CN") if isinstance(data, dict) else ""
            return MiaaooResult(False, msg or f"HTTP {status}", status=status, data=data)
        return MiaaooResult(True, "", status=status, data=data)

    @staticmethod
    def parse_members(payload: Dict[str, Any]) -> List[Dict[str, str]]:
        """roominfo.members 是 {"0": {...}, "1": {...}}，取出 gamertag / xuid。"""
        out: List[Dict[str, str]] = []
        members = (payload or {}).get("members") or {}
        if not isinstance(members, dict):
            return out
        for key, item in members.items():
            if not isinstance(item, dict) or not item:
                continue
            const = (item.get("constants") or {}).get("system") or {}
            out.append({
                "index": str(const.get("index", key)),
                "gamertag": str(item.get("gamertag") or "(未知)"),
                "xuid": str(const.get("xuid") or ""),
                "join_time": str(item.get("joinTime") or ""),
            })
        out.sort(key=lambda m: int(m["index"]) if m["index"].isdigit() else 999)
        return out

    # ------------------------------------------------------------ 帳號 / 身分
    async def list_accounts(self, force: bool = False) -> List[Dict[str, Any]]:
        """GET /v2/account → 中繼帳號清單 [{id, name, canaddfriends}]。

        這個端點被 nginx 限流得很兇（常常直接回 HTML 429），所以：
          - 成功結果快取 120 秒，`/lianji me` 連按也不會一直打
          - 取不到時**退回上次成功的結果**，不要變成一片空白
        呼叫端可用 `self._accounts_note` 知道這次是即時資料還是舊的。
        """
        now = time.monotonic()
        if not force and self._accounts and now - self._accounts_at < 120:
            self._accounts_note = ""
            return self._accounts

        status, data = await self._get("v2/account")
        if status == 200 and isinstance(data, list):
            accounts = [a for a in data if isinstance(a, dict)]
            if accounts:
                self._accounts = accounts
                self._accounts_at = now
                self._accounts_note = ""
                return accounts
            self._accounts_note = "API 回空清單"
            return []

        msg = (data or {}).get("message_zh_CN") if isinstance(data, dict) else ""
        if status == 429:
            self._accounts_note = "API 限流中（429）"
        else:
            self._accounts_note = f"HTTP {status} {msg or ''}".strip()
        log.warning(f"取得帳號清單失敗: {self._accounts_note}")
        return self._accounts          # 退回上次成功的結果

    async def resolve_addid(self, force: bool = False) -> str:
        """addid = /v2/account 裡 canaddfriends=true 的帳號 id（目前通常 6 或 8）。"""
        if self.addid and not force:
            return self.addid
        accounts = await self.list_accounts()
        if not accounts:
            return ""
        usable = [a for a in accounts if a.get("canaddfriends")] or accounts
        self.addid = str(usable[0].get("id") or "")
        log.info(f"🔑 addid = {self.addid}（{usable[0].get('name', '')}）")
        return self.addid

    async def get_profile(self, gt: str = "", xuid: str = "") -> Dict[str, str]:
        """GET /profile。注意 gt 與 xuid 不能同時傳（會 400）。"""
        if not gt and not xuid:
            return {}
        params = {"gt": gt} if gt else {"xuid": xuid}
        status, data = await self._get("profile", params)
        if status != 200 or not isinstance(data, dict):
            msg = (data or {}).get("message_zh_CN") if isinstance(data, dict) else ""
            log.warning(f"查詢 profile 失敗: HTTP {status} {msg or data}")
            return {}
        users = data.get("profileUsers") or []
        if not users or not isinstance(users[0], dict):
            return {}
        user = users[0]
        settings = {s.get("id"): s.get("value")
                    for s in (user.get("settings") or []) if isinstance(s, dict)}
        return {
            "xuid": str(user.get("id") or ""),
            "gamertag": str(settings.get("Gamertag") or settings.get("GameDisplayName") or ""),
            "avatar": str(settings.get("GameDisplayPicRaw") or ""),
        }

    async def resolve_identity(self, force: bool = False) -> Dict[str, str]:
        """取得 (xuid, addid)。.env 有 MC_XUID 就直接用，不必查 /profile。"""
        if not force and self.xuid and self.addid:
            return {"xuid": self.xuid, "addid": self.addid, "gamertag": self.gamertag or ""}

        if MC_XUID:
            self.xuid = MC_XUID
        addid = await self.resolve_addid(force=force)

        if not self.xuid:
            profile = await self.get_profile(gt=MC_PLAYER_NAME)
            if profile.get("xuid"):
                self.xuid = profile["xuid"]
                self.gamertag = profile.get("gamertag") or MC_PLAYER_NAME

        if self.xuid and not self.gamertag:
            profile = await self.get_profile(xuid=self.xuid)
            self.gamertag = profile.get("gamertag") or MC_PLAYER_NAME

        return {"xuid": self.xuid or "", "addid": addid or "", "gamertag": self.gamertag or ""}

    # ------------------------------------------------------------ 加入 / 刷新
    async def join_room(self, room: Room, *, addid: str = "", xuid: str = "") -> MiaaooResult:
        """POST /v2/join，新版協議 apiversion=2（userxuid 必填）。"""
        addid = addid or self.addid or await self.resolve_addid()
        xuid = xuid or self.xuid or (await self.resolve_identity()).get("xuid", "")

        if not addid:
            return MiaaooResult(False, "取不到 addid（/v2/account 沒有可用帳號）")
        if not xuid.isdigit() or len(xuid) != 16:
            return MiaaooResult(
                False, f"XUID 無效（{xuid or '空'}），需 16 位數字；可在 .env 設 MC_XUID")
        if not room.id or not room.session:
            return MiaaooResult(False, "房間資料不完整（缺 id / session）")

        payload = {
            "apiversion": 2,
            "joininformation": {
                "addid": str(addid),
                "roomfrom": str(room.roomfrom),
                "handleid": room.id,
                "sessionname": room.session,
                "userxuid": str(xuid),
            },
        }
        status, data = await self._post("v2/join", payload)
        body = data if isinstance(data, dict) else {}
        code = str(body.get("code", "") or "")

        if status == 200 and code in ("1", "2", ""):
            return MiaaooResult(True, body.get("message_zh_CN") or "加入成功", status, code, data)

        detail = (JOIN_CODE_TEXT.get(code)
                  or body.get("message_zh_CN")
                  or body.get("message")
                  or f"HTTP {status}")
        log.warning(f"加入房間失敗 room={room.id} HTTP {status} code={code} {detail}")
        return MiaaooResult(False, detail, status, code, data)

    async def refresh_followers(self) -> Tuple[int, str]:
        """GET /refreshFollowers。文件註明：回應碼不可靠，200/206/502 都算正常。"""
        status, data = await self._get("refreshFollowers")
        if status in (200, 206):
            return status, "已觸發刷新（房間列表約 30 秒後更新）"
        if status == 502:
            return status, "部分帳號拉取失敗，但已排入刷新"
        msg = (data or {}).get("message_zh_CN") if isinstance(data, dict) else ""
        return status, msg or f"HTTP {status}"

# ============================================================ 7. Discord 客戶端
def is_admin(message: discord.Message) -> bool:
    perms = getattr(message.author, "guild_permissions", None)
    return bool(perms and getattr(perms, "administrator", False))

@dataclass
class DeferredCmd:
    """遊戲 WebSocket 沒連線時暫存的指令，連上後由 worker 補送。"""
    cmd: str
    label: str
    created: float
    player: str = ""        # 踢人指令才有：補送前先確認這人還在線上

class RoomPaginator(discord.ui.View):
    """房間列表分頁。每頁 6 間，避免整串超過 embed 上限。"""

    PER_PAGE = 6

    def __init__(self, api: MiaaooAPI, rooms: List[Room], lang: str = ""):
        super().__init__(timeout=180)
        self.api = api
        self.rooms = rooms
        self.lang = lang
        self.page = 0
        self._refresh_buttons()

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.rooms) + self.PER_PAGE - 1) // self.PER_PAGE)

    def _page_rooms(self) -> List[Room]:
        start = self.page * self.PER_PAGE
        return self.rooms[start:start + self.PER_PAGE]

    def _refresh_buttons(self):
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages - 1
        self.page_button.label = f"{self.page + 1}/{self.total_pages}"

    def build_embed(self) -> discord.Embed:
        joinable = sum(1 for room in self.rooms if room.joinable)
        desc = f"共 **{len(self.rooms)}** 間，可加入 **{joinable}** 間"
        if self.lang:
            desc += f"　語言：`{self.lang}`"
        embed = discord.Embed(title="📋 聯機大廳", description=desc, color=0x3B82F6)

        offset = self.page * self.PER_PAGE
        for index, room in enumerate(self._page_rooms(), offset + 1):
            badge = "🟢" if room.joinable else "🔒"
            extra = "　💀 極限" if room.hardcore else ""
            embed.add_field(
                name=_clip(f"{badge} {index}. {room.world}", 250),
                value=_clip(
                    f"🏠 {room.host}　{room.mode_text}　👥 {room.players}/{room.max_players}\n"
                    f"🧩 {room.version}{extra}\n`{room.id}`",
                    1000,
                ),
                inline=False,
            )
        embed.set_footer(text="用 /lianji join 加入　/lianji info 看成員名單")
        return embed

    async def _flip(self, interaction: discord.Interaction, delta: int):
        self.page = min(max(0, self.page + delta), self.total_pages - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._flip(interaction, -1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._flip(interaction, 1)

    @discord.ui.button(label="🔄", style=discord.ButtonStyle.success)
    async def reload_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        rooms = await self.api.list_rooms(self.lang)
        if rooms:
            self.rooms = sorted(rooms, key=lambda r: (not r.joinable, -r.players))
            self.page = 0
            self._refresh_buttons()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

# ------------------------------------------------------------------ 外部封禁紀錄統計
# 狀態列的「封禁系統已封禁 N 名玩家」。服務位址與永久 Token 全部從 .env 讀
# （BAN_RECORDS_API_BASE / BAN_RECORDS_TOKEN），程式裡不寫死任何服務位址；
# 沒填就整組停用。/list 回傳的是「紀錄」列表，同一個玩家可能有多筆，所以這裡
# 統計的是「不重複的 playerName 數」。
class BanRecordsCounter:
    CACHE_TTL = 300.0            # 5 分鐘，別每次刷新狀態列都打一次

    def __init__(self):
        self.base = (os.getenv("BAN_RECORDS_API_BASE") or "").rstrip("/")
        self.token = (os.getenv("BAN_RECORDS_TOKEN") or "").strip()
        self.on_update: Optional[Callable] = None   # 每次拿到新數字都會呼叫（fire-and-forget）
        self._session: Optional[aiohttp.ClientSession] = None
        self._count: Optional[int] = None      # None = 還沒查到過
        self._note = ""
        self._at = 0.0
        self._items: List[dict] = []           # 最近一次 /list 的原始紀錄
        self._names: Dict[str, dict] = {}      # 小寫 playerName → 紀錄

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.token)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10, connect=5),
                headers={"Authorization": f"Bearer {self.token}",
                         "User-Agent": f"mc-bridge/{__version__}"})
        return self._session

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def count(self, force: bool = False) -> Tuple[Optional[int], str]:
        """回傳 (不重複玩家數或 None, 最近一次失敗說明)。失敗時退回上次的數字。"""
        if not self.enabled:
            return None, "未設定 BAN_RECORDS_API_BASE / BAN_RECORDS_TOKEN"
        now = time.monotonic()
        if not force and self._count is not None and now - self._at < self.CACHE_TTL:
            return self._count, ""

        session = await self._ensure_session()
        status = 0
        try:
            async with session.get(self.base + "/list") as resp:
                status = resp.status
                text = await resp.text()
        except asyncio.TimeoutError:
            self._note = "請求逾時"
            self._at = now              # 失敗也退避，別讓每秒掃描連環撞限流
            return self._count, self._note
        except aiohttp.ClientError as exc:
            self._note = f"連線失敗：{exc}"
            self._at = now
            return self._count, self._note

        try:
            data = json.loads(text)
        except ValueError:
            self._note = f"HTTP {status} 非 JSON"
            self._at = now
            return self._count, self._note

        items: List[Any] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    items = value
                    break
        if status != 200:
            msg = data.get("message") if isinstance(data, dict) else ""
            self._note = f"HTTP {status}{('：' + str(msg)) if msg else ''}"
            self._at = now
            return self._count, self._note

        self._items = [r for r in items if isinstance(r, dict)]
        self._names = {}
        for r in self._items:
            nm = str(r.get("playerName") or "").strip()
            if nm:
                self._names[nm.lower()] = r
        self._count = len(self._names)
        self._at = now
        self._note = ""
        log.info(f"🛡 封禁紀錄統計更新：{self._count} 名玩家")
        if self.on_update:
            cb = self.on_update
            _spawn(cb(self._count))
        return self._count, ""

    async def lookup(self, player: str) -> Optional[dict]:
        """查一個玩家在不在外部封禁名單，回傳紀錄 dict（含 reason），不在回 None。

        名單沿用 count() 的快取（CACHE_TTL），過期就重抓一次；避免每次有人加入都打 /list。
        """
        if not self.enabled:
            return None
        now = time.monotonic()
        if self._at == 0.0 or now - self._at >= self.CACHE_TTL:
            await self.count(force=True)
        name = str(player or "").strip().lower()
        return self._names.get(name)

    async def cached_names(self) -> Dict[str, dict]:
        """回傳快取的黑名單（小寫 playerName → 紀錄），過期就重抓一次。

        每秒主動掃描會對「整批在線玩家」比對，用這方法一次拿整份名單，
        而不是對每個玩家各打一次 lookup（那樣第一次進來會 force 重抓很多次）。
        """
        if not self.enabled:
            return {}
        now = time.monotonic()
        if self._at == 0.0 or now - self._at >= self.CACHE_TTL:
            await self.count(force=True)
        return self._names


class StatusBoard:
    """一條會自己刷新的伺服器狀態列（一行式）。

        2/20 個玩家在線 ｜ 3 個玩家已加入伺服器 ｜ 伺服器已運行 30 分鐘 ｜ 最近更新時間: ...

    在線人數只用「加入 / 離開」事件累積，**不去打 `list` 指令**：
    那條指令會被橋接器注入遊戲，每分鐘一次既沒必要，又會污染指令佇列。
    真實上限也只在第一次連上時問一次（seed），之後沿用。
    """

    def __init__(self, client: "DCListener"):
        self.client = client
        self.channel_id = 0
        self.message_id = 0
        self.task: Optional[asyncio.Task] = None
        self._proc_cache: Tuple[float, Optional[float]] = (0.0, None)
        self.room_started_at: Optional[float] = None   # 換房間時更新
        self._max_players = MC_MAX_PLAYERS
        self.load()

    # ------------------------------------------------ 持久化
    def load(self):
        data = _read_json(STATUS_FILE, None)
        if not isinstance(data, dict):
            return
        try:
            self.channel_id = int(data.get("channel") or 0)
            self.message_id = int(data.get("message") or 0)
            self._max_players = int(data.get("max") or MC_MAX_PLAYERS)
        except (TypeError, ValueError):
            pass

    def save(self):
        try:
            _atomic_write_json(STATUS_FILE, {
                "channel": self.channel_id,
                "message": self.message_id,
                "max": self._max_players,
            })
        except OSError as exc:
            log.error(f"儲存 statusboard.json 失敗: {exc}")

    # ------------------------------------------------ 資料
    def _game_started_at(self) -> Optional[float]:
        """遊戲進程啟動時間，快取 5 分鐘（每分鐘開一次快照太浪費）。"""
        now = time.time()
        stamp, cached = self._proc_cache
        if now - stamp < 300:
            return cached
        try:
            cached = game_process_started_at()
        except Exception:
            cached = None
        self._proc_cache = (now, cached)
        return cached

    def mark_room_switch(self):
        """換房間：運行時間從現在重新算（bot 重啟後沒偵測到就退回遊戲進程時間）。"""
        self.room_started_at = time.time()

    def uptime_seconds(self) -> float:
        base = self.room_started_at or (self._game_started_at() or BOT_STARTED_AT)
        return max(0.0, time.time() - base)

    def render(self) -> str:
        if getattr(self.client, "_in_world", None) is False:
            return (f"⚪ **未進入世界 / 指令不可用**（房間已關或世界沒開指令） ｜ "
                    f"最近更新時間: {cn_stamp()}")
        online = len(self.client.mc.players)
        joined = len(self.client._seen_players)
        dot = "🟢" if self.client.mc.game_clients() else "🔴"
        return (f"{dot} **{online}/{self._max_players}** 個玩家在線 ｜ "
                f"**{joined}** 個玩家已加入伺服器 ｜ "
                f"伺服器已運行 **{humanize_duration(self.uptime_seconds())}** ｜ "
                f"最近更新時間: {cn_stamp()}")

    async def seed(self):
        """連上遊戲後問一次 `list`：補齊「bot 啟動前就在線的人」與真實上限。"""
        if not self.client.mc.game_clients():
            return
        try:
            raw = self.client._clean_result(
                await self.client.mc.send_command("list", timeout=6))
        except Exception as exc:
            log.debug(f"狀態列 seed 失敗: {exc}")
            return
        if looks_like_no_world(raw):
            self.client._in_world = False
            return
        online, cap = parse_list_counts(raw)
        if cap:
            self._max_players = cap
        for name in extract_players(raw):
            self.client.mc.players.add(name)
            self.client._seen_players.add(name)
        if online:
            log.info(f"📊 狀態列 seed：遊戲回報 {online}/{self._max_players} 人")

    # ------------------------------------------------ 動作
    async def reseed(self):
        """換房間後重新問一次 list：在線人數與上限跟著新房間走。"""
        await self.seed()
        await self.refresh()

    async def _records_segment(self) -> str:
        """「🛡 封禁系統已封禁 N 名玩家」：只在外部紀錄源連上時才顯示。"""
        counter = getattr(self.client, "ban_records", None)
        if counter is None or not counter.enabled:
            return ""
        count, _note = await counter.count()
        if count is None:
            return ""                     # 沒連上就不顯示，避免狀態列一直報錯
        return f" ｜ 🛡 封禁系統已封禁 **{count}** 名玩家"

    async def refresh(self) -> str:
        channel = self.client.get_channel(self.channel_id)
        if channel is None:
            return "（找不到頻道）"

        text = self.render() + await self._records_segment()

        if self.message_id:
            try:
                message = await channel.fetch_message(self.message_id)
                if message.content != text:
                    await message.edit(content=text)
                return text
            except discord.NotFound:
                self.message_id = 0           # 訊息被刪了，下面重建
            except discord.HTTPException as exc:
                log.warning(f"狀態列更新失敗: {exc}")
                return text

        message = await channel.send(text)
        self.message_id = message.id
        self.save()
        try:
            await message.pin(reason="伺服器狀態列")
        except discord.HTTPException:
            pass                              # 沒有管理訊息權限就算了
        return text

    async def start(self, channel) -> str:
        self.channel_id = channel.id
        self.message_id = 0                   # 換頻道就重建一條
        self.save()
        await self.seed()
        text = await self.refresh()
        self._ensure_loop()
        log.info(f"📊 狀態列已啟動（每 {STATUS_INTERVAL} 秒刷新）")
        return text

    async def stop(self, delete_message: bool = True):
        """停止刷新。delete_message=False 時保留記錄，下次啟動可以接續同一條訊息。"""
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None

        if delete_message:
            if self.message_id:
                channel = self.client.get_channel(self.channel_id)
                if channel is not None:
                    try:
                        message = await channel.fetch_message(self.message_id)
                        await message.delete()
                    except discord.HTTPException:
                        pass
            self.channel_id = 0
            self.message_id = 0
            self.save()
            log.info("📊 狀態列已停止")
        else:
            log.info("📊 狀態列已暫停（記錄保留，下次啟動接續）")

    def _ensure_loop(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(STATUS_INTERVAL)
                await self.refresh()
            except asyncio.CancelledError:
                return
            except Exception as exc:          # 單次失敗不該讓整條狀態列死掉
                log.warning(f"狀態列刷新失敗: {exc}")
                await asyncio.sleep(30)

    async def resume(self):
        """重啟後接續上一次的狀態列；沒有的話用已設定的頻道自動建一條。"""
        if not STATUS_AUTO_START:
            return
        try:
            if not self.channel_id:
                # 還沒有狀態列 → 拿第一個已設定的頻道直接建立，省得每次都要敲指令
                targets = list(self.client.config.channel_ids)
                if self.client.config.channel_id:
                    targets.append(self.client.config.channel_id)
                channel = self.client.get_channel(targets[0]) if targets else None
                if channel is None:
                    log.info("📊 尚未設定狀態列頻道，用 /statusboard start 指定一個")
                    return
                await self.start(channel)
                return
            await self.refresh()
            self._ensure_loop()
            log.info("📊 已接續上次的狀態列")
        except Exception as exc:
            log.warning(f"接續狀態列失敗: {exc}")

class DCListener(discord.Client):
    """Discord 端：把遊戲事件送到頻道，並提供管理指令。"""

    # 聯機大廳指令組：/lianji list|info|join|me|refresh
    lianji = app_commands.Group(name="lianji", description="聯機大廳（Miaaoo）")

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
        self._sync_at: Dict[int, List[float]] = {}  # guild_id → 最近 sync 時間（限流保護）
        self._cmd_sync_state: Dict[str, str] = {}  # guild_id(str) → 上次同步成功的指令簽名
        self._load_cmd_sync_state()
        self._empty_warned: Dict[int, float] = {}  # 空白訊息告警節流
        self._tp_kicked: Dict[str, float] = {}     # TP 玩家最近一次被踢的時間
        self._tp_announced: Dict[str, float] = {}  # TP 玩家最近一次公屏播報的時間
        self._tp_alert_at: Dict[str, float] = {}   # TP 提示節流（同一類別幾分鐘只發一次）
        self._deferred: deque = deque()            # 等遊戲 WebSocket 連上才送的指令
        self._deferred_task: Optional[asyncio.Task] = None
        self._dc_buffer: deque = deque()           # Discord 還沒就緒時暫存的訊息
        self._board_started = False                # 狀態列只接續一次（on_ready 可能重複觸發）
        self._seen_players: Set[str] = set()       # 本次執行以來加入過的人（狀態列的「已加入」）
        self._in_world: Optional[bool] = None      # 在不在世界裡（None=還沒判斷）
        self._list_miss: Dict[str, int] = {}       # list 對帳：連續幾次沒看到這個人
        self._announced_at: Dict[str, float] = {}  # 剛被 DLL 報過的人，list 不重複報
        self._suppress_until: float = 0.0          # 換房間後先靜默填名單，不刷「加入了遊戲」
        self._list_task: Optional[asyncio.Task] = None
        self._ext_ban_kicked: Dict[str, float] = {}  # 外部黑名單每秒掃描：最近一次踢這人的時間
        self._ext_ban_task: Optional[asyncio.Task] = None
        self.board = StatusBoard(self)             # 一行式狀態列
        self.ban_records = BanRecordsCounter()     # 外部封禁紀錄統計（狀態列顯示人數）
        self.ban_records.on_update = self._announce_ban_count
        self._ban_count_announced: Optional[int] = None  # 已打進遊戲公屏的數字（沒連線會重試）
        self._shutdown_requested = False           # 使用者主動要求下線（會清指令 + 不重啟）
        self._closing = False                      # close() 進來過就別再進來
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
            "!eat": self._cmd_eat,
        }

    # ------------------------------------------------ 生命週期
    async def setup_hook(self):
        self.tree.on_error = self.on_tree_error

        for cmd in (self.slash_status, self.slash_ping, self.slash_players,
                    self.slash_check, self.slash_cmd,
                    self.slash_kick, self.slash_ban, self.slash_unban, self.slash_banned,
                    self.slash_settings, self.slash_sync, self.slash_wl,
                    self.slash_shutdown,
                    self.slash_join, self.lianji, self.statusboard):
            self.tree.add_command(cmd)

        self._bind_commands()

        # 遊戲 WebSocket 沒連線時，指令排隊等它連上再送
        if self._deferred_task is None or self._deferred_task.done():
            self._deferred_task = asyncio.create_task(self._deferred_worker())

        # list 對帳：DLL 掃記憶體的加入事件在不同世界的記憶體布局下
        # 會抓不到或抓成碎片，`list` 是遊戲官方給的名單，名字一定是對的
        if self._list_task is None or self._list_task.done():
            self._list_task = asyncio.create_task(self._list_poll_loop())

        # 外部黑名單每秒掃描：list → 比對緩存的 acz.cc.cd 名單 → 命中就踢
        if EXTERNAL_BAN_SCAN and (self._ext_ban_task is None or self._ext_ban_task.done()):
            self._ext_ban_task = asyncio.create_task(self._ext_ban_poll_loop())

    def _bind_commands(self):
        """把每個指令綁到這個 bot 實例上。

        在類別裡用 `@app_commands.command` 定義時，裝飾器拿到的是**未綁定的函式**，
        discord.py 只會從 `callback.__self__` 取 binding，所以 `Command.binding` 是 None。
        執行時 `_do_call` 見 binding 為 None 就只傳 `interaction`，少傳 `self` →
        拋 TypeError；而 discord.py 會把「呼叫時參數不對」的 TypeError 誤判成
        CommandSignatureMismatch，看起來就像「指令沒同步」。

        結果就是：**所有斜杠指令都回「指令定義已更新，已自動重新同步」**。
        """
        def _bind(command):
            command.binding = self

        for command in self.tree.get_commands():
            if isinstance(command, app_commands.Group):
                for child in command.commands:
                    _bind(child)
            else:
                _bind(command)

    # ------------------------------------------------ 指令同步（只在變更時 PUT）
    def _command_signature(self) -> str:
        """算出目前指令樹的簽名（名字 + 描述 + 參數）。

        指令定義只有在改 bot.py 時才會變；簽名沒變就代表 Discord 上已是最新，
        重啟時可以跳過 sync，不去消耗 Bulk Overwrite 的限流額度（每 10 分鐘 2 次）。
        """
        def _sig(cmd) -> str:
            if isinstance(cmd, app_commands.Group):
                subs = ",".join(_sig(c) for c in sorted(cmd.commands, key=lambda c: c.name))
                return f"G:{cmd.name}:{cmd.description}:({subs})"
            try:
                opts = ",".join(f"{p.name}:{getattr(getattr(p, 'type', None), 'value', '')}"
                                for p in (cmd.parameters or []))
            except Exception:
                opts = ""
            return f"C:{cmd.name}:{cmd.description}:{opts}"

        parts = [_sig(c) for c in sorted(self.tree.get_commands(), key=lambda c: c.name)]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def _load_cmd_sync_state(self):
        data = _read_json(COMMAND_SYNC_FILE, None)
        if isinstance(data, dict) and isinstance(data.get("guilds"), dict):
            self._cmd_sync_state = {str(k): str(v) for k, v in data["guilds"].items()}

    def _save_cmd_sync_state(self):
        try:
            _atomic_write_json(COMMAND_SYNC_FILE, {"guilds": self._cmd_sync_state})
        except OSError as exc:
            log.error(f"儲存 command_sync.json 失敗: {exc}")

    async def _flush_dc_buffer(self):
        """把「還沒登入時收到的遊戲事件」補發到 Discord。"""
        if not self._dc_buffer:
            return
        pending = list(self._dc_buffer)
        self._dc_buffer.clear()
        sent = 0
        for created, username, content in pending:
            if time.monotonic() - created > DC_BUFFER_TTL:
                continue
            for cid in dict.fromkeys(self.config.channel_ids):
                channel = self.get_channel(cid)
                if not channel:
                    continue
                try:
                    await channel.send(content)
                    sent += 1
                except Exception as exc:
                    log.debug(f"補發到頻道 {cid} 失敗: {exc}")
        if sent:
            log.info(f"📮 已補發 {len(pending)} 則離線期間的遊戲事件到 Discord")

    async def on_disconnect(self):
        log.warning("⚠️ Discord 連線中斷，準備重連")

    async def on_resumed(self):
        log.info("✅ Discord 連線已恢復")

    async def on_ready(self):
        log.info(f"✅ Discord 已登入：{self.user}")
        # 「正在玩」狀態：顯示目前版本（Discord 資料卡上會看到「正在玩 目前版本：X.X.X」）
        try:
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Game(name=f"目前版本：{__version__}"),
            )
        except Exception as exc:
            log.warning(f"設定在線狀態失敗: {exc}")
        if not self._session:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": f"DiscordBot (mc-bridge {__version__})"},
            )
        # 把「登入前」收到的遊戲事件補發出去
        await self._flush_dc_buffer()

        # 只同步還沒同步過的伺服器：重連時 on_ready 會再觸發一次，
        # 每次都全量 sync 很容易撞上 Discord 的 rate limit。
        pending = [g for g in self.guilds if g.id not in self._synced_guilds]
        if pending:
            await self._sync_guild_commands(pending)

        # 重啟後接續上次的狀態列（訊息還在就接著改，被刪了就重建）
        if not self._board_started:
            self._board_started = True
            await self.board.resume()

    async def request_shutdown(self) -> None:
        """主動下線：先移除 Discord 上註冊的斜線指令，再斷線。"""
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        log.info("🛑 收到關閉請求，準備移除斜線指令後下線")
        await self.close()

    async def close(self):
        if self._closing:
            return
        self._closing = True

        # 狀態列留在頻道裡（下次啟動會接著改它），只停掉刷新迴圈
        try:
            await self.board.stop(delete_message=False)
        except Exception as exc:
            log.warning(f"停止狀態列失敗: {exc}")

        if self._list_task and not self._list_task.done():
            self._list_task.cancel()
            try:
                await self._list_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._ext_ban_task and not self._ext_ban_task.done():
            self._ext_ban_task.cancel()
            try:
                await self._ext_ban_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            await self.ban_records.close()
        except Exception:
            pass

        if self._deferred_task and not self._deferred_task.done():
            self._deferred_task.cancel()
            try:
                await self._deferred_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._session:
            await self._session.close()
            self._session = None
        await super().close()

    def _sync_allowed(self, guild_id: int) -> bool:
        """Discord 對 guild 指令覆蓋限流：每 10 分鐘只允許 2 次。

        超過就回 False。這是「指令一同步就消失」的元兇——關閉 bot 清一次、
        啟動註冊一次、再手動 !sync 一次，三連發全撞 429，指令被清空後又
        註冊不回來。這裡先把額度管起來，寧可跳過也不要撞限流。
        """
        now = time.monotonic()
        recent = [t for t in self._sync_at.get(guild_id, []) if now - t < 600]
        self._sync_at[guild_id] = recent
        return len(recent) < 2

    def _mark_synced(self, guild_id: int):
        self._sync_at.setdefault(guild_id, []).append(time.monotonic())

    async def _resync_after_limit(self):
        """限流窗口（10 分鐘）過後自動補一次同步，把指令註冊回去。"""
        await asyncio.sleep(600)
        log.info("🔄 Discord 指令限流窗口已過，自動重新同步")
        self._synced_guilds.clear()
        try:
            await self._sync_guild_commands()
        except Exception as exc:
            log.warning(f"自動重新同步失敗: {exc}")

    async def _sync_guild_commands(self, guilds=None, force: bool = False) -> Tuple[int, int]:
        """把本地指令樹同步到伺服器。回傳 (已同步數, 限流跳過數)。

        關鍵：同步前先清空該伺服器的指令樹，否則舊版殘留（改了參數的 /status、
        已刪掉的 /join…）會留在 Discord 上，使用者一按就 CommandSignatureMismatch。

        省限流：指令結構沒變（簽名相同）就完全不 PUT，只有簽名變了或 force
        （手動 `!sync` / 簽名不符自動重同步）才真的去覆寫。
        """
        synced = skipped = 0
        signature = self._command_signature()
        for guild in (guilds if guilds is not None else self.guilds):
            obj = discord.Object(id=guild.id)
            gid = str(guild.id)

            # 指令沒變、也同步過了 → 跳過，不 PUT（避免每次重啟都撞限流）
            if not force and self._cmd_sync_state.get(gid) == signature:
                self._synced_guilds.add(guild.id)
                continue

            if not self._sync_allowed(guild.id):
                skipped += 1
                log.warning(f"⏳ {guild.name} 的指令同步被 Discord 限流，跳過本次")
                continue
            names = ""
            for attempt in range(1, 4):
                try:
                    self.tree.clear_commands(guild=obj)
                    self.tree.copy_global_to(guild=obj)
                    synced_cmds = await self.tree.sync(guild=obj)
                    names = ", ".join(sorted(f"/{c.name}" for c in synced_cmds))
                    self._synced_guilds.add(guild.id)
                    self._mark_synced(guild.id)
                    self._cmd_sync_state[gid] = signature
                    self._save_cmd_sync_state()
                    log.info(f"🧹 已同步 {guild.name} 的 {len(synced_cmds)} 個指令: {names}")
                    synced += 1
                    break
                except Exception as exc:
                    log.warning(f"同步 {guild.name} 失敗（第 {attempt} 次）: {exc}")
                    if attempt < 3:
                        await asyncio.sleep(2 * attempt)
            else:
                log.error(f"❌ {guild.name} 的斜線指令同步失敗，Discord 上可能還是舊的")

        if skipped and not synced:
            _spawn(self._resync_after_limit())
        return synced, skipped

    async def on_tree_error(self, interaction: discord.Interaction, error: Exception):
        """指令簽名不符時自動重新同步，而不是只丟一大段 traceback。"""
        if isinstance(error, app_commands.CommandSignatureMismatch):
            cmd_name = getattr(interaction.command, "name", "?")
            log.warning(f"⚠️ /{cmd_name} 的簽名與 Discord 不符，自動重新同步")
            self._synced_guilds.clear()
            targets = [interaction.guild] if interaction.guild else None
            await self._sync_guild_commands(targets, force=True)
            text = ("🔄 **指令定義已更新，已自動重新同步**\n"
                    "請再試一次；如果還是不行，重開 Discord 用戶端（Ctrl+R）讓它刷新指令快取。")
        elif isinstance(error, app_commands.CheckFailure):
            text = "❌ 你沒有權限使用這個指令"
        else:
            log.error(f"斜線指令 /{getattr(interaction.command, 'name', '?')} 執行失敗: {error!r}")
            text = f"❌ 執行失敗：{error}"

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(text, ephemeral=True)
            else:
                await interaction.followup.send(text, ephemeral=True)
        except Exception:
            pass

    # ------------------------------------------------ Discord → 遊戲
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.webhook_id is not None:
            return

        content = message.content.strip()
        if not content:
            self._warn_empty_content(message)
            return

        # `.eat @玩家` / `!eat @玩家`：把被 @ 的玩家在遊戲裡 kill 掉（權限在 _cmd_eat 內檢查）
        if content.lower().startswith(".eat"):
            await self._cmd_eat(message)
            return

        # 指令不受頻道白名單限制（權限在各指令內部檢查），避免「打了沒反應」
        if content.startswith(COMMAND_PREFIX):
            await self._handle_command(message)
            return

        if not self._channel_allowed(message):
            return

        if feature_state("dcmc") and self.mc.connected:
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
        if str(getattr(author, "id", "")) in users:
            return True
        # 名字也接受（白名單裡可能是 ID，也可能是名字）
        names = {str(getattr(author, "name", "") or ""),
                 str(getattr(author, "display_name", "") or "")}
        names.discard("")
        return bool(names & set(users))

    @staticmethod
    def _is_owner(user) -> bool:
        """是不是 bot 擁有者（.env 的 OWNER_ID）。"""
        if not OWNER_ID:
            return False
        return OWNER_ID in {str(getattr(user, "name", "") or ""),
                            str(getattr(user, "display_name", "") or "")}

    def _require_admin(self, message: discord.Message) -> bool:
        """管理員、白名單使用者、或 bot 擁有者。

        以前只認伺服器管理員，導致在「自己不是管理員」的伺服器上
        （例如別人的群）連 !sync / !wl 都會被擋成「需要管理員權限」。
        """
        perms = getattr(getattr(message.author, "guild_permissions", None), "administrator", False)
        if perms:
            return True
        return self._authorized(message.author) or self._is_owner(message.author)

    # ------------------------------------------------ 遊戲 → Discord
    async def _send_to_discord(self, username: str, content: str):
        text = _clip(content)

        # 還沒登入完成（重啟後那幾秒）時，get_channel() 一律回 None，
        # 以前就是這樣把「有人加入 / 有人說話」整批丟掉的。先存起來，登入後補發。
        if not self.is_ready():
            self._dc_buffer.append((time.monotonic(), username, text))
            while len(self._dc_buffer) > DC_BUFFER_MAX:
                self._dc_buffer.popleft()
            log.debug(f"Discord 尚未就緒，先暫存: {text[:40]}")
            return

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
        self._seen_players.add(player)          # 「已加入伺服器」是累計，離開也不減
        if feature_state("join"):
            await self._send_to_discord("伺服器", f"✅ **{player}** 加入了遊戲")
        await self.on_player_joined(player)

    async def on_mc_leave(self, player: str):
        if feature_state("join"):
            await self._send_to_discord("伺服器", f"❌ **{player}** 離開了遊戲")

    # ------------------------------------------------ list 對帳
    async def _list_poll_loop(self):
        """每 LIST_POLL_SECONDS 秒問一次 `list`，用官方名單校正加入/離開。

        DLL 掃記憶體的加入事件，在某些世界（尤其進到別人的房間）的記憶體
        布局下會抓不到、或抓成 "gZs" / "加入gI Ҹ" 這種碎片；`list` 的回覆
        是遊戲本體給的，名字一定是對的。DLL 先報的就不再重報，DLL 漏掉的
        這裡補報，名單裡消失的補報離開。
        """
        last_fail_note = 0.0
        while True:
            try:
                await asyncio.sleep(LIST_POLL_SECONDS)
                if not self.mc.game_clients():
                    continue
                raw = await self.mc.send_command("list", timeout=8)
                text = self._clean_result(raw or "")
                if text and looks_like_no_world(text):
                    # 房間關了：清掉殘留名單，狀態列改成「未進入世界」
                    if self._in_world is not False:
                        self._in_world = False
                        self.mc.players.clear()
                        self._list_miss.clear()
                        log.info("🏠 遊戲回報「未知的命令」→ 已離開世界（房間已關閉）")
                    continue
                if text:
                    if self._in_world is not True:
                        self._in_world = True
                        log.info("🏠 偵測到有效的 list 回覆 → 已進入世界")
                    # 順手把「上限」更新成遊戲回報的值（list 回覆像 2/8、2 of a max of 8）
                    _online_n, cap = parse_list_counts(text)
                    if cap and cap != self.board._max_players:
                        self.board._max_players = cap
                        log.info(f"📊 list 回報上限更新：{cap}")
                names = [n for n in extract_players(text) if looks_like_player_name(n)]
                if not names:
                    # 沒回顯（非房主 / 指令被擋 / 世界剛載入）→ 10 分鐘提醒一次
                    now = time.monotonic()
                    if now - last_fail_note > 600:
                        last_fail_note = now
                        log.warning("⚠️ list 輪詢沒有回顯，加入/離開校正暫時停用"
                                    "（進到別人的房間時常見）")
                    continue
                await self._reconcile_players(names)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug(f"list 輪詢失敗: {exc}")

    async def _ext_ban_poll_loop(self):
        """每 EXTERNAL_BAN_SCAN_SECONDS 秒問一次 `list`，用緩存的 acz.cc.cd 黑名單比對在線玩家。

        DLL 掃記憶體在多人世界常抓不到加入事件，玩家會「漏報加入」，
        自然也就不會觸發 on_player_joined 的外部黑名單檢查。這個循環用
        `list`（遊戲官方回覆）當在線名單，命中 acz.cc.cd 黑名單就直接踢，
        不依賴 DLL 的任何事件。黑名單本身走 cached_names() 的快取，
        不會每秒都打一次外部 API（acz.cc.cd 限流很兇）。
        """
        last_fail_note = 0.0
        while True:
            try:
                await asyncio.sleep(EXTERNAL_BAN_SCAN_SECONDS)
                if not ENFORCE_EXTERNAL_BAN_ON_JOIN:
                    continue
                if not self.mc.game_clients():
                    continue

                # 快取的黑名單（小寫名 → 紀錄）；沒配 API 或名單為空就略過
                banned = await self.ban_records.cached_names()
                if not banned:
                    continue

                raw = await self.mc.send_command("list", timeout=6)
                text = self._clean_result(raw or "")
                if not text or looks_like_no_world(text):
                    continue
                names = [n for n in extract_players(text) if looks_like_player_name(n)]
                if not names:
                    now = time.monotonic()
                    if now - last_fail_note > 600:
                        last_fail_note = now
                        log.warning("⚠️ 外部黑名單掃描：list 沒有回顯，略過（進別人房間時常見）")
                    continue

                now = time.monotonic()
                for name in names:
                    rec = banned.get(name.lower())
                    if not rec:
                        continue
                    if is_platform_inviter(name):
                        continue
                    # 冷卻：同一人幾秒內不重複踢（list 每秒都在跑，別刷指令）
                    last = self._ext_ban_kicked.get(name.lower(), 0.0)
                    if now - last < EXTERNAL_BAN_SCAN_COOLDOWN:
                        continue
                    self._ext_ban_kicked[name.lower()] = now
                    reason = str(rec.get("reason") or "").strip() or "外部封禁名單"
                    result = await self.kick_player(name, reason,
                                                    use_selector=EXTERNAL_BAN_KICK_SELECTOR)
                    await self._send_to_discord(
                        "防禦系統",
                        f"👢 **外部封禁攔截（list 掃描）**\n玩家: **{name}**\n理由: {reason}\n指令結果: `{result}`",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug(f"外部黑名單掃描失敗: {exc}")

    async def _reconcile_players(self, names: List[str]):
        online = set(names)
        now = time.monotonic()
        suppressed = now < self._suppress_until

        # 補報加入：DLL 沒報到的（或換房後靜默期）在這裡補
        for name in names:
            if name in self.mc.players:
                continue
            self.mc.players.add(name)
            self._list_miss.pop(name, None)
            if suppressed or now - self._announced_at.get(name.lower(), 0) < 90:
                continue                      # 剛報過 / 換房靜默期，只補名單不發訊息
            self._announced_at[name.lower()] = now
            log.info(f"➕ {name} 加入（list 校正）")
            if feature_state("join"):
                await self.on_mc_join(name)

        # 補報離開：連續 2 次不在名單才算（list 偶爾會抖一下）
        for name in list(self.mc.players):
            if name in online:
                self._list_miss.pop(name, None)
                continue
            miss = self._list_miss.get(name, 0) + 1
            self._list_miss[name] = miss
            if miss < 2:
                continue
            self._list_miss.pop(name, None)
            self.mc.players.discard(name)
            log.info(f"➖ {name} 離開（list 校正）")
            if feature_state("join"):
                await self.on_mc_leave(name)

        if len(self._announced_at) > 300:
            self._announced_at.clear()

    async def on_room_change(self, rejoined: bool):
        """自己加入/離開＝換房間：所有「跟著房間走」的統計重置。

        房主不是固定的 —— 進哪個房間，狀態列的數字就該是哪個房間的。
        在線名單在 mc 那邊已清空，這裡把 bot 端的累計與節流狀態一併清掉，
        並把運行時間的起點改成本次進房時間，再重新問一次 list。
        """
        self._seen_players.clear()
        self._tp_kicked.clear()
        self._tp_announced.clear()
        self._tp_alert_at.clear()
        self._ext_ban_kicked.clear()
        self.board.mark_room_switch()
        # 換房後第一輪 list 對帳先「靜默」填名單，不然新房間裡既有的人
        # 會被整批公告成「加入了遊戲」
        self._suppress_until = time.monotonic() + 50
        self._list_miss.clear()
        self._ban_count_announced = None   # 新房間再播一次封禁人數
        log.info("🏠 已切換房間：在線 / 已加入 / 運行時間統計重置")
        try:
            await self._send_to_discord(
                "伺服器", "🏠 **已切換房間**，在線人數 / 已加入 / 運行時間統計已重置")
        except Exception:
            pass
        if rejoined:
            async def _reseed_later():
                await asyncio.sleep(4)          # 等遊戲安頓，list 才查得到新房間的人
                try:
                    await self.board.reseed()
                except Exception as exc:
                    log.debug(f"換房間 reseed 失敗: {exc}")
            _spawn(_reseed_later())

    async def on_mc_death(self, player: str, message: str):
        text = message.strip() or f"**{player}** 死亡了"
        await self._send_to_discord("伺服器", f"💀 {text}")

    async def on_mc_ban(self, text: str):
        if feature_state("ban"):
            await self._send_to_discord("防禦系統", f"🔨 **封禁事件**\n`{_clip(text, 500)}`")

    async def on_mc_tp(self, text: str):
        # 外掛橫幅在 _dispatch 已經濾掉了，這裡再擋一次（舊 DLL 可能還在送）
        if is_tp_banner(text):
            return

        # 去重鍵：把括號裡的網域/雜訊去掉，同一類提示只發一次
        key = re.sub(r"[\(（].*?[\)）]", "", text).strip().lower()[:40]
        now = time.monotonic()
        if now - self._tp_alert_at.get(key, 0) < TP_ALERT_COOLDOWN:
            log.debug(f"TP 提示在冷卻期內，略過: {text[:60]}")
            return
        self._tp_alert_at[key] = now
        if len(self._tp_alert_at) > 50:
            self._tp_alert_at = {key: now}

        match = re.search(r"(?:名稱|名称|xuid|玩家)\s*[:：]?\s*([A-Za-z0-9_]{2,16})", text)
        lines = ["🛑 **偵測到 TP 平台特徵**"]
        if match:
            lines.append(f"目標：**{match.group(1).strip()}**")
        lines.append(f"內容：`{_clip(text, 500)}`")
        if ONE_WAY_MODE:
            lines.append("（單向模式：未執行 kick）")
        await self._send_to_discord("防禦系統", "\n".join(lines))

    async def _announce_ban_count(self, count: int):
        """封禁人數更新時打一次到遊戲公屏（同一個數字只發一次；沒連上遊戲會重試）。"""
        if not count or count == self._ban_count_announced:
            return
        text = f"§b[封禁系統] §f目前共封禁 §c{count} §f名玩家"
        try:
            ok = await self.mc.tellraw_players(text)
        except Exception as exc:
            log.debug(f"封禁人數公屏播報失敗: {exc}")
            return
        if ok:
            self._ban_count_announced = count
            log.info(f"📢 已在遊戲公屏播報封禁人數: {count}")
        # ok=False（遊戲沒連線）→ 不標記，下次拿到數字再試

    async def _announce_tp(self, player: str) -> None:
        """把「玩家嘗試加入」打在遊戲公屏上（讓遊戲裡的人也看得到）。"""
        text = TP_ANNOUNCE_TEXT.replace("{player}", player)
        try:
            ok = await self.mc.tellraw_players(text)
        except Exception as exc:
            log.warning(f"公屏播報失敗: {exc}")
            return
        if ok:
            log.info(f"📢 已在公屏播報: {player}")
        else:
            log.info(f"📢 遊戲 WebSocket 未連線，略過公屏播報: {player}")

    async def on_mc_tp_join(self, player: str):
        """偵測到 TP 平台拉人：報告 + 直接踢出（TP_KICK 可關）。"""
        if is_platform_inviter(player):
            log.info(f"ℹ️ {player} 是 miaaoo 邀請帳號（不是 TP 入侵者），略過")
            return
        if not valid_player_name(player):
            log.warning(f"🚫 TP 事件的名字不像玩家名，直接忽略: {player[:32]!r}")
            return

        now = time.monotonic()

        # 公屏播報：同一人 8 秒內只播一次（舊 DLL 可能重複送事件）
        if feature_state("tpann"):
            if now - self._tp_announced.get(player.lower(), 0.0) >= 8:
                self._tp_announced[player.lower()] = now
                _spawn(self._announce_tp(player))

        last = self._tp_kicked.get(player.lower(), 0)
        if now - last < TP_KICK_COOLDOWN:
            log.debug(f"TP 玩家 {player} 在冷卻期內，略過重複處理")
            return

        log.warning(f"🚨 TP 平台加入者: {player}")
        rec = self.blacklist.get(player)

        if feature_state("tpkick") and TP_AUTO_BLACKLIST and not rec:
            rec = self.blacklist.ban(player, "TP 平台加入", TP_BAN_MINUTES, "auto")
            log.info(f"📝 已自動寫入封禁名單: {player}")

        status = f"已在封禁名單（{rec.get('reason')}）" if rec else "不在封禁名單"
        lines = [f"🛑 **偵測到 TP 平台拉人**", f"玩家: **{player}**", status]

        if feature_state("tpkick"):
            reason = TP_KICK_REASON or (rec or {}).get("reason") or "TP 平台加入"
            result = await self.kick_player(player, reason)
            self._tp_kicked[player.lower()] = now
            lines.append(f"👢 已送出踢出指令（{reason}）")
            lines.append(f"結果: `{result}`")
        else:
            lines.append("（TP 自動踢出已關閉，只提示不踢）")
            lines.append(f"要處理請用 `!ban {player}`")

        await self._send_to_discord("防禦系統", "\n".join(lines))

    async def on_player_joined(self, player: str):
        """有人加入時檢查封禁名單（本地 blacklist.json + 外部 BAN_RECORDS_API_BASE）。"""
        if is_platform_inviter(player):
            log.info(f"ℹ️ {player} 是 miaaoo 邀請帳號，不做封禁處理")
            return

        # 1) 本地封禁名單
        self.blacklist.load()
        rec = self.blacklist.get(player)
        if rec:
            if not ENFORCE_BAN_ON_JOIN:
                await self._send_to_discord(
                    "防禦系統", f"⚠️ **封禁名單玩家上線**\n玩家: **{player}**\n（未開啟自動踢）")
                return
            result = await self.kick_player(player, rec.get("reason", "黑名單"))
            await self._send_to_discord(
                "防禦系統",
                f"👢 **封禁攔截**\n玩家: **{player}**\n理由: {rec.get('reason')}\n指令結果: `{result}`",
            )
            return

        # 2) 外部封禁名單（BAN_RECORDS_API_BASE）：命中直接踢
        if not ENFORCE_EXTERNAL_BAN_ON_JOIN:
            return
        ext = await self.ban_records.lookup(player)
        if not ext:
            return
        reason = str(ext.get("reason") or "").strip() or "外部封禁名單"
        result = await self.kick_player(player, reason)
        await self._send_to_discord(
            "防禦系統",
            f"👢 **外部封禁攔截**\n玩家: **{player}**\n理由: {reason}\n指令結果: `{result}`",
        )

    async def on_bridge_connected(self):
        log.info("🟢 橋接程式已連線")

    async def on_bridge_disconnected(self):
        log.info("🔴 橋接程式已斷線")

    # ------------------------------------------------ 踢人
    async def send_game_command(self, cmd: str, label: str = "", timeout: float = 8.0,
                                player: str = "") -> str:
        """送指令給遊戲。WebSocket 沒連線就排隊，等連上再送。

        以前沒連線時會退回「鍵盤注入」，但那需要遊戲在前景、而且只保證按鍵打出去，
        既不確定成功又會誤導；現在一律排隊，連上後自動補送。
        """
        label = label or cmd

        if self.mc.game_clients():
            result = await self.mc.send_command(cmd, timeout=timeout)
            log.info(f"🎮 {label} → {result or '（無回應）'}")
            return result or "（無回應）"

        if len(self._deferred) >= KICK_QUEUE_MAX:
            log.warning(f"⏳ 指令佇列已滿（{KICK_QUEUE_MAX}），丟棄: {label}")
            return "（佇列已滿，未執行）"

        self._deferred.append(DeferredCmd(cmd=cmd, label=label,
                                          created=time.monotonic(), player=player))
        log.warning(f"⏳ 遊戲 WebSocket 未連線，已排隊 #{len(self._deferred)}: {label}")
        return "⏳ 遊戲 WebSocket 未連線，已排入佇列，連上後自動執行"

    async def kick_player(self, player: str, reason: str = "黑名單", use_selector: bool = False) -> str:
        if not ENABLE_COMMAND:
            log.warning("ENABLE_COMMAND=false，略過 kick")
            return "（指令傳送已停用）"
        if is_platform_inviter(player):
            log.info(f"ℹ️ {player} 是 miaaoo 邀請帳號，不執行踢出")
            return "（miaaoo 邀請帳號，已跳過）"
        # selector 模式用「寬鬆」名字校驗：基岩版 gamertag 允許空格（如 "Amo 0704"），
        # 而 @a[name="..."] 能精確匹配含空格的名字；反之 kick "名字" 用嚴格校驗（純英數底線）。
        name_ok = looks_like_player_name(player) if use_selector else valid_player_name(player)
        if not name_ok:
            log.warning(f"🚫 拒絕踢出不合法的名字: {player[:32]!r}")
            return "（名字不像玩家，已忽略）"
        if use_selector:
            # 用目標選擇器踢：kick @a[name="玩家名"]。比 kick "名字" 更精準，
            # 玩家名含特殊字元時不會被遊戲誤解析。
            cmd = f'kick @a[name="{player}"] §c{reason}'
        else:
            cmd = f'kick "{player}" §c{reason}'
        return await self.send_game_command(cmd, f"踢出 {player}（{reason}）", player=player)

    async def kill_player(self, player: str) -> str:
        """在遊戲裡 kill 一個玩家（`!eat @玩家` 用的）。

        創造模式下 `/kill` 可能無效，所以先 `gamemode default` 把它切回
        預設（生存）模式，再 kill。
        """
        if not ENABLE_COMMAND:
            log.warning("ENABLE_COMMAND=false，略過 kill")
            return "（指令傳送已停用）"
        if is_platform_inviter(player):
            log.info(f"ℹ️ {player} 是 miaaoo 邀請帳號，不執行 kill")
            return "（miaaoo 邀請帳號，已跳過）"
        if not valid_player_name(player):
            log.warning(f"🚫 拒絕 kill 不合法的名字: {player[:32]!r}")
            return "（名字不像玩家，已忽略）"
        # 先切回預設模式（無視創造模式），再 kill
        gm = await self.send_game_command(
            f'gamemode default "{player}"', f"切回預設模式 {player}", player=player)
        cmd = f'kill "{player}"'
        result = await self.send_game_command(cmd, f"kill {player}", player=player)
        return result

    async def _deferred_worker(self):
        """等遊戲 WebSocket 連上後，依序補送排隊的指令。"""
        while True:
            try:
                if not self._deferred:
                    await asyncio.sleep(2)
                    continue

                now = time.monotonic()
                # 先清掉放太久的：遊戲一直沒連上就不該在幾小時後突然踢人
                while self._deferred and now - self._deferred[0].created > KICK_QUEUE_TTL:
                    item = self._deferred.popleft()
                    log.warning(f"⌛ 排隊逾時（{KICK_QUEUE_TTL}s）作廢: {item.label}")
                    await self._send_to_discord(
                        "防禦系統",
                        f"⌛ **排隊指令已作廢**\n{item.label}\n"
                        f"（遊戲 {KICK_QUEUE_TTL // 60} 分鐘內都沒連上）",
                    )

                if not self._deferred or not self.mc.game_clients():
                    await asyncio.sleep(2)
                    continue

                item = self._deferred.popleft()

                # 補送前先確認這個人還在線上：離開了的玩家會讓遊戲回
                # 「沒有與選擇器匹配的目標」，看起來像 bot 在踢不存在的人
                if item.player and self.mc.players:
                    online = {p.lower() for p in self.mc.players}
                    if item.player.lower() not in online:
                        log.info(f"⌛ {item.player} 已不在線上，作廢排隊指令")
                        await self._send_to_discord(
                            "防禦系統",
                            f"⌛ **排隊指令已作廢**\n{item.label}\n"
                            f"（{item.player} 已經不在遊戲裡了）",
                        )
                        continue

                waited = int(now - item.created)
                result = await self.mc.send_command(item.cmd, timeout=8)
                log.info(f"🎮 補送排隊指令（等了 {waited}s）{item.label} → {result or '（無回應）'}")
                await self._send_to_discord(
                    "防禦系統",
                    f"▶️ **排隊指令已執行**\n{item.label}\n"
                    f"等待 {waited} 秒\n結果: `{result or '（無回應）'}`",
                )
                await asyncio.sleep(0.5)      # 別把指令連續塞進遊戲
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"排隊指令處理失敗: {exc}")
                await asyncio.sleep(5)

    async def on_kick_result(self, text: str) -> None:
        """DLL 回報的執行結果（僅在使用鍵盤注入時才會有）。"""
        status, _, detail = text.partition("|")
        if status == "ok":
            log.info(f"✅ 遊戲內已執行（{detail}）")
            return
        log.warning(f"⚠️ 遊戲回報失敗：{detail}")
        await self._send_to_discord("防禦系統", f"⚠️ **指令沒有成功**\n原因: `{detail}`")

    # ------------------------------------------------ 斜線指令：共用
    def _may_manage(self, interaction: discord.Interaction) -> bool:
        """管理員，或白名單使用者，或 bot 擁有者。"""
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and perms.administrator:
            return True
        return self._authorized(interaction.user) or self._is_owner(interaction.user)

    def _is_guild_admin(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    @staticmethod
    async def _deny(interaction: discord.Interaction,
                    text: str = "❌ 你沒有權限（需要管理員或在白名單內）"):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass

    def _status_embed(self) -> discord.Embed:
        game_online = bool(self.mc.game_clients())
        embed = discord.Embed(
            title="🛰️ 橋接狀態",
            color=0x22C55E if game_online else 0xEF4444,
        )
        embed.add_field(
            name="遊戲 WebSocket",
            value="🟢 已連線" if game_online else "🔴 未連線（指令會排隊等連上）",
            inline=False,
        )
        embed.add_field(name="橋接器", value="🟢 已連線" if self.mc.connected else "🔴 未連線", inline=True)
        embed.add_field(name="線上玩家", value=f"{len(self.mc.players)} 人", inline=True)
        embed.add_field(name="待送指令", value=f"{len(self._deferred)} 筆", inline=True)
        embed.add_field(name="模式", value="單向（遊戲→Discord）" if ONE_WAY_MODE else "雙向", inline=True)
        embed.add_field(name="監聽", value=f"`{MC_HOST}:{MC_PORT}`", inline=True)
        embed.add_field(name="版本", value=f"v{__version__}", inline=True)
        if self.mc.players:
            names = "、".join(sorted(self.mc.players)[:15])
            embed.add_field(name="玩家名單", value=_clip(names, 1000), inline=False)
        return embed

    # ------------------------------------------------ 斜線指令：autocomplete
    async def _room_choices(self, current: str) -> List[app_commands.Choice[str]]:
        """房間自動補全：用快取，過期才去打 API（打字不該刷爆介面）。"""
        try:
            rooms = await self.miaaoo.fetch_rooms_if_stale()
        except Exception as exc:
            log.warning(f"房間補全失敗: {exc}")
            return []
        needle = (current or "").strip().lower()
        out: List[app_commands.Choice[str]] = []
        for room in rooms:
            haystack = f"{room.world} {room.host} {room.id}".lower()
            if needle and needle not in haystack:
                continue
            out.append(app_commands.Choice(name=room.label(), value=room.id))
            if len(out) >= 25:
                break
        return out

    async def room_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._room_choices(current)

    async def player_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = (current or "").strip().lower()
        names = sorted(self.mc.players)
        return [app_commands.Choice(name=n, value=n)
                for n in names if needle in n.lower()][:25]

    async def _find_room(self, room_id: str) -> Optional[Room]:
        room_id = (room_id or "").strip()
        if not room_id:
            return None
        for room in self.miaaoo.cached_rooms():
            if room.id == room_id:
                return room
        rooms = await self.miaaoo.list_rooms()
        return next((r for r in rooms if r.id == room_id), None)

    # ------------------------------------------------ 斜線指令：基本
    @app_commands.command(name="status", description="橋接與機器人狀態")
    async def slash_status(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self._status_embed(), ephemeral=True)

    @app_commands.command(name="ping", description="測試機器人是否在線")
    async def slash_ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("🏓 Pong！機器人運作正常！", ephemeral=True)

    @app_commands.command(name="players", description="列出線上玩家")
    async def slash_players(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(thinking=True)
        names: List[str] = []
        source = ""
        # 先問遊戲 `list`；拿不到有效回顯就退回事件快取（加入/離開事件累積的名單）
        if self.mc.game_clients():
            raw = self._clean_result(await self.mc.send_command("list", timeout=6))
            names = extract_players(raw)
            if names:
                source = "遊戲回報（list）"
        if not names:
            names = sorted(self.mc.players)
            if names:
                source = "事件快取（PlayerJoin / PlayerLeave）"

        embed = discord.Embed(title=f"👥 線上玩家（{len(names)}）", color=0x3B82F6)
        embed.description = _clip("、".join(names), 1900) if names else "📋 目前沒有偵測到玩家在線"
        if source:
            embed.set_footer(text=f"來源：{source}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="check", description="檢查伺服器狀態（時間 / 天氣 / 種子）")
    async def slash_check(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(thinking=True)
        if not self.mc.game_clients():
            embed = discord.Embed(title="🔴 遊戲 WebSocket 未連線",
                                  description="遊戲端沒有連上橋接器，查不到時間/天氣。",
                                  color=0xEF4444)
            embed.add_field(name="橋接器", value="🟢 已連線" if self.mc.connected else "🔴 未連線")
            await interaction.followup.send(embed=embed)
            return

        results = await asyncio.gather(
            self.mc.send_command("list", timeout=10),
            self.mc.send_command("time query daytime", timeout=10),
            self.mc.send_command("weather query", timeout=10),
            self.mc.send_command("seed", timeout=10),
            return_exceptions=True,
        )
        list_raw, time_raw, weather_raw, seed_raw = (self._clean_result(r) for r in results)
        players = extract_players(list_raw)

        # 房間關掉以後，MC-WS 的 WebSocket 還連著（它不管你在不在世界裡），但遊戲
        # 會把每一個指令都用「未知的命令」打回來 —— 用這個判斷在不在世界裡，
        # 比看連線狀態準得多。
        no_world = any(looks_like_no_world(t)
                       for t in (list_raw, time_raw, weather_raw, seed_raw) if t)
        if no_world:
            self._in_world = False
            embed = discord.Embed(
                title="🔴 遊戲世界未就緒",
                description="遊戲對 `list` / `time` / `weather` / `seed` 全回「未知的命令」。\n"
                            "通常是**房間已經關掉**或**還沒進世界**；如果你人在世界裡，"
                            "那就是那個世界沒開「允許指令」。",
                color=0xEF4444)
            embed.add_field(name="🔌 橋接器", value="🟢 已連線（MC-WS 還在跑）", inline=False)
            embed.add_field(name="🌍 遊戲世界", value="🔴 未進入", inline=False)
            await interaction.followup.send(embed=embed)
            return
        self._in_world = True

        embed = discord.Embed(title="📊 伺服器狀態", color=0x3B82F6)
        embed.add_field(name="👥 線上", value=f"{len(players)} 人", inline=True)
        embed.add_field(name="👑 房主", value=OWNER_ID, inline=True)
        embed.add_field(name="🎮 連線", value="🟢" if self.mc.game_clients() else "🔴", inline=True)
        if time_raw:
            embed.add_field(name="⏰ 時間", value=_clip(time_raw, 500), inline=False)
        if weather_raw:
            embed.add_field(name="🌤️ 天氣", value=_clip(weather_raw, 500), inline=False)
        if seed_raw:
            embed.add_field(name="🌍 種子", value=_clip(seed_raw, 500), inline=False)
        if players:
            embed.add_field(name="玩家", value=_clip("、".join(players), 1000), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="cmd", description="在遊戲內執行 Minecraft 指令")
    @app_commands.describe(command="不用打斜線，例如 time set day")
    async def slash_cmd(self, interaction: discord.Interaction, command: str):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        cmd = normalize_cmd(command)
        token = first_token(cmd)
        if token in COMMAND_BLACKLIST:
            await interaction.response.send_message(
                f"🚫 `{token}` 在禁用清單（.env 的 COMMAND_BLACKLIST）", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        result = await self.send_game_command(cmd, f"/cmd {cmd}", timeout=15)
        if result.startswith("⏳"):
            await interaction.followup.send(result)
            return
        embed = discord.Embed(title=f"📋 /{_clip(cmd, 200)}", color=0x3B82F6)
        embed.description = _clip(result, 1900) or "（沒有回應）"
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------ 斜線指令：封禁 / 踢人
    @app_commands.command(name="kick", description="踢出玩家（不加入封禁名單）")
    @app_commands.describe(player="玩家名稱", reason="踢出理由")
    @app_commands.autocomplete(player=player_autocomplete)
    async def slash_kick(self, interaction: discord.Interaction, player: str,
                         reason: str = "由管理員踢出"):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(thinking=True)
        result = await self.kick_player(player, reason)
        queued = result.startswith("⏳")
        embed = discord.Embed(
            title=f"{'⏳' if queued else '👢'} 踢出 {player}",
            color=0xF59E0B if queued else 0x3B82F6,
        )
        embed.add_field(name="理由", value=_clip(reason, 500), inline=False)
        embed.add_field(name="結果", value=_clip(result, 1000), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="ban", description="封禁玩家（寫入名單並踢出）")
    @app_commands.describe(player="玩家名稱", minutes="封禁分鐘數，0 或留空 = 永久",
                           reason="封禁理由")
    @app_commands.autocomplete(player=player_autocomplete)
    async def slash_ban(self, interaction: discord.Interaction, player: str,
                        minutes: int = 0, reason: str = ""):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(thinking=True)
        rec = self.blacklist.ban(player, reason, max(0, minutes), str(interaction.user))
        result = await self.kick_player(player, rec["reason"])
        queued = result.startswith("⏳")

        embed = discord.Embed(
            title=f"{'⏳' if queued else '🔨'} 已封禁 {player}",
            color=0xF59E0B if queued else 0xEF4444,
        )
        embed.add_field(name="理由", value=_clip(rec["reason"], 500), inline=False)
        embed.add_field(name="期限", value=f"{minutes} 分鐘" if minutes else "永久", inline=True)
        embed.add_field(name="操作人", value=str(interaction.user), inline=True)
        embed.add_field(name="結果", value=_clip(result, 1000), inline=False)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="unban", description="解除封禁")
    @app_commands.describe(player="玩家名稱")
    @app_commands.autocomplete(player=player_autocomplete)
    async def slash_unban(self, interaction: discord.Interaction, player: str):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        ok = self.blacklist.unban(player)
        await interaction.response.send_message(
            f"✅ 已解除 **{player}** 的封禁" if ok else f"⚠️ **{player}** 本來就不在名單裡",
            ephemeral=True,
        )

    @app_commands.command(name="banned", description="查看封禁名單")
    async def slash_banned(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        items = self.blacklist.items()
        if not items:
            await interaction.response.send_message("📋 封禁名單是空的", ephemeral=True)
            return

        lines = []
        for item in items[:20]:
            until = item.get("until")
            if until:
                expiry = time.strftime("%m-%d %H:%M", time.localtime(until / 1000))
                lines.append(f"`{item['name']}` — {item.get('reason', '')}（到 {expiry}）")
            else:
                lines.append(f"`{item['name']}` — {item.get('reason', '')}（永久）")
        embed = discord.Embed(title=f"🔨 封禁名單（{len(items)}）",
                              description=_clip("\n".join(lines), 3900), color=0xEF4444)
        if len(items) > 20:
            embed.set_footer(text=f"只顯示前 20 筆，共 {len(items)} 筆")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------ 斜線指令：狀態列
    statusboard = app_commands.Group(name="statusboard", description="伺服器狀態列（一行式，自動刷新）")

    @statusboard.command(name="start", description="在目前頻道建立狀態列並開始自動刷新")
    async def board_start(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await self._deny(interaction, "❌ 只能在文字頻道 / 討論串裡使用")

        await interaction.response.defer(thinking=True)
        text = await self.board.start(channel)
        await interaction.followup.send(
            f"📊 **狀態列已啟動**（每 {STATUS_INTERVAL} 秒刷新）\n```\n{text}\n```")

    @statusboard.command(name="stop", description="停止自動刷新並刪掉那條狀態訊息")
    async def board_stop(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)
        if not self.board.channel_id:
            return await self._deny(interaction, "❌ 狀態列還沒啟動")
        await interaction.response.defer(ephemeral=True)
        await self.board.stop()
        await interaction.followup.send("📊 已停止狀態列並刪除訊息", ephemeral=True)

    @statusboard.command(name="now", description="立刻刷新一次並顯示結果")
    async def board_now(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)
        if not self.board.channel_id:
            return await self._deny(interaction, "❌ 狀態列還沒啟動，先用 `/statusboard start`")
        await interaction.response.defer(ephemeral=True)
        text = await self.board.refresh()
        await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)

    # ------------------------------------------------ 斜線指令：設定
    @app_commands.command(name="settings", description="查看目前設定")
    async def slash_settings(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        guild = self.get_guild(self.config.guild_id) if self.config.guild_id else None
        channel = self.get_channel(self.config.channel_id) if self.config.channel_id else None
        embed = discord.Embed(title="⚙️ 當前設定", color=0x3B82F6)
        embed.add_field(name="伺服器", value=self._describe_guild(guild), inline=False)
        embed.add_field(name="頻道", value=self._describe_channel(channel), inline=False)
        embed.add_field(name="Minecraft",
                        value="🟢 已連線" if self.mc.game_clients() else "🔴 未連線", inline=True)
        embed.add_field(name="房主", value=OWNER_ID, inline=True)
        embed.add_field(name="XUID", value=f"`{self.miaaoo.xuid or '未獲取'}`", inline=True)
        embed.add_field(name="AddID", value=f"`{self.miaaoo.addid or '未獲取'}`", inline=True)
        embed.add_field(name="API", value=f"`{self.miaaoo.base_url}`", inline=False)
        embed.add_field(
            name="平台邀請號（豁免，不踢）",
            value=_clip("、".join(sorted(PLATFORM_INVITERS)) or "（無）", 1000),
            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="sync", description="重新同步斜線指令（改版後用）")
    async def slash_sync(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(ephemeral=True)
        self._synced_guilds.clear()
        synced, skipped = await self._sync_guild_commands()
        if skipped and not synced:
            await interaction.followup.send(
                "⏳ 指令同步被 Discord 限流（每 10 分鐘只能 2 次），已排定稍後自動補同步。\n"
                "Discord 上原本的指令不會被動到，等幾分鐘就會自己回來。", ephemeral=True)
        else:
            text = f"✅ 已重新同步 {synced} 個伺服器的斜線指令"
            if skipped:
                text += f"（{skipped} 個因限流跳過）"
            await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="shutdown", description="關閉 bot（斜線指令會一併消失）")
    async def slash_shutdown(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.send_message(
            "🛑 **正在關閉**\n會先移除斜線指令再下線；下次要上線請跑 `start.bat`。",
            ephemeral=True,
        )
        await asyncio.sleep(0.6)      # 先讓這則訊息送出去，再開始拆連線
        await self.request_shutdown()

    @app_commands.command(name="wl", description="白名單管理")
    @app_commands.describe(action="操作", user="Discord 使用者 ID 或名稱")
    @app_commands.choices(action=[
        app_commands.Choice(name="新增", value="add"),
        app_commands.Choice(name="移除", value="del"),
        app_commands.Choice(name="清單", value="list"),
    ])
    async def slash_wl(self, interaction: discord.Interaction,
                       action: app_commands.Choice[str], user: Optional[str] = None):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        if action.value == "list":
            users = self.whitelist.users
            text = "、".join(sorted(users)) if users else "（空）"
            await interaction.response.send_message(
                _clip(f"📋 **白名單**\n{text}", 1900), ephemeral=True)
            return

        if not user:
            await interaction.response.send_message(
                "❌ 請提供要操作的使用者（建議用 Discord ID）", ephemeral=True)
            return

        if action.value == "add":
            ok = self.whitelist.add(user)
            await interaction.response.send_message(
                f"✅ 已新增 `{user}`" if ok else f"⚠️ `{user}` 已在白名單中", ephemeral=True)
        else:
            ok = self.whitelist.remove(user)
            await interaction.response.send_message(
                f"✅ 已移除 `{user}`" if ok else f"❌ `{user}` 不在白名單中", ephemeral=True)

    # ------------------------------------------------ 斜線指令：聯機大廳
    @lianji.command(name="list", description="查看聯機大廳房間列表")
    @app_commands.describe(lang="語言篩選：zh / en / ja，多個用逗號分隔；留空 = 全部")
    async def lianji_list(self, interaction: discord.Interaction, lang: str = ""):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(thinking=True)
        rooms = await self.miaaoo.list_rooms(lang)
        if not rooms:
            await interaction.followup.send(
                "📋 **目前沒有房間**\n"
                "可能是 API 被限流或 Redis 剛好沒資料，房間列表每 30 秒刷新一次。")
            return

        rooms = sorted(rooms, key=lambda r: (not r.joinable, -r.players))
        view = RoomPaginator(self.miaaoo, rooms, lang)
        await interaction.followup.send(embed=view.build_embed(), view=view)

    @lianji.command(name="info", description="查看房間詳情與成員名單")
    @app_commands.describe(room="選擇房間（輸入關鍵字可搜尋）")
    @app_commands.autocomplete(room=room_autocomplete)
    async def lianji_info(self, interaction: discord.Interaction, room: str):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(thinking=True)
        target = await self._find_room(room)
        if not target:
            await interaction.followup.send(f"❌ 找不到房間：`{room}`（可能已關閉，列表 30 秒更新）")
            return

        detail = await self.miaaoo.room_detail(target)
        members = MiaaooAPI.parse_members(detail.data) if detail.ok else []

        embed = discord.Embed(
            title=f"🏠 {target.world}",
            description=f"房主 **{target.host}**　{target.mode_text}"
                        + ("　💀 極限" if target.hardcore else ""),
            color=0x22C55E if target.joinable else 0x94A3B8,
        )
        embed.add_field(name="👥 人數",
                        value=f"{target.players}/{target.max_players}", inline=True)
        embed.add_field(name="🧩 版本", value=target.version, inline=True)
        embed.add_field(name="🔗 可加入",
                        value="🟢 是" if target.joinable else f"🔒 否（{target.broadcast}）", inline=True)
        embed.add_field(name="🆔 房間 ID", value=f"`{target.id}`", inline=False)

        if not detail.ok:
            embed.add_field(name="⚠️ 成員名單", value=_clip(detail.message, 500), inline=False)
        elif members:
            # 標出「平台成員」與「遊戲內實際玩家」的差集，方便看有沒有異常加入
            in_game = {p.lower() for p in self.mc.players}
            lines = []
            for m in members[:20]:
                mark = "🟢" if m["gamertag"].lower() in in_game else "⚪"
                lines.append(f"{mark} {m['gamertag']}")
            embed.add_field(name=f"👤 成員（{len(members)}）",
                            value=_clip("\n".join(lines), 1000), inline=False)
            embed.set_footer(text="🟢 = 遊戲內也有偵測到　⚪ = 只在平台名單")
        else:
            embed.add_field(name="👤 成員", value="（取不到成員名單）", inline=False)

        await interaction.followup.send(embed=embed)

    async def _join_flow(self, interaction: discord.Interaction, room: str):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(thinking=True)
        target = await self._find_room(room)
        if not target:
            await interaction.followup.send(f"❌ 找不到房間：`{room}`（可能已關閉，列表 30 秒更新）")
            return
        if not target.joinable:
            await interaction.followup.send(
                f"🔒 **{target.world}** 目前不可加入\n"
                f"BroadcastSetting = {target.broadcast}，要 3（好友的好友）才能被外部加入。")
            return

        ident = await self.miaaoo.resolve_identity()
        xuid = ident.get("xuid", "")
        if not xuid or not xuid.isdigit() or len(xuid) != 16:
            await interaction.followup.send(
                "❌ 取不到有效的 XUID（需要 16 位數字）\n"
                f"目前：`{xuid or '空'}`\n"
                "解法：在 `.env` 設 `MC_XUID=你的16位XUID`，或確認 `MC_PLAYER_NAME` 是正確的 Gamertag。")
            return

        result = await self.miaaoo.join_room(target, xuid=xuid, addid=ident.get("addid", ""))
        if result.ok:
            embed = discord.Embed(title="✅ 加入請求已送出", color=0x22C55E)
            embed.description = (
                f"房間：**{target.world}**（房主 {target.host}）\n\n"
                "**下一步**：打開遊戲 → `世界` 分頁 → 點重新整理，房間卡片就會出現，點它加入。\n"
                "（後端會先把你的帳號加進該帳號的粉絲，再發邀請，所以直接開世界列表就行）\n\n"
                "ℹ️ 之後 Xbox 會收到來自 "
                f"**{_clip('、'.join(sorted(PLATFORM_INVITERS)) or '平台帳號')}** "
                "的邀請 — 那是 miaaoo 官方的邀請帳號，屬正常流程，不用理會（這些號已設為豁免，不會被踢）。"
            )
            embed.set_footer(text=f"XUID {xuid}｜addid {ident.get('addid', '')}｜roomfrom {target.roomfrom}")
        else:
            embed = discord.Embed(title="❌ 加入失敗", color=0xEF4444)
            embed.description = _clip(result.message, 1500)
            embed.add_field(name="HTTP", value=f"`{result.status}`", inline=True)
            embed.add_field(name="code", value=f"`{result.code or '-'}`", inline=True)
            embed.add_field(name="房間", value=f"`{target.id}`", inline=False)
        await interaction.followup.send(embed=embed)

    @lianji.command(name="join", description="加入指定房間")
    @app_commands.describe(room="選擇房間（輸入關鍵字可搜尋）")
    @app_commands.autocomplete(room=room_autocomplete)
    async def lianji_join(self, interaction: discord.Interaction, room: str):
        await self._join_flow(interaction, room)

    @app_commands.command(name="join", description="加入指定房間（快捷，等同 /lianji join）")
    @app_commands.describe(room="選擇房間（輸入關鍵字可搜尋）")
    @app_commands.autocomplete(room=room_autocomplete)
    async def slash_join(self, interaction: discord.Interaction, room: str):
        await self._join_flow(interaction, room)

    @lianji.command(name="me", description="查看自己的 XUID / addid 與可用帳號")
    async def lianji_me(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(thinking=True)
        ident = await self.miaaoo.resolve_identity()
        accounts = await self.miaaoo.list_accounts()
        note = self.miaaoo._accounts_note      # 429 之類的原因，讓使用者知道是不是舊資料

        embed = discord.Embed(title="🔑 我的聯機身分", color=0x3B82F6)
        embed.add_field(name="XUID", value=f"`{ident.get('xuid') or '（取不到）'}`", inline=True)
        embed.add_field(name="AddID", value=f"`{ident.get('addid') or '（取不到）'}`", inline=True)
        embed.add_field(name="Gamertag", value=ident.get("gamertag") or MC_PLAYER_NAME, inline=True)

        if accounts:
            lines = [f"`{a.get('id')}` {a.get('name', '')}"
                     + ("　✅ 可加入" if a.get("canaddfriends") else "　⛔ 不可加入")
                     for a in accounts]
            title = "中繼帳號（/v2/account）"
            if note:
                title += f" · {note}，以下為上次結果"
            embed.add_field(name=title, value=_clip("\n".join(lines), 1000), inline=False)
        else:
            embed.add_field(name="中繼帳號",
                            value=f"取不到（{note or 'API 沒回資料'}），稍後再試", inline=False)

        if not MC_XUID:
            embed.set_footer(text="想固定 XUID？在 .env 設 MC_XUID=你的16位XUID，就不必每次查 /profile")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @lianji.command(name="refresh", description="刷新後端粉絲快取（房間列表會更新）")
    async def lianji_refresh(self, interaction: discord.Interaction):
        if not self._may_manage(interaction):
            return await self._deny(interaction)

        await interaction.response.defer(thinking=True)
        status, message = await self.miaaoo.refresh_followers()
        await interaction.followup.send(
            f"🔄 **刷新粉絲快取**\nHTTP `{status}`\n{message}\n"
            "房間列表由後端每 30 秒重建一次，稍等一下再用 `/lianji list`。",
            ephemeral=True,
        )

    # ------------------------------------------------ 文字指令
    def _status_text(self) -> str:
        mode = "單向（遊戲 → Discord）" if ONE_WAY_MODE else "雙向"
        state = "🟢 已連線" if self.mc.connected else "🔴 未連線"
        return f"**橋接狀態**：{state}\n**模式**：{mode}\n**玩家數**：{len(self.mc.players)}"

    async def _cmd_help(self, message: discord.Message):
        await message.reply(
            "**斜線指令**（推薦）\n"
            "`/status` 橋接狀態　`/players` 線上玩家　`/check` 時間/天氣/種子\n"
            "`/lianji list` 房間列表　`/lianji info` 房間詳情　`/lianji join` 加入\n"
            "`/lianji me` 我的 XUID　`/lianji refresh` 刷新粉絲\n"
            "`/kick` 踢人　`/ban` 封禁　`/unban` 解封　`/banned` 名單\n"
            "`/cmd` 執行遊戲指令　`/settings` 設定　`/sync` 同步指令　`/wl` 白名單\n\n"
            "**文字指令**（舊版，仍可用）\n"
            "`!status`　`!players`　`!check`　`!settings`\n"
            "`!ban 名字 [分鐘] [理由]`　`!unban 名字`　`!banned`\n"
            "`!eat @玩家`（kill 被 @ 的人）\n"
            "`!cmd <指令>`　`!invite`　`!sync`　`!reload`　`!wl add/del/list`"
        )

    async def _cmd_status(self, message: discord.Message):
        await message.reply(self._status_text())

    async def _cmd_sync(self, message: discord.Message):
        if not self._check_perm(message):
            await message.reply("❌ 你沒有權限")
            return
        status = await message.reply("🔄 同步中...")
        self._synced_guilds.clear()
        synced, skipped = await self._sync_guild_commands(force=True)
        if skipped and not synced:
            await status.edit(content="⏳ 指令同步被 Discord 限流（每 10 分鐘只能 2 次），已排定稍後自動補同步。原本的指令不會被動到。")
        else:
            text = f"✅ 已同步 {synced} 個伺服器的斜線指令"
            if skipped:
                text += f"（{skipped} 個因限流跳過）"
            await status.edit(content=text)

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
        """把逾時 / 例外 / 「只有 ACK」統一成空字串，避免被當成真的回覆顯示。"""
        if value is None or isinstance(value, BaseException):
            return ""
        text = str(value).strip()
        if text.startswith("⚠️") or text.startswith("（"):
            return ""
        return text

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

        status = await message.reply("⏳ 執行中...")
        result = await self.send_game_command(cmd, f"!cmd {cmd}", timeout=15)
        if result.startswith("⏳"):
            await status.edit(content=result)
        elif not result or result == "（無回應）" or result.startswith("⚠️"):
            await status.edit(content="⚠️ 執行失敗（指令無回應）")
        else:
            await status.edit(content=f"📋 結果：\n```\n{_clip(result, 1800)}\n```")

    async def _cmd_eat(self, message: discord.Message):
        """`!eat @玩家`（也接受 `.eat @玩家`）→ 把被 @ 的玩家在遊戲裡 kill 掉。

        Discord 的 @ 對應到誰，用「暱稱(display_name) == 遊戲名」來認，
        所以要 kill 誰，就先 @ 那個跟遊戲同名的成員。
        """
        if not self._check_perm(message):
            await message.reply("❌ 你沒有權限")
            return
        mentions = message.mentions
        if not mentions:
            await message.reply("用法：`!eat @玩家`（要 @ 一個人；Discord 暱稱需等於遊戲名）")
            return
        results = []
        for m in mentions:
            name = (m.display_name or m.name or "").strip()
            if not name:
                continue
            result = await self.kill_player(name)
            results.append(f"💀 **{name}**：{result}")
        await message.reply("\n".join(results) if results else "❌ 沒抓到要 kill 的玩家")

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
    mc.on_room_change = bot.on_room_change
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

    gui_runner = None
    try:
        gui_runner = await start_gui(bot, blacklist)
    except OSError as exc:
        log.warning(f"⚠️ 控制面板啟動失敗（port {GUI_PORT} 被占用？）: {exc}")

    # Ctrl+C / SIGTERM 也要走「優雅關閉」，否則來不及移除斜線指令
    stop_event = asyncio.Event()

    def _handle_signal(*_args):
        if bot._shutdown_requested:
            log.warning("⌨️ 再次中斷，強制結束")
            os._exit(1)
        log.info("⌨️ 收到中斷訊號，準備優雅關閉")
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass                      # 非主執行緒等情況維持預設行為

    start_task = asyncio.create_task(bot.start(DISCORD_TOKEN))
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

        if start_task.done():
            exc = start_task.exception()
            if isinstance(exc, discord.LoginFailure):
                log.error("❌ Discord 登入失敗：DISCORD_TOKEN 無效或已被重置，請到 Developer Portal 重新複製")
            elif isinstance(exc, discord.PrivilegedIntentsRequired):
                log.error("❌ 缺少特權 Intent：請到 Developer Portal → Bot → 開啟 MESSAGE CONTENT INTENT")
            elif exc is not None:
                log.error(f"❌ Discord 連線異常: {exc!r}")

        if not start_task.done():
            bot._shutdown_requested = True      # 訊號觸發，算「主動停止」
        if not bot.is_closed():
            await bot.close()
    finally:
        stop_task.cancel()
        if not start_task.done():
            start_task.cancel()
        try:
            await start_task
        except (asyncio.CancelledError, Exception):
            pass

        log.info("🛑 正在關閉...")
        ws_task.cancel()
        try:
            await ws_task
        except (asyncio.CancelledError, Exception):
            pass
        await mc.stop()
        if api_runner is not None:
            await api_runner.cleanup()
        if gui_runner is not None:
            await gui_runner.cleanup()
        await miaaoo.close()

        # 告訴 run_bot.bat「這次是使用者主動停的，不要自動重啟」
        if bot._shutdown_requested:
            try:
                STOP_FLAG.write_text(str(int(time.time())), encoding="utf-8")
                log.info("📛 已標記主動停止，run_bot.bat 不會自動重啟")
            except OSError as exc:
                log.warning(f"無法寫入停止標記: {exc}")

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, signal.SIG_DFL)
                except (ValueError, OSError):
                    pass

def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0

def _cli_stop() -> None:
    """`python bot.py --stop`：請正在跑的實例優雅關閉（含移除斜線指令）。"""
    url = f"http://127.0.0.1:{BAN_API_PORT}/shutdown"
    print(f"[stop] 請求 {url} …")
    try:
        req = urllib.request.Request(
            url, data=b"{}", method="POST",
            headers={"X-Api-Key": BAN_API_KEY, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print("[stop]", resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        print(f"[stop] 連不上執行中的 bot（可能沒在跑）: {exc}")
        return

    for _ in range(30):
        time.sleep(0.5)
        if not _port_open(LOCK_PORT):
            print("[stop] bot 已停止（斜線指令保留在 Discord，可繼續使用）")
            return
    print("[stop] ⚠️ bot 還在關閉中，稍等一下（或直接關掉視窗）")

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

    # python bot.py --stop：不打單實例鎖，直接把正在跑的那份請下線
    if "--stop" in sys.argv or "-stop" in sys.argv:
        _cli_stop()
        return

    # 清掉上一次留下的「主動停止」標記
    try:
        if STOP_FLAG.exists():
            STOP_FLAG.unlink()
    except OSError:
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
