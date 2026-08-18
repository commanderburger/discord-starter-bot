import asyncio
import json
import logging
import os
import random
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


log = logging.getLogger("starter-bot.giveaways")
DATA_FILE = Path(os.getenv("BOT_DATA_DIR", "/data")) / "giveaways.json"
DURATION_RE = re.compile(r"^(\d+)([smhdw])$", re.IGNORECASE)
ENTRY_EMOJI = "🎉"


def parse_duration(value: str) -> int:
    match = DURATION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("Use a time such as `10m`, `2h`, `1d`, or `1w`.")
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2).lower()]
    if seconds < 10 or seconds > 31_536_000:
        raise ValueError("The time must be between 10 seconds and 1 year.")
    return seconds


def load_data() -> dict:
    try:
        with DATA_FILE.open(encoding="utf-8") as file:
            value = json.load(file)
        if isinstance(value, dict):
            value.setdefault("giveaways", {})
            value.setdefault("auto", {})
            return value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"giveaways": {}, "auto": {}}


def atomic_write(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")
    temporary.replace(DATA_FILE)


class Giveaways(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.data_lock = asyncio.Lock()
        self.giveaway_loop.start()

    def cog_unload(self) -> None:
        self.giveaway_loop.cancel()

    async def save(self, data: dict) -> None:
        await asyncio.to_thread(atomic_write, data)

    async def create_giveaway(
        self,
        channel: discord.TextChannel,
        host_id: int,
        prize: str,
        duration_seconds: int,
        winner_count: int,
        required_role_id: int | None,
        schedule_id: str | None = None,
    ) -> discord.Message:
        ends_at = int(datetime.now(UTC).timestamp()) + duration_seconds
        embed = discord.Embed(
            title=f"🎉 {prize}",
            description=(
                f"React with {ENTRY_EMOJI} to enter!\n\n"
                f"**Winners:** {winner_count}\n"
                f"**Ends:** <t:{ends_at}:R>\n"
                f"**Hosted by:** <@{host_id}>"
            ),
            color=discord.Color.magenta(),
        )
        if required_role_id:
            embed.add_field(name="Required role", value=f"<@&{required_role_id}>", inline=False)
        message = await channel.send(embed=embed)
        await message.add_reaction(ENTRY_EMOJI)
        async with self.data_lock:
            data = load_data()
            data["giveaways"][str(message.id)] = {
                "guild_id": channel.guild.id,
                "channel_id": channel.id,
                "host_id": host_id,
                "prize": prize[:250],
                "winner_count": winner_count,
                "required_role_id": required_role_id,
                "ends_at": ends_at,
                "ended": False,
                "schedule_id": schedule_id,
            }
            await self.save(data)
        return message

    async def eligible_users(self, message: discord.Message, required_role_id: int | None) -> list[discord.Member]:
        reaction = discord.utils.find(lambda item: str(item.emoji) == ENTRY_EMOJI, message.reactions)
        if reaction is None:
            return []
        users: list[discord.Member] = []
        async for user in reaction.users(limit=None):
            if user.bot:
                continue
            member = message.guild.get_member(user.id) if message.guild else None
            if member is None:
                continue
            if required_role_id and all(role.id != required_role_id for role in member.roles):
                continue
            users.append(member)
        return users

    async def end_giveaway(self, message_id: str, *, reroll: bool = False) -> str:
        data = load_data()
        entry = data.get("giveaways", {}).get(str(message_id))
        if not isinstance(entry, dict):
            return "I could not find that giveaway."
        channel = self.bot.get_channel(int(entry.get("channel_id", 0)))
        if not isinstance(channel, discord.TextChannel):
            return "The giveaway channel no longer exists."
        try:
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return "I could not access that giveaway message."
        users = await self.eligible_users(message, entry.get("required_role_id"))
        count = min(max(1, int(entry.get("winner_count", 1))), len(users))
        winners = random.sample(users, count) if count else []
        winner_text = ", ".join(user.mention for user in winners) if winners else "No valid entries"
        if reroll:
            await channel.send(f"🎉 **Rerolled {entry.get('prize', 'giveaway')}!** Winner(s): {winner_text}")
            return f"Rerolled the giveaway. Winner(s): {winner_text}"
        entry["ended"] = True
        entry["winners"] = [user.id for user in winners]
        entry["ended_at"] = int(datetime.now(UTC).timestamp())
        async with self.data_lock:
            current = load_data()
            current.setdefault("giveaways", {})[str(message_id)] = entry
            await self.save(current)
        embed = message.embeds[0].copy() if message.embeds else discord.Embed(title=str(entry.get("prize", "Giveaway")))
        embed.color = discord.Color.dark_grey()
        embed.description = f"**Ended**\n**Winner(s):** {winner_text}\n**Hosted by:** <@{entry.get('host_id')}>"
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass
        await channel.send(f"🎉 **{entry.get('prize', 'Giveaway')} has ended!** Winner(s): {winner_text}")
        return f"Ended the giveaway. Winner(s): {winner_text}"

    @app_commands.command(name="gcreate", description="Create a reaction giveaway")
    @app_commands.describe(duration="Examples: 10m, 2h, 1d", prize="What the winner receives")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gcreate(
        self,
        interaction: discord.Interaction,
        duration: str,
        prize: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        channel: discord.TextChannel | None = None,
        required_role: discord.Role | None = None,
    ) -> None:
        try:
            seconds = parse_duration(duration)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("Choose a normal text channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        message = await self.create_giveaway(target, interaction.user.id, prize, seconds, winners, required_role.id if required_role else None)
        await interaction.followup.send(f"Giveaway created: {message.jump_url}", ephemeral=True)

    @app_commands.command(name="gend", description="End a giveaway now")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gend(self, interaction: discord.Interaction, message_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.end_giveaway(message_id), ephemeral=True)

    @app_commands.command(name="greroll", description="Choose new winner(s) for an ended giveaway")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def greroll(self, interaction: discord.Interaction, message_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.end_giveaway(message_id, reroll=True), ephemeral=True)

    @app_commands.command(name="autogcreate", description="Schedule giveaways to repeat automatically")
    @app_commands.describe(interval="Time between giveaways", giveaway_duration="How long each giveaway stays open")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def autogcreate(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        interval: str,
        giveaway_duration: str,
        prize: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        required_role: discord.Role | None = None,
    ) -> None:
        try:
            interval_seconds = parse_duration(interval)
            duration_seconds = parse_duration(giveaway_duration)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        schedule_id = uuid.uuid4().hex[:8]
        async with self.data_lock:
            data = load_data()
            data["auto"][schedule_id] = {
                "guild_id": interaction.guild_id,
                "channel_id": channel.id,
                "host_id": interaction.user.id,
                "interval_seconds": interval_seconds,
                "duration_seconds": duration_seconds,
                "prize": prize[:250],
                "winner_count": winners,
                "required_role_id": required_role.id if required_role else None,
                "next_at": int(datetime.now(UTC).timestamp()),
                "active": True,
            }
            await self.save(data)
        await interaction.response.send_message(f"Auto giveaway schedule `{schedule_id}` saved. The first one will post shortly.", ephemeral=True)

    @app_commands.command(name="autoglist", description="List automatic giveaway schedules")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def autoglist(self, interaction: discord.Interaction) -> None:
        schedules = load_data().get("auto", {})
        lines = [
            f"`{key}` — **{entry.get('prize')}** in <#{entry.get('channel_id')}> · next <t:{entry.get('next_at')}:R>"
            for key, entry in schedules.items()
            if isinstance(entry, dict) and entry.get("active") and entry.get("guild_id") == interaction.guild_id
        ]
        await interaction.response.send_message("\n".join(lines) or "No automatic giveaways are active.", ephemeral=True)

    @app_commands.command(name="autogstop", description="Stop an automatic giveaway schedule")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def autogstop(self, interaction: discord.Interaction, schedule_id: str) -> None:
        async with self.data_lock:
            data = load_data()
            entry = data.get("auto", {}).get(schedule_id)
            if not isinstance(entry, dict) or entry.get("guild_id") != interaction.guild_id:
                await interaction.response.send_message("That schedule was not found.", ephemeral=True)
                return
            entry["active"] = False
            await self.save(data)
        await interaction.response.send_message(f"Stopped auto giveaway `{schedule_id}`.", ephemeral=True)

    @tasks.loop(seconds=15)
    async def giveaway_loop(self) -> None:
        now = int(datetime.now(UTC).timestamp())
        data = load_data()
        due_giveaways = [key for key, entry in data.get("giveaways", {}).items() if isinstance(entry, dict) and not entry.get("ended") and int(entry.get("ends_at", 0)) <= now]
        for message_id in due_giveaways:
            try:
                await self.end_giveaway(message_id)
            except Exception:
                log.exception("Could not end giveaway %s", message_id)
        data = load_data()
        for schedule_id, entry in list(data.get("auto", {}).items()):
            if not isinstance(entry, dict) or not entry.get("active") or int(entry.get("next_at", 0)) > now:
                continue
            channel = self.bot.get_channel(int(entry.get("channel_id", 0)))
            if isinstance(channel, discord.TextChannel):
                try:
                    await self.create_giveaway(
                        channel,
                        int(entry.get("host_id", 0)),
                        str(entry.get("prize", "Giveaway")),
                        int(entry.get("duration_seconds", 3600)),
                        int(entry.get("winner_count", 1)),
                        entry.get("required_role_id"),
                        schedule_id,
                    )
                except discord.HTTPException:
                    log.exception("Could not post automatic giveaway %s", schedule_id)
            async with self.data_lock:
                current = load_data()
                current_entry = current.get("auto", {}).get(schedule_id)
                if isinstance(current_entry, dict):
                    current_entry["next_at"] = now + max(10, int(current_entry.get("interval_seconds", 3600)))
                    await self.save(current)

    @giveaway_loop.before_loop
    async def before_giveaway_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaways(bot))

