import asyncio
import json
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

from cogs.permissions import normalise_role_name


log = logging.getLogger("starter-bot.updates")
DATA_FILE = Path(os.getenv("BOT_DATA_DIR", "/data")) / "release-announcements.json"
UPDATES_CHANNEL_NAME = os.getenv("BOT_UPDATES_CHANNEL", "bot-updates")
RELEASE_ID = "2026-08-28-partner-manager-applications-v17"


def load_announced_releases() -> set[str]:
    try:
        value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    releases = value.get("announced", []) if isinstance(value, dict) else []
    return {str(release) for release in releases}


def save_announced_releases(releases: set[str]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"announced": sorted(releases)}, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, DATA_FILE)
    try:
        DATA_FILE.chmod(0o600)
    except OSError:
        pass


class Updates(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ready_lock = asyncio.Lock()
        self.checked = False

    def updates_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        wanted = normalise_role_name(UPDATES_CHANNEL_NAME)
        return discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel)
            and normalise_role_name(channel.name) == wanted,
            guild.channels,
        )

    async def ensure_updates_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel = self.updates_channel(guild)
        if channel:
            return channel
        try:
            return await guild.create_text_channel(
                UPDATES_CHANNEL_NAME,
                reason="Density Bot release updates",
            )
        except discord.Forbidden:
            log.warning("Missing permission to create #%s in %s", UPDATES_CHANNEL_NAME, guild.name)
        except discord.HTTPException:
            log.exception("Could not create #%s in %s", UPDATES_CHANNEL_NAME, guild.name)
        return None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with self.ready_lock:
            if self.checked:
                return
            self.checked = True
            announced = load_announced_releases()
            for guild in self.bot.guilds:
                channel = await self.ensure_updates_channel(guild)
                release_key = f"{guild.id}:{RELEASE_ID}"
                if channel is None or release_key in announced:
                    continue
                embed = discord.Embed(
                    title="Density Bot Update",
                    description=(
                        "• Added a **Partner Manager application** panel.\n"
                        "• Applicants answer one question at a time in private DMs.\n"
                        "• Applications ask for previous-server experience, server links, and availability for five partnerships weekly.\n"
                        "• Completed applications go to the private pending review channel.\n"
                        "• Owner, Co-Owner and Manager can accept or deny with review buttons.\n"
                        "• Accepted applicants automatically receive Partner Manager and Staff Team.\n"
                        "• Denied applicants are notified and cannot reapply for 14 days."
                    ),
                    color=discord.Color.blurple(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_footer(text="Density SMP • Bot update")
                try:
                    await channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.exception("Could not post the update in #%s", channel.name)
                    continue
                announced.add(release_key)
                try:
                    save_announced_releases(announced)
                except OSError:
                    log.exception("Could not save release announcement state")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Updates(bot))
