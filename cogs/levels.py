import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from cogs.permissions import normalise_role_name


log = logging.getLogger("starter-bot.levels")
DATA_FILE = Path(os.getenv("BOT_DATA_DIR", "/data")) / "levels.json"
RANK_CHANNEL_NAME = os.getenv("SPECIAL_RANKS_CHANNEL", "special-ranks")
IGNORED_CHANNEL_NAMES = os.getenv("LEVEL_IGNORED_CHANNELS", "special-ranks")
XP_COOLDOWN_SECONDS = max(5.0, float(os.getenv("LEVEL_XP_COOLDOWN", "45")))
XP_MIN = max(1, int(os.getenv("LEVEL_XP_MIN", "15")))
XP_MAX = max(XP_MIN, int(os.getenv("LEVEL_XP_MAX", "25")))


def xp_needed(level: int) -> int:
    """XP required to move from this level to the next level."""

    return 5 * level * level + 50 * level + 100


def empty_data() -> dict[str, dict]:
    return {"guilds": {}}


def load_data() -> dict[str, dict]:
    try:
        value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty_data()
    if not isinstance(value, dict) or not isinstance(value.get("guilds"), dict):
        return empty_data()
    return value


def save_data(value: dict[str, dict]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, DATA_FILE)
    try:
        DATA_FILE.chmod(0o600)
    except OSError:
        pass


def ignored_channel_names() -> set[str]:
    return {
        normalise_role_name(name)
        for name in IGNORED_CHANNEL_NAMES.split(",")
        if normalise_role_name(name)
    }


class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.data = load_data()
        self.data_lock = asyncio.Lock()
        self.channel_lock = asyncio.Lock()
        self.last_xp_at: dict[tuple[int, int], float] = {}
        self.channels_checked = False

    def rank_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        wanted = normalise_role_name(RANK_CHANNEL_NAME)
        return discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel)
            and normalise_role_name(channel.name) == wanted,
            guild.channels,
        )

    async def ensure_rank_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        existing = self.rank_channel(guild)
        if existing:
            return existing
        async with self.channel_lock:
            existing = self.rank_channel(guild)
            if existing:
                return existing
            try:
                return await guild.create_text_channel(
                    RANK_CHANNEL_NAME,
                    reason="Density Bot level-up announcements",
                )
            except discord.Forbidden:
                log.warning("Missing permission to create #%s in %s", RANK_CHANNEL_NAME, guild.name)
            except discord.HTTPException:
                log.exception("Could not create #%s in %s", RANK_CHANNEL_NAME, guild.name)
        return None

    def user_record(self, guild_id: int, user_id: int) -> dict[str, int]:
        guilds = self.data.setdefault("guilds", {})
        guild = guilds.setdefault(str(guild_id), {"users": {}})
        users = guild.setdefault("users", {})
        record = users.setdefault(str(user_id), {"level": 0, "xp": 0, "messages": 0})
        return record

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.channels_checked:
            return
        self.channels_checked = True
        for guild in self.bot.guilds:
            await self.ensure_rank_channel(guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            message.guild is None
            or message.author.bot
            or not isinstance(message.author, discord.Member)
            or not isinstance(message.channel, discord.TextChannel)
        ):
            return
        if normalise_role_name(message.channel.name) in ignored_channel_names():
            return
        if not message.content.strip() and not message.attachments:
            return

        cooldown_key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self.last_xp_at.get(cooldown_key, 0.0) < XP_COOLDOWN_SECONDS:
            return
        self.last_xp_at[cooldown_key] = now

        level_ups: list[int] = []
        async with self.data_lock:
            record = self.user_record(message.guild.id, message.author.id)
            record["messages"] = int(record.get("messages", 0)) + 1
            record["xp"] = int(record.get("xp", 0)) + XP_MIN + secrets.randbelow(XP_MAX - XP_MIN + 1)
            record["level"] = int(record.get("level", 0))
            while record["xp"] >= xp_needed(record["level"]):
                record["xp"] -= xp_needed(record["level"])
                record["level"] += 1
                level_ups.append(record["level"])
            try:
                save_data(self.data)
            except OSError:
                log.exception("Could not save level data")

        if not level_ups:
            return
        channel = await self.ensure_rank_channel(message.guild)
        if channel is None:
            return
        try:
            await channel.send(
                f"🎉 {message.author.mention} has reached **level {level_ups[-1]}**. GG!",
                allowed_mentions=discord.AllowedMentions(
                    users=[message.author],
                    roles=False,
                    everyone=False,
                ),
            )
        except discord.HTTPException:
            log.exception("Could not post a level-up for %s", message.author)

    @app_commands.command(name="rank", description="Show your or another member's chat level")
    @app_commands.describe(member="Member whose rank you want to view")
    async def rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in the server.", ephemeral=True)
            return
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("I could not find that member.", ephemeral=True)
            return
        async with self.data_lock:
            record = self.user_record(interaction.guild.id, target.id).copy()
        level = int(record.get("level", 0))
        xp = int(record.get("xp", 0))
        required = xp_needed(level)
        progress = min(10, int((xp / required) * 10)) if required else 10
        bar = "█" * progress + "░" * (10 - progress)
        embed = discord.Embed(
            title=f"{target.display_name}'s Rank",
            description=(
                f"**Level:** {level}\n"
                f"**XP:** {xp}/{required}\n"
                f"`{bar}`\n"
                f"**XP messages:** {int(record.get('messages', 0))}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Show the server chat-level leaderboard")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in the server.", ephemeral=True)
            return
        async with self.data_lock:
            guild_data = self.data.get("guilds", {}).get(str(interaction.guild.id), {})
            users = guild_data.get("users", {}) if isinstance(guild_data, dict) else {}
            ranked = sorted(
                users.items(),
                key=lambda item: (
                    int(item[1].get("level", 0)),
                    int(item[1].get("xp", 0)),
                ),
                reverse=True,
            )[:10]
        lines = []
        for position, (user_id, record) in enumerate(ranked, start=1):
            member = interaction.guild.get_member(int(user_id))
            name = member.mention if member else f"User `{user_id}`"
            lines.append(
                f"**{position}.** {name} — Level **{int(record.get('level', 0))}** "
                f"({int(record.get('xp', 0))} XP)"
            )
        embed = discord.Embed(
            title="Density SMP Level Leaderboard",
            description="\n".join(lines) or "Nobody has earned chat XP yet.",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Levels(bot))
