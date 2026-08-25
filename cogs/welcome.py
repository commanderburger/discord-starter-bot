import logging
import os

import discord
from discord.ext import commands

from cogs.permissions import normalise_role_name


log = logging.getLogger("starter-bot.welcome")
WELCOME_CHANNEL_NAMES = os.getenv("WELCOME_CHANNEL_NAMES", "welcome,welcom")
SERVER_NAME = os.getenv("WELCOME_SERVER_NAME", "Density SMP")


def ordinal(value: int) -> str:
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def welcome_channel(guild: discord.Guild) -> discord.TextChannel | None:
    wanted = {
        normalise_role_name(name)
        for name in WELCOME_CHANNEL_NAMES.split(",")
        if normalise_role_name(name)
    }
    return discord.utils.find(
        lambda channel: isinstance(channel, discord.TextChannel)
        and normalise_role_name(channel.name) in wanted,
        guild.channels,
    )


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        channel = welcome_channel(member.guild)
        if channel is None:
            log.warning("No welcome channel found in %s", member.guild.name)
            return
        member_number = member.guild.member_count or len(member.guild.members)
        try:
            await channel.send(
                f"Welcome {member.mention} to **{SERVER_NAME}**! "
                f"You are the **{ordinal(member_number)}** member!",
                allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
            )
        except discord.HTTPException:
            log.exception("Could not welcome %s", member)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
