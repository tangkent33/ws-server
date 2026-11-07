"""舊版 Discord 指令（!cmd / !check / !players / !invite / !settings / !wl / /lianji / /join）。

以 mixin 形式掛回 DCListener，避免把主類別撐得太大。
需要的屬性：self.mc、self.whitelist、self.blacklist、self.config、self.miaaoo、self._session
"""
import json
import re

import discord
from discord import app_commands

from .config import (CHAT_FORMAT_DC_TO_MC, COMMAND_BLACKLIST, OWNER_ID, log)

# ---------------------------------------------------------------- 工具
def normalize_cmd(raw: str) -> str:
    raw = raw.strip()
    return raw[1:].strip() if raw.startswith("/") else raw


def first_token(cmd: str) -> str:
    parts = cmd.split()
    return parts[0].lower() if parts else ""


def extract_players(raw: str):
    if not raw:
        return []
    m = re.search(r"(?:玩家在線|Players online)\s*[:：]?\s*\n?\s*(.+)", raw, re.IGNORECASE)
    if m:
        tail = m.group(1).strip()
        if tail in ("0", "無"):
            return []
        return [x.strip() for x in tail.split(",") if x.strip()]
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    return parts if parts and parts[0] != "0" else []


class LegacyCommands:
    """混入 DCListener 使用；方法名稱以 _legacy_ 開頭避免撞名。"""

    # ------------------------------------------------ 斜線指令
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
                world_name = props.get("worldName", "未知地圖")
                host_name = props.get("hostName", "未知房主")
                member_count = props.get("MemberCount", 0)
                max_members = props.get("MaxMemberCount", 0)
                version = props.get("version", "未知版本")
                world_type = props.get("worldType", "未知模式")
                room_id = room.get("id", "N/A")
                joinable = "🟢 可加入" if props.get("BroadcastSetting", 0) == 3 else "🔒 不可加入"
                lines.append(
                    f"**{i}. {world_name}**\n"
                    f"  🏠 房主: {host_name}\n"
                    f"  👥 {member_count}/{max_members} 人\n"
                    f"  📌 模式: {world_type}\n"
                    f"  📦 版本: {version}\n"
                    f"  🆔 ID: `{room_id}`\n"
                    f"  🔗 {joinable}\n"
                )
            lines.append(f"\n共 {len(rooms)} 個房間")
            lines.append("使用 `/join <ID>` 加入房間")
            content = "\n".join(lines)
            if len(content) > 1900:
                content = content[:1900] + "\n...(截斷)"
            await interaction.followup.send(content)
        except Exception as e:
            log.error(f"Lianji 錯誤: {e}")
            await interaction.followup.send(f"❌ 查詢失敗：{e}")

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
        except Exception as e:
            log.error(f"Join 錯誤: {e}")
            await interaction.followup.send(f"❌ 加入失敗：{e}")

    # ------------------------------------------------ 文字指令（由 DCListener 分派）
    async def _cmd_check(self, message: discord.Message):
        if not self._authorized(message.author):
            await message.reply("❌ 你不在白名單中")
            return
        if not self.mc.connected:
            await message.reply("❌ Minecraft 未連線")
            return

        status = await message.reply("🔍 檢查中...")
        list_raw = await self.mc.send_command("list", timeout=10)
        time_raw = await self.mc.send_command("time query daytime", timeout=10)
        weather_raw = await self.mc.send_command("weather query", timeout=10)
        seed_raw = await self.mc.send_command("seed", timeout=10)

        players = extract_players(list_raw or "")
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
        await status.edit(content="**📊 伺服器狀態**\n" + "\n".join(lines))

    async def _cmd_players_list(self, message: discord.Message):
        if not self._authorized(message.author):
            await message.reply("❌ 你不在白名單中")
            return
        if not self.mc.connected:
            await message.reply("❌ Minecraft 未連線")
            return

        raw = await self.mc.send_command("list", timeout=10)
        players = extract_players(raw or "")
        if players:
            await message.reply(f"**👥 線上玩家 ({len(players)})**\n```\n{', '.join(players)}\n```")
        else:
            await message.reply("📋 目前沒有玩家在線")

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
        result = await self.mc.send_command(cmd, timeout=15)
        if result is None:
            await status.edit(content="⚠️ 執行失敗（遊戲未連線？）")
        else:
            if len(result) > 1800:
                result = result[:1800] + "\n...(截斷)"
            await status.edit(content=f"📋 結果：\n```\n{result}\n```")

    async def _cmd_invite(self, message: discord.Message):
        if not self._authorized(message.author):
            await message.reply("❌ 你不在白名單中")
            return
        if not self.mc.connected:
            await message.reply("❌ Minecraft 未連線")
            return

        status = await message.reply(f"🎮 邀請 {OWNER_ID}...")
        result = await self.mc.send_command(f"invite {OWNER_ID}", timeout=12)
        if result:
            await status.edit(content=f"**邀請結果**\n```\n{result[:1800]}\n```")
        else:
            await status.edit(content="⚠️ 邀請失敗")

    async def _cmd_settings(self, message: discord.Message):
        guild = self.get_guild(self.config.guild_id) if self.config.guild_id else None
        channel = self.get_channel(self.config.channel_id) if self.config.channel_id else None
        lines = [
            f"📢 伺服器: {guild.name if guild else (f'ID {self.config.guild_id}（未找到）' if self.config.guild_id else '未設定')}",
            f"📢 頻道: {channel.mention if channel else (f'ID {self.config.channel_id}（未找到）' if self.config.channel_id else '未設定')}",
            f"🎮 Minecraft: {'🟢 已連線' if self.mc.connected else '🔴 已斷線'}",
            f"👑 房主: {OWNER_ID}",
            f"🔑 XUID: {self.miaaoo.xuid or '未獲取（會自動獲取）'}",
            f"🔑 AddID: {self.miaaoo.addid or '未獲取（會自動獲取）'}",
        ]
        await message.reply("**⚙️ 當前設定**\n" + "\n".join(lines))

    async def _cmd_setguild(self, message: discord.Message):
        if not (getattr(message.author.guild_permissions, "administrator", False)):
            await message.reply("❌ 需要管理員權限")
            return
        if message.guild is None:
            await message.reply("❌ 此指令只能在伺服器中使用")
            return
        self.config.guild_id = message.guild.id
        self.config.save()
        await message.reply(f"✅ 已設定允許的伺服器：{message.guild.name} (`{message.guild.id}`)")

    async def _cmd_setchannel(self, message: discord.Message):
        if not (getattr(message.author.guild_permissions, "administrator", False)):
            await message.reply("❌ 需要管理員權限")
            return
        self.config.channel_id = message.channel.id
        self.config.save()
        await message.reply(f"✅ 已設定允許的頻道：{message.channel.mention} (`{message.channel.id}`)")

    async def _cmd_reload(self, message: discord.Message):
        if not (getattr(message.author.guild_permissions, "administrator", False)):
            await message.reply("❌ 需要管理員權限")
            return
        self.whitelist.load()
        await message.reply("🔄 白名單已重新載入")

    async def _cmd_wl(self, message: discord.Message):
        if not (getattr(message.author.guild_permissions, "administrator", False)):
            await message.reply("❌ 需要管理員權限")
            return

        parts = message.content.split()
        if len(parts) < 2:
            await message.reply("❌ 用法: `!wl add|del|list <名稱>`")
            return

        action = parts[1].lower()
        users = list(self.whitelist.users)

        def save():
            self.whitelist._data = users
            self.whitelist.save()

        if action == "add":
            if len(parts) < 3:
                await message.reply("❌ 請提供名稱")
                return
            target = parts[2]
            if target in users:
                await message.reply(f"⚠️ `{target}` 已在白名單中")
                return
            users.append(target)
            save()
            await message.reply(f"✅ 已新增 `{target}`")

        elif action == "del":
            if len(parts) < 3:
                await message.reply("❌ 請提供名稱")
                return
            target = parts[2]
            if target not in users:
                await message.reply(f"❌ `{target}` 不在白名單中")
                return
            users.remove(target)
            save()
            await message.reply(f"✅ 已移除 `{target}`")

        elif action == "list":
            if not users:
                await message.reply("📋 白名單為空")
                return
            preview = ", ".join(users)
            if len(preview) > 1800:
                preview = preview[:1800] + "..."
            await message.reply(f"📋 白名單：\n```\n{preview}\n```")
        else:
            await message.reply("❌ 未知子指令")
