import asyncio
import json
import logging
import os
import random
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.permissions import StaffOnly, member_is_staff, normalise_role_name, staff_only


log = logging.getLogger("starter-bot.giveaways")
DATA_FILE = Path(os.getenv("BOT_DATA_DIR", "/data")) / "giveaways.json"
DURATION_RE = re.compile(r"^(\d+)([smhdw])$", re.IGNORECASE)
ENTRY_EMOJI = "🎉"
GIVEAWAY_PING_ROLE = os.getenv("GIVEAWAY_PING_ROLE", "giveaway ping")


def parse_duration(value: str) -> int:
    match = DURATION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("Use a time such as `10m`, `2h`, `1d`, or `1w`.")
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2).lower()]
    if seconds < 10 or seconds > 31_536_000:
        raise ValueError("The time must be between 10 seconds and 1 year.")
    return seconds


def parse_winner_count(value: str) -> int:
    try:
        winners = int(value.strip())
    except ValueError as error:
        raise ValueError("Winners must be a whole number from 1 to 20.") from error
    if not 1 <= winners <= 20:
        raise ValueError("Winners must be a whole number from 1 to 20.")
    return winners


def resolve_text_channel(
    interaction: discord.Interaction,
    value: str,
) -> discord.TextChannel | None:
    if interaction.guild is None:
        return None
    raw = value.strip()
    if not raw:
        return interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None

    mention = re.fullmatch(r"<#(\d+)>", raw)
    channel_id = int(mention.group(1)) if mention else int(raw) if raw.isdigit() else None
    if channel_id:
        channel = interaction.guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    wanted = normalise_role_name(raw.removeprefix("#"))
    return discord.utils.find(
        lambda channel: isinstance(channel, discord.TextChannel)
        and normalise_role_name(channel.name) == wanted,
        interaction.guild.channels,
    )


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


