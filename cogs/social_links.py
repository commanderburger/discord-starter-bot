import asyncio
import json
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

from cogs.permissions import normalise_role_name


log = logging.getLogger("starter-bot.social-links")
DATA_FILE = Path(os.getenv("BOT_DATA_DIR", "/data")) / "social-links-posted.json"
LINKS_CHANNEL_NAME = os.getenv("SOCIAL_LINKS_CHANNEL", "links")
POST_ID = "density-social-links-2026-08-25-v1"
SOCIAL_LINKS = (
    "https://www.youtube.com/@DensitySMP_",
    "https://www.tiktok.com/@_densitysmp_?_r=1&_t=ZG-98t7QECbcB6",
)


def posted_guilds() -> set[str]:
    try:
        value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    posted = value.get("posted", []) if isinstance(value, dict) else []
    return {str(item) for item in posted}


def save_posted_guilds(posted: set[str]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"posted": sorted(posted)}, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, DATA_FILE)
    try:
        DATA_FILE.chmod(0o600)
    except OSError:
        pass


class SocialLinks(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ready_lock = asyncio.Lock()
        self.checked = False

    def links_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        wanted = normalise_role_name(LINKS_CHANNEL_NAME)
        return discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel)
            and normalise_role_name(channel.name) == wanted,
            guild.channels,
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with self.ready_lock:
            if self.checked:
                return
            self.checked = True
            posted = posted_guilds()
            for guild in self.bot.guilds:
                post_key = f"{guild.id}:{POST_ID}"
                if post_key in posted:
                    continue
                channel = self.links_channel(guild)
                if channel is None:
                    log.warning("No #%s channel found in %s", LINKS_CHANNEL_NAME, guild.name)
                    continue
                try:
                    await channel.send(
                        "**Density SMP Socials**",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    for link in SOCIAL_LINKS:
                        await channel.send(
                            link,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                except discord.HTTPException:
                    log.exception("Could not post social links in #%s", channel.name)
                    continue
                posted.add(post_key)
                try:
                    save_posted_guilds(posted)
                except OSError:
                    log.exception("Could not save social-link posting state")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SocialLinks(bot))
