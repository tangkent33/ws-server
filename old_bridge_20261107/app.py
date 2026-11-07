"""組裝：把 WebSocket 伺服器、Discord bot、Ban API 接起來跑。"""
import asyncio

from .banapi import start_ban_api
from .config import (BLACKLIST_FILE, CONFIG_FILE, DEFAULT_AUTHORIZED, DISCORD_TOKEN,
                     INIT_ALLOWED_CHANNELS, INIT_ALLOWED_GUILDS, MC_HOST, MC_PORT,
                     WEBHOOK_URL, WHITELIST_FILE, log)
from .discordbot import DCListener
from .jsonstore import BanStore, BridgeConfig, WhitelistStore
from .miaaoo import MiaaooAPI
from .mcws import MCWebSocketServer


def _wire(mc: MCWebSocketServer, bot: DCListener):
    mc.on_chat = bot.on_mc_chat
    mc.on_join = bot.on_mc_join
    mc.on_leave = bot.on_mc_leave
    mc.on_death = bot.on_mc_death
    mc.on_tp = bot.on_mc_tp
    mc.on_tp_join = bot.on_mc_tp_join
    mc.on_ban = bot.on_mc_ban
    mc.on_connected = bot.on_bridge_connected
    mc.on_disconnected = bot.on_bridge_disconnected


async def main():
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

    ws_task = asyncio.create_task(mc.start())
    api_runner = await start_ban_api(bot, blacklist)

    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        await mc.stop()
        await api_runner.cleanup()
        await miaaoo.close()
