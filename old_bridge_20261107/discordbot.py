"""Discord 端：把遊戲事件送到頻道，並提供管理指令。"""
import re

import aiohttp
import discord
from discord import app_commands

from .config import (CHAT_FORMAT_DC_TO_MC, CHAT_FORMAT_MC_TO_DC, ENABLE_BAN_ALERT, ENABLE_COMMAND,
                     ENABLE_JOIN_LEAVE, ONE_WAY_MODE, log)
from .jsonstore import BanStore, BridgeConfig, WhitelistStore
from .legacy_commands import LegacyCommands, extract_players
from .miaaoo import MiaaooAPI
from .mcws import MCWebSocketServer


def is_admin(message: discord.Message) -> bool:
    perms = getattr(message.author, "guild_permissions", None)
    return bool(perms and getattr(perms, "administrator", False))


class DCListener(LegacyCommands, discord.Client):
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
        self._session: aiohttp.ClientSession | None = None
        self.tree = app_commands.CommandTree(self)

    # ------------------------------------------------ 生命週期
    async def setup_hook(self):
        # 先把「全域指令」清空：舊版本殘留的全域 /ping 等會造成 signature mismatch，
        # 而且全域指令更新要等 Discord 快取，乾脆不用全域，全部走 guild 指令。
        self.tree.clear_commands(guild=None)
        try:
            await self.tree.sync()
            log.info("🧹 已清空全域斜線指令，改用 guild 指令")
        except Exception as e:
            log.error(f"清空全域指令失敗: {e}")

        for cmd in (self.slash_status, self.slash_ping, self.slash_lianji, self.slash_join):
            self.tree.add_command(cmd)

    async def on_disconnect(self):
        log.warning("⚠️ Discord 連線中斷，準備重連")

    async def on_resumed(self):
        log.info("✅ Discord 連線已恢復")

    async def on_ready(self):
        log.info(f"✅ Discord 已登入：{self.user}")
        if not self._session:
            self._session = aiohttp.ClientSession()

        # 舊版同步過的 guild 指令可能和新程式 signature 不同 → 用目前程式覆蓋掉
        for guild in self.guilds:
            try:
                obj = discord.Object(id=guild.id)
                self.tree.copy_global_to(guild=obj)
                await self.tree.sync(guild=obj)
                log.info(f"🧹 已覆蓋 {guild.name} 的 guild 指令")
            except Exception as e:
                log.warning(f"覆蓋 {guild.name} 的 guild 指令失敗: {e}")

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None
        await super().close()

    # ------------------------------------------------ Discord → 遊戲
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.webhook_id is not None:
            return
        content = message.content.strip()
        if not content:
            # 沒有內容通常是 Discord 後台沒開「Message Content Intent」
            log.warning(f"收到空白訊息（可能缺少 Message Content Intent）: {message.author} channel={message.channel.id}")
            return

        # 指令不受頻道白名單限制（權限在各指令內部檢查），避免「打了沒反應」
        if content.startswith("!"):
            await self._handle_command(message)
            return

        # 以下只影響「Discord 聊天轉發進遊戲」，允許條件取聯集：
        # .env 的清單、或 bridge_config.json 的單一值；兩者都沒設定就全開
        guild_ok = not (self.config.guild_ids or self.config.guild_id)
        if self.config.guild_ids and message.guild is not None and message.guild.id in self.config.guild_ids:
            guild_ok = True
        if self.config.guild_id and message.guild is not None and message.guild.id == self.config.guild_id:
            guild_ok = True

        channel_ok = not (self.config.channel_ids or self.config.channel_id)
        if self.config.channel_ids and message.channel.id in self.config.channel_ids:
            channel_ok = True
        if self.config.channel_id and message.channel.id == self.config.channel_id:
            channel_ok = True

        if not guild_ok or not channel_ok:
            return

        if not ONE_WAY_MODE and self.mc.connected:
            formatted = CHAT_FORMAT_DC_TO_MC.format(username=message.author.display_name,
                                                    message=message.clean_content)
            await self.mc.send_tellraw(formatted)

    # ------------------------------------------------ 指令
    async def _handle_command(self, message: discord.Message):
        cmd = message.content.strip().split()[0].lower()
        authorized = self._check_perm(message)

        if cmd == "!sync":
            if not authorized:
                return
            await message.reply("🔄 同步中...")
            self.tree.clear_commands(guild=None)
            self.tree.add_command(self.slash_status)
            await self.tree.sync()
            await message.reply("✅ 已同步斜線指令")

        elif cmd == "!status":
            await message.reply(self._status_text())

        elif cmd == "!players":
            await self._cmd_players_list(message)

        elif cmd == "!ban":
            if not authorized:
                return
            await self._cmd_ban(message)

        elif cmd == "!unban":
            if not authorized:
                return
            await self._cmd_unban(message)

        elif cmd == "!banned":
            await self._cmd_banned(message)

        elif cmd == "!check":
            await self._cmd_check(message)

        elif cmd == "!cmd":
            await self._cmd_mccommand(message)

        elif cmd == "!invite":
            await self._cmd_invite(message)

        elif cmd == "!settings":
            await self._cmd_settings(message)

        elif cmd == "!setguild":
            await self._cmd_setguild(message)

        elif cmd == "!setchannel":
            await self._cmd_setchannel(message)

        elif cmd == "!reload":
            await self._cmd_reload(message)

        elif cmd == "!wl":
            await self._cmd_wl(message)

        elif cmd == "!help":
            await message.reply(
                "**斜線指令**\n"
                "`/status` 橋接狀態　`/ping` 測試\n"
                "`/lianji` Miaaoo 房間列表　`/join <ID>` 加入房間\n\n"
                "**查詢**\n"
                "`!status` 橋接狀態　`!players` 線上玩家（用 list 查）\n"
                "`!check` 伺服器狀態（時間/天氣/種子）　`!settings` 當前設定\n\n"
                "**封禁**\n"
                "`!ban 名字 [分鐘] [理由]`　`!unban 名字`　`!banned`\n\n"
                "**執行**\n"
                "`!cmd <指令>` 執行 Minecraft 指令（有黑名單）\n"
                "`!invite` 邀請房主\n\n"
                "**管理（管理員）**\n"
                "`!setguild` `!setchannel` `!reload` `!wl add/del/list` `!sync`"
            )

        else:
            await message.reply(f"❓ 未知指令 `{cmd}`，打 `!help` 看全部指令")

    async def _cmd_ban(self, message: discord.Message):
        parts = message.content.strip().split(maxsplit=3)
        if len(parts) < 2:
            await message.reply("用法：`!ban 名字 [分鐘] [理由]`")
            return

        name = parts[1]
        minutes, reason = 0, ""
        if len(parts) >= 3 and parts[2].isdigit():
            minutes = int(parts[2])
            reason = parts[3] if len(parts) >= 4 else ""
        elif len(parts) >= 3:
            reason = parts[2]

        rec = self.blacklist.ban(name, reason, minutes, str(message.author))
        result = await self.kick_player(name, rec["reason"])
        await message.reply(f"🔨 已封禁 **{name}**（{rec['reason']}）\n{result}")

    async def _cmd_unban(self, message: discord.Message):
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
        lines = [f"`{i['name']}` — {i.get('reason', '')}" for i in items[:20]]
        await message.reply("**封禁名單**\n" + "\n".join(lines))

    # ------------------------------------------------ 權限
    def _status_text(self) -> str:
        mode = "單向（遊戲 → Discord）" if ONE_WAY_MODE else "雙向"
        state = "🟢 已連線" if self.mc.connected else "🔴 未連線"
        return f"**橋接狀態**：{state}\n**模式**：{mode}\n**玩家數**：{len(self.mc.players)}"

    def _check_perm(self, message: discord.Message) -> bool:
        return is_admin(message) or self._authorized(message.author)

    def _authorized(self, author) -> bool:
        users = self.whitelist.users
        return str(author.id) in users or getattr(author, "name", "") in users

    # ------------------------------------------------ 遊戲 → Discord
    async def _send_to_discord(self, username: str, content: str):
        if self.config.channel_ids:
            for cid in self.config.channel_ids:
                channel = self.get_channel(cid)
                if not channel:
                    continue
                try:
                    await channel.send(content[:1900])
                except Exception as e:
                    log.error(f"發送到頻道 {cid} 失敗: {e}")

        if self.webhook_url and self._session:
            try:
                payload = {
                    "username": username[:80],
                    "content": content[:1900],
                    "avatar_url": f"https://mc-heads.net/avatar/{username}/64",
                }
                async with self._session.post(self.webhook_url, json=payload, timeout=5) as resp:
                    await resp.text()
            except Exception:
                pass

    async def on_mc_chat(self, sender: str, message: str):
        await self._send_to_discord(sender, CHAT_FORMAT_MC_TO_DC.format(sender=sender, message=message))

    async def on_mc_join(self, player: str):
        if ENABLE_JOIN_LEAVE:
            await self._send_to_discord("伺服器", f"✅ **{player}** 加入了遊戲")
        await self.on_player_joined(player)

    async def on_mc_leave(self, player: str):
        if ENABLE_JOIN_LEAVE:
            await self._send_to_discord("伺服器", f"❌ **{player}** 離開了遊戲")

    async def on_mc_death(self, player: str, message: str):
        await self._send_to_discord("伺服器", f"💀 {message.strip() or f'**{player}** 死亡了'}")

    async def on_mc_ban(self, text: str):
        if ENABLE_BAN_ALERT:
            await self._send_to_discord("防禦系統", f"🔨 **封禁事件**\n`{text[:500]}`")

    async def on_mc_tp(self, text: str):
        match = re.search(r"(?:名稱|名称|xuid|玩家)\s*[:：]?\s*([A-Za-z0-9_ ]{2,24})", text)
        lines = ["🛑 **偵測到 TP 平台特徵**"]
        if match:
            lines.append(f"目標：**{match.group(1).strip()}**")
        lines.append(f"內容：`{text[:500]}`")
        if ONE_WAY_MODE:
            lines.append("（單向模式：未執行 kick）")
        await self._send_to_discord("防禦系統", "\n".join(lines))

    async def on_mc_tp_join(self, player: str):
        """偵測到 TP 平台拉人：只提示，等你用 /ban 或 !ban 處理。"""
        log.warning(f"🚨 TP 平台加入者: {player}")
        rec = self.blacklist.get(player)
        status = f"已在封禁名單（{rec.get('reason')}）" if rec else "不在封禁名單"
        await self._send_to_discord(
            "防禦系統",
            f"🛑 **偵測到 TP 平台拉人**\n玩家: **{player}**\n{status}\n"
            f"要處理請用 `POST /ban` 或 Discord `!ban {player}`",
        )

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
        return result

    async def on_bridge_connected(self):
        log.info("🟢 橋接程式已連線")

    async def on_bridge_disconnected(self):
        log.info("🔴 橋接程式已斷線")

    # ------------------------------------------------ 斜線指令
    @app_commands.command(name="status", description="顯示橋接狀態")
    async def slash_status(self, interaction: discord.Interaction):
        await interaction.response.send_message(self._status_text(), ephemeral=True)
