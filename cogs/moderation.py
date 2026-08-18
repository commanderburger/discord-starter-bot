import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


log = logging.getLogger("starter-bot.moderation")
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "/data"))
WARNINGS_FILE = DATA_DIR / "moderation.json"
MAX_TIMEOUT = timedelta(days=28)
DISCORD_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg)/[a-z0-9-]+",
    re.IGNORECASE,
)
DURATION_RE = re.compile(r"^(\d+)([smhdw])$", re.IGNORECASE)


def normalise_channel_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def parse_duration(value: str) -> timedelta:
    match = DURATION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("Use a duration such as `30m`, `2h`, `3d`, or `1w`.")
    amount = int(match.group(1))
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2).lower()]
    duration = timedelta(seconds=seconds)
    if duration <= timedelta(0) or duration > MAX_TIMEOUT:
        raise ValueError("Discord timeouts must be between 1 second and 28 days.")
    return duration


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
        file.write("\n")
    temporary.replace(path)


def load_warnings() -> dict:
    try:
        with WARNINGS_FILE.open(encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.warning_lock = asyncio.Lock()
        configured = os.getenv("INVITE_EXEMPT_CHANNELS", "partners,announcements,our-ad")
        self.exempt_channels = {
            normalise_channel_name(name) for name in configured.split(",") if name.strip()
        }

    def channel_is_exempt(self, channel: discord.abc.GuildChannel | discord.Thread) -> bool:
        names = {normalise_channel_name(channel.name)}
        if isinstance(channel, discord.Thread) and channel.parent:
            names.add(normalise_channel_name(channel.parent.name))
        return bool(names & self.exempt_channels)

    async def check_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or not message.content:
            return
        if self.channel_is_exempt(message.channel) or not DISCORD_INVITE_RE.search(message.content):
            return
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, Discord invite links are only allowed in the approved advert channels.",
                delete_after=8,
            )
            await self.send_mod_log(
                message.guild,
                "Invite removed",
                f"{message.author.mention} posted an invite in {message.channel.mention}.",
            )
        except discord.Forbidden:
            log.warning("Could not delete an invite in #%s: Manage Messages is missing", message.channel)
        except discord.HTTPException:
            log.exception("Discord rejected an invite moderation action")

    async def send_mod_log(self, guild: discord.Guild, title: str, description: str) -> None:
        configured = os.getenv("MOD_LOG_CHANNEL", "").strip()
        channel = guild.get_channel(int(configured)) if configured.isdigit() else None
        if channel is None:
            channel = discord.utils.find(
                lambda item: isinstance(item, discord.TextChannel)
                and normalise_channel_name(item.name) in {"modlogs", "moderationlogs"},
                guild.channels,
            )
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    embed=discord.Embed(title=title, description=description, color=discord.Color.orange())
                )
            except discord.HTTPException:
                log.warning("Could not send a moderation log message")

    @staticmethod
    def target_allowed(interaction: discord.Interaction, member: discord.Member) -> str | None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return "This command can only be used in the server."
        if member.id == interaction.user.id:
            return "You cannot use that moderation action on yourself."
        if member.id == interaction.guild.owner_id:
            return "The server owner cannot be moderated."
        if interaction.user.id != interaction.guild.owner_id and member.top_role >= interaction.user.top_role:
            return "That member has an equal or higher role than you."
        bot_member = interaction.guild.me
        if bot_member and member.top_role >= bot_member.top_role:
            return "Move the bot role above that member's role first."
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.check_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content != after.content:
            await self.check_message(after)

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        if error := self.target_allowed(interaction, member):
            await interaction.response.send_message(error, ephemeral=True)
            return
        await member.ban(reason=f"{reason} — by {interaction.user}")
        await interaction.response.send_message(f"Banned {member.mention}. Reason: {reason}")
        await self.send_mod_log(interaction.guild, "Member banned", f"{member} was banned by {interaction.user.mention}.\nReason: {reason}")

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        if error := self.target_allowed(interaction, member):
            await interaction.response.send_message(error, ephemeral=True)
            return
        await member.kick(reason=f"{reason} — by {interaction.user}")
        await interaction.response.send_message(f"Kicked **{member}**. Reason: {reason}")
        await self.send_mod_log(interaction.guild, "Member kicked", f"{member} was kicked by {interaction.user.mention}.\nReason: {reason}")

    @app_commands.command(name="mute", description="Mute a member for 24 hours")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def mute(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided") -> None:
        await self.apply_timeout(interaction, member, timedelta(hours=24), reason)

    @app_commands.command(name="tempmute", description="Mute a member for a chosen time")
    @app_commands.describe(duration="Examples: 30m, 2h, 3d, 1w")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def tempmute(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided") -> None:
        try:
            parsed = parse_duration(duration)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await self.apply_timeout(interaction, member, parsed, reason)

    async def apply_timeout(self, interaction: discord.Interaction, member: discord.Member, duration: timedelta, reason: str) -> None:
        if error := self.target_allowed(interaction, member):
            await interaction.response.send_message(error, ephemeral=True)
            return
        until = datetime.now(UTC) + duration
        await member.timeout(until, reason=f"{reason} — by {interaction.user}")
        await interaction.response.send_message(f"Muted {member.mention} until <t:{int(until.timestamp())}:R>. Reason: {reason}")
        await self.send_mod_log(interaction.guild, "Member muted", f"{member.mention} was muted by {interaction.user.mention} until <t:{int(until.timestamp())}:F>.\nReason: {reason}")

    @app_commands.command(name="unmute", description="Remove a member's timeout")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Mute removed") -> None:
        if error := self.target_allowed(interaction, member):
            await interaction.response.send_message(error, ephemeral=True)
            return
        await member.timeout(None, reason=f"{reason} — by {interaction.user}")
        await interaction.response.send_message(f"Unmuted {member.mention}.")
        await self.send_mod_log(interaction.guild, "Member unmuted", f"{member.mention} was unmuted by {interaction.user.mention}.\nReason: {reason}")

    @app_commands.command(name="purge", description="Delete recent messages from this channel")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use this in a normal text channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Deleted {len(deleted)} message(s).", ephemeral=True)

    @app_commands.command(name="warn", description="Record a warning for a member")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        if error := self.target_allowed(interaction, member):
            await interaction.response.send_message(error, ephemeral=True)
            return
        async with self.warning_lock:
            data = load_warnings()
            entries = data.setdefault(str(interaction.guild_id), {}).setdefault(str(member.id), [])
            entries.append({"reason": reason[:500], "moderator_id": interaction.user.id, "created_at": datetime.now(UTC).isoformat()})
            await asyncio.to_thread(atomic_write_json, WARNINGS_FILE, data)
        await interaction.response.send_message(f"Warned {member.mention}. They now have **{len(entries)}** warning(s).", ephemeral=True)
        await self.send_mod_log(interaction.guild, "Member warned", f"{member.mention} was warned by {interaction.user.mention}.\nReason: {reason}")

    @app_commands.command(name="warnings", description="Show a member's recorded warnings")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member) -> None:
        entries = load_warnings().get(str(interaction.guild_id), {}).get(str(member.id), [])
        lines = [f"**{index}.** {entry.get('reason', 'No reason')} — <@{entry.get('moderator_id', 0)}>" for index, entry in enumerate(entries[-10:], 1)]
        embed = discord.Embed(title=f"Warnings for {member}", description="\n".join(lines) or "No warnings.", color=discord.Color.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Clear all recorded warnings for a member")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member) -> None:
        async with self.warning_lock:
            data = load_warnings()
            removed = len(data.get(str(interaction.guild_id), {}).pop(str(member.id), []))
            await asyncio.to_thread(atomic_write_json, WARNINGS_FILE, data)
        await interaction.response.send_message(f"Cleared **{removed}** warning(s) for {member.mention}.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You do not have permission to use that moderation command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = "The bot is missing the required Discord permission."
        elif isinstance(error, discord.Forbidden):
            message = "Discord blocked that action. Check the bot role and permissions."
        else:
            log.exception("Moderation command failed", exc_info=error)
            message = f"That action failed: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))

