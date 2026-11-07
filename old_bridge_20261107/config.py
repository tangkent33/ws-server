"""設定：從 .env 讀取，集中所有可調參數。"""
import logging
import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ------------------------------------------------------------------ Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# ------------------------------------------------------------------ Minecraft
MC_HOST = os.getenv("MC_SERVER_IP", "127.0.0.1")
MC_PORT = int(os.getenv("MC_WS_PORT", "8000"))
MC_PLAYER_NAME = os.getenv("MC_PLAYER_NAME", "tangkent3")

# ------------------------------------------------------------------ 檔案
WHITELIST_FILE = os.path.expanduser(os.getenv("WHITELIST_FILE", "~/whitelist.json"))
BLACKLIST_FILE = str(BASE_DIR / os.getenv("BLACKLIST_FILE", "blacklist.json"))
CONFIG_FILE = os.path.expanduser(os.getenv("BRIDGE_CONFIG_FILE", "~/bridge_config.json"))
MIAOO_API_BASE = os.getenv("MIAOO_API_BASE", "https://api.miaaoo.com")
OWNER_ID = os.getenv("OWNER_ID", "tangkent3")

# ------------------------------------------------------------------ 封禁
TP_BAN_MINUTES = int(os.getenv("TP_BAN_MINUTES", "0"))          # 0 = 永久
TP_AUTO_BLACKLIST = os.getenv("TP_AUTO_BLACKLIST", "false").lower() == "true"
ENFORCE_BAN_ON_JOIN = os.getenv("ENFORCE_BAN_ON_JOIN", "true").lower() == "true"

BAN_API_PORT = int(os.getenv("BAN_API_PORT", "3001"))
BAN_API_KEY = os.getenv("BAN_API_KEY", "my-secret-123")

# ------------------------------------------------------------------ 事件開關
ENABLE_JOIN_LEAVE = os.getenv("ENABLE_JOIN_LEAVE", "true").lower() == "true"
ENABLE_DEATH = os.getenv("ENABLE_DEATH", "true").lower() == "true"
ENABLE_TP_ALERT = os.getenv("ENABLE_TP_ALERT", "true").lower() == "true"
ENABLE_BAN_ALERT = os.getenv("ENABLE_BAN_ALERT", "true").lower() == "true"
ENABLE_CHAT = os.getenv("ENABLE_CHAT", "true").lower() == "true"
ENABLE_COMMAND = os.getenv("ENABLE_COMMAND", "true").lower() == "true"

# 單向模式：只把遊戲事件送到 Discord，不嘗試送聊天回遊戲
ONE_WAY_MODE = os.getenv("ONE_WAY_MODE", "true").lower() == "true"

# ------------------------------------------------------------------ 格式
COMMAND_BLACKLIST = set(
    x.strip().lower()
    for x in os.getenv("COMMAND_BLACKLIST", "stop,op,deop,ban,pardon,kick,gamemode,give,clear,kill").split(",")
    if x.strip()
)

CHAT_FORMAT_MC_TO_DC = os.getenv("CHAT_FORMAT_MC_TO_DC", "{message}")
CHAT_FORMAT_DC_TO_MC = os.getenv("CHAT_FORMAT_DC_TO_MC", "§c[§6Discord] §b{username}§e: {message}")


def parse_ids(env_str: str) -> List[int]:
    """把 "123, 456" 這種字串轉成 [123, 456]，忽略 0 與非數字。"""
    if not env_str:
        return []
    return [int(x.strip()) for x in env_str.split(",") if x.strip().isdigit() and int(x.strip()) != 0]


INIT_ALLOWED_GUILDS = parse_ids(os.getenv("ALLOWED_GUILD_ID", "0"))
INIT_ALLOWED_CHANNELS = parse_ids(os.getenv("ALLOWED_CHANNEL_ID", "0"))

DEFAULT_AUTHORIZED = [x.strip() for x in os.getenv("DEFAULT_AUTHORIZED_USERS", "tangkent3").split(",") if x.strip()]


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] [%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return logging.getLogger("bridge")


log = setup_logging()
