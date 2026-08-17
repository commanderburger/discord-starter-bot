import logging
import os
import re

import discord
from discord.ext import commands


log = logging.getLogger("starter-bot.moderation")

DISCORD_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg)/[a-z0-9-]+",
    re.IGNORECASE,
)


def normalise_channel_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        configured = os.getenv(
            "INVITE_EXEMPT_CHANNELS",
            "partners,announcements,our-ad",
        )
        self.exempt_channels = {
            normalise_channel_name(name)
            for name in configured.split(",")
            if name.strip()
        }

    def channel_is_exempt(self, channel: discord.abc.GuildChannel | discord.Thread) -> bool:
        names = {normalise_channel_name(channel.name)}
        if isinstance(channel, discord.Thread) and channel.parent:
            names.add(normalise_channel_name(channel.parent.name))
        return bool(names & self.exempt_channels)

    async def check_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot or not message.content:
            return
        if self.channel_is_exempt(message.channel):
            return
        if not DISCORD_INVITE_RE.search(message.content):
            return

        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, Discord invite links are only allowed in "
                "the approved advert channels.",
                delete_after=8,
            )
            log.info(
                "Removed an invite posted by %s (%s) in #%s",
                message.author,
                message.author.id,
                message.channel,
            )
        except discord.Forbidden:
            log.warning("Could not delete an invite in #%s: Manage Messages is missing", message.channel)
        except discord.HTTPException:
            log.exception("Discord rejected an invite moderation action")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.check_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content != after.content:
            await self.check_message(after)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