class GiveawayCreateModal(discord.ui.Modal, title="Create a giveaway"):
    prize = discord.ui.TextInput(
        label="Prize",
        placeholder="What will the winner receive?",
        min_length=1,
        max_length=250,
    )
    duration = discord.ui.TextInput(
        label="Duration",
        placeholder="Examples: 10m, 2h, 1d",
        min_length=2,
        max_length=20,
    )
    winners = discord.ui.TextInput(
        label="Number of winners",
        default="1",
        min_length=1,
        max_length=2,
    )
    channel = discord.ui.TextInput(
        label="Channel (optional)",
        placeholder="#giveaways, a channel ID, or leave blank for here",
        required=False,
        max_length=100,
    )

    def __init__(self, cog: "Giveaways") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not member_is_staff(interaction, "manage_guild"):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return
        try:
            seconds = parse_duration(str(self.duration))
            winner_count = parse_winner_count(str(self.winners))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        target = resolve_text_channel(interaction, str(self.channel))
        if target is None:
            await interaction.response.send_message(
                "I could not find that text channel. Use a channel mention, ID or exact name.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        message, pinged = await self.cog.create_giveaway(
            target,
            interaction.user.id,
            str(self.prize),
            seconds,
            winner_count,
        )
        note = "" if pinged else f" I could not find the `{GIVEAWAY_PING_ROLE}` role to ping."
        await interaction.followup.send(
            f"Giveaway created: {message.jump_url}.{note}",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Giveaway form failed", exc_info=error)
        message = "I could not create that giveaway. Check my channel permissions and try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class AutoGiveawayCreateModal(discord.ui.Modal, title="Create an automatic giveaway"):
    prize = discord.ui.TextInput(
        label="Prize",
        placeholder="What will each winner receive?",
        min_length=1,
        max_length=250,
    )
    interval = discord.ui.TextInput(
        label="Time between giveaways",
        placeholder="Examples: 12h, 1d, 1w",
        min_length=2,
        max_length=20,
    )
    giveaway_duration = discord.ui.TextInput(
        label="How long each giveaway runs",
        placeholder="Examples: 30m, 2h, 1d",
        min_length=2,
        max_length=20,
    )
    winners = discord.ui.TextInput(
        label="Number of winners",
        default="1",
        min_length=1,
        max_length=2,
    )
    channel = discord.ui.TextInput(
        label="Channel (optional)",
        placeholder="#giveaways, a channel ID, or leave blank for here",
        required=False,
        max_length=100,
    )

    def __init__(self, cog: "Giveaways") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not member_is_staff(interaction, "manage_guild"):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return
        try:
            interval_seconds = parse_duration(str(self.interval))
            duration_seconds = parse_duration(str(self.giveaway_duration))
            winner_count = parse_winner_count(str(self.winners))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        target = resolve_text_channel(interaction, str(self.channel))
        if target is None:
            await interaction.response.send_message(
                "I could not find that text channel. Use a channel mention, ID or exact name.",
                ephemeral=True,
            )
            return
        schedule_id = uuid.uuid4().hex[:8]
        await self.cog.save_auto_schedule(
            schedule_id=schedule_id,
            guild_id=interaction.guild_id,
            channel_id=target.id,
            host_id=interaction.user.id,
            interval_seconds=interval_seconds,
            duration_seconds=duration_seconds,
            prize=str(self.prize),
            winner_count=winner_count,
        )
        await interaction.response.send_message(
            f"Auto giveaway schedule `{schedule_id}` saved. The first one will post shortly in {target.mention}.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Automatic giveaway form failed", exc_info=error)
        message = "I could not save that automatic giveaway. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


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
        schedule_id: str | None = None,
    ) -> tuple[discord.Message, bool]:
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
        ping_role = discord.utils.find(
            lambda role: normalise_role_name(role.name) == normalise_role_name(GIVEAWAY_PING_ROLE),
            channel.guild.roles,
        )
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[ping_role] if ping_role else False,
        )
        message = await channel.send(
            content=ping_role.mention if ping_role else None,
            embed=embed,
            allowed_mentions=allowed_mentions,
        )
        await message.add_reaction(ENTRY_EMOJI)
        async with self.data_lock:
            data = load_data()
            data["giveaways"][str(message.id)] = {
                "guild_id": channel.guild.id,
                "channel_id": channel.id,
                "host_id": host_id,
                "prize": prize[:250],
                "winner_count": winner_count,
                "ends_at": ends_at,
                "ended": False,
                "schedule_id": schedule_id,
            }
            await self.save(data)
        return message, ping_role is not None

    async def eligible_users(self, message: discord.Message) -> list[discord.Member]:
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
        users = await self.eligible_users(message)
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
    @staff_only("manage_guild")
    async def gcreate(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(GiveawayCreateModal(self))

    @app_commands.command(name="gend", description="End a giveaway now")
    @staff_only("manage_guild")
    async def gend(self, interaction: discord.Interaction, message_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.end_giveaway(message_id), ephemeral=True)

    @app_commands.command(name="greroll", description="Choose new winner(s) for an ended giveaway")
    @staff_only("manage_guild")
    async def greroll(self, interaction: discord.Interaction, message_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.end_giveaway(message_id, reroll=True), ephemeral=True)

    @app_commands.command(name="autogcreate", description="Schedule giveaways to repeat automatically")
    @staff_only("manage_guild")
    async def autogcreate(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AutoGiveawayCreateModal(self))

    async def save_auto_schedule(
        self,
        *,
        schedule_id: str,
        guild_id: int | None,
        channel_id: int,
        host_id: int,
        interval_seconds: int,
        duration_seconds: int,
        prize: str,
        winner_count: int,
    ) -> None:
        async with self.data_lock:
            data = load_data()
            data["auto"][schedule_id] = {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "host_id": host_id,
                "interval_seconds": interval_seconds,
                "duration_seconds": duration_seconds,
                "prize": prize[:250],
                "winner_count": winner_count,
                "next_at": int(datetime.now(UTC).timestamp()),
                "active": True,
            }
            await self.save(data)

    @app_commands.command(name="autoglist", description="List automatic giveaway schedules")
    @staff_only("manage_guild")
    async def autoglist(self, interaction: discord.Interaction) -> None:
        schedules = load_data().get("auto", {})
        lines = [
            f"`{key}` — **{entry.get('prize')}** in <#{entry.get('channel_id')}> · next <t:{entry.get('next_at')}:R>"
            for key, entry in schedules.items()
            if isinstance(entry, dict) and entry.get("active") and entry.get("guild_id") == interaction.guild_id
        ]
        await interaction.response.send_message("\n".join(lines) or "No automatic giveaways are active.", ephemeral=True)

    @app_commands.command(name="autogstop", description="Stop an automatic giveaway schedule")
    @staff_only("manage_guild")
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

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, StaffOnly):
            message = "This command is for staff only."
        elif isinstance(error, discord.Forbidden):
            message = "Discord blocked that action. Check the bot role and channel permissions."
        else:
            log.exception("Giveaway command failed", exc_info=error)
            message = f"That giveaway action failed: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

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
