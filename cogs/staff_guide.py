import asyncio
import logging
import os

import discord
from discord.ext import commands

from cogs.permissions import normalise_role_name, role_is_staff


log = logging.getLogger("starter-bot.staff-guide")
CHANNEL_NAME = os.getenv("STAFF_COMMANDS_CHANNEL", "staff-commands")
GUIDE_MARKER = "Density Bot Staff Guide v13"


def staff_overwrites(guild: discord.Guild) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    for role in guild.roles:
        if role_is_staff(role):
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                embed_links=True,
                attach_files=True,
            )
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            manage_channels=True,
            embed_links=True,
            attach_files=True,
        )
    return overwrites


def guide_embeds() -> list[discord.Embed]:
    first = discord.Embed(
        title="Density Bot — Staff Commands",
        description=(
            "Use `!command` in the server. Commands marked **Senior** are restricted to "
            "Manager, Co Owner, Owner, or the Discord server owner. Arguments in `[brackets]` "
            "are optional."
        ),
        color=discord.Color.blurple(),
    )
    first.add_field(
        name="Panels & tickets",
        value=(
            "`!welcome-test` — preview the welcome message\n"
            "`!refresh` — refresh panels, guide, queues, and saved displays (**Senior**)\n"
            "`!claim [builder_ign]` — claim the current ticket\n"
            "`!close` — open the ticket close dashboard\n"
            "`!req close` — request the other side to confirm closing\n"
            "`!unclaim` — release the current ticket\n"
            "`!rename name` — rename the ticket and award one builder point\n"
            "`!ptrack` — create/open payment tracking for this ticket\n"
            "`!payment track 125m` — set or update the tracked payment\n"
            "`!build cancel` — cancel your unclaimed build/digout request"
        ),
        inline=False,
    )
    first.add_field(
        name="Leaderboards & staff records",
        value=(
            "`!leaderboard` / `!lb` — builder points leaderboard\n"
            "`!msglb` — message leaderboard\n"
            "`!strikelb` — strike leaderboard (**Senior**)\n"
            "`!strike @user reason` — add a staff strike (**Senior**)\n"
            "`!unstrike @user reason` — remove the latest strike (**Senior**)\n"
            "`!clearstrikes @user` — clear all strikes (**Senior**)\n"
            "`/point remove user amount` — remove builder/staff points\n"
            "`!loa 3d reason` — submit a leave-of-absence request\n"
            "`!vouch @user reason` — vouch for someone\n"
            "`!vouch list @user` — list a member's vouches\n"
            "`/v remove user` — choose and remove a vouch"
        ),
        inline=False,
    )
    first.set_footer(text=GUIDE_MARKER)

    second = discord.Embed(title="Moderation & utilities", color=discord.Color.blurple())
    second.add_field(
        name="Member tools",
        value=(
            "`!afk reason` — set your AFK status\n"
            "`!membercount` — show the member count\n"
            "`!pfp @user_or_id` — show a profile picture\n"
            "`!banner @user_or_id` — show a profile banner\n"
            "`!stats IGN` — show DonutSMP player stats\n"
            "`!translate [language]` — translate the replied-to message (English by default)\n"
            "`!snipe` — show the last deleted message in this channel\n"
            "`!suggest suggestion` — send a suggestion"
        ),
        inline=False,
    )
    second.add_field(
        name="Channel & server moderation",
        value=(
            "`!lock` / `!unlock` — lock or unlock the current channel\n"
            "`!sticky message` / `!editsticky message` — set or edit the channel sticky\n"
            "`!unsticky` / `!removesticky` — remove the sticky\n"
            "`!purge 1000` — delete messages in batches and log the count\n"
            "`!to @user 10m reason` — timeout a member\n"
            "`!rto @user reason` — remove a timeout\n"
            "`!ban @user reason` / `!kick @user reason` — remove a member (**Senior**)\n"
            "`!role @user role_id_or_name` — toggle a role\n"
            "`!setnick @user nickname` — set/reset a nickname\n"
            "`!steal emoji` — add the supplied custom emoji\n"
            "`!manage` — open the staff/builder role dashboard (**Senior**)"
        ),
        inline=False,
    )
    second.set_footer(text=GUIDE_MARKER)

    third = discord.Embed(title="Embeds, giveaways & quickdrops", color=discord.Color.blurple())
    third.add_field(
        name="Embeds",
        value=(
            "`!ecreate` — open the embed creation dashboard\n"
            "`!eedit message_id` — open the embed edit dashboard\n"
            "`!edelete message_id` — delete a bot message by ID"
        ),
        inline=False,
    )
    third.add_field(
        name="Giveaways",
        value=(
            "`!gcreate` — open the giveaway dashboard\n"
            "`!autogcreate [HH:MM] repeat duration winners prize` — schedule repeating giveaways\n"
            "`!rautogcreate [job_id]` — list or remove automatic giveaway jobs\n"
            "`!gend message_id` — end a giveaway\n"
            "`!gedit message_id 10m 1 prize` — edit a giveaway\n"
            "`!greroll message_id` — reroll winners\n"
            "`!qcreate` — open the quickdrop dashboard\n"
            "`!qend message_id` — end a quickdrop\n"
            "`!qedit message_id 10m 1 prize` — edit a quickdrop\n"
            "`!qreroll message_id` — reroll a quickdrop\n\n"
            "Selected winners press **Claim Prize**, enter their Minecraft IGN, and receive a private claim ticket."
        ),
        inline=False,
    )
    third.add_field(
        name="Automatic staff activity",
        value=(
            "A check is posted in `#staff-activity` every three days. Eligible staff must react ✅ "
            "within 24 hours. Owner, Co Owner, and Manager are exempt. Missed checks are recorded "
            "as strikes in `#staff-punishments`."
        ),
        inline=False,
    )
    third.set_footer(text=GUIDE_MARKER)

    fourth = discord.Embed(title="Existing slash commands", color=discord.Color.blurple())
    fourth.add_field(
        name="Minecraft & economy",
        value=(
            "`/stats` — Minecraft server status · `!stats IGN` — DonutSMP player stats\n"
            "`/links` — server/community links\n"
            "`/ordering` — live item orders · `/calc` — spawner profit estimate\n"
            "`/spawner count` — server-wide real spawner totals\n"
            "`/sellprice` — live `/sell` value\n"
            "`/farm pickle` / `/farm bamboo` — farm profit calculators\n"
            "`/auction browse` / `/auction track` — listings and trading graph"
        ),
        inline=False,
    )
    fourth.add_field(
        name="Slash moderation, tickets & giveaways",
        value=(
            "`/ban` / `/kick` — senior removals\n"
            "`/mute` — permanent mute until `/unmute` · `/tempmute` — timed mute\n"
            "`/purge` — delete messages\n"
            "`/warn` / `/warnings` / `/clearwarnings` — warning records\n"
            "`/ticketsetup` — post a ticket panel (**Senior**)\n"
            "`/gcreate` / `/gend` / `/greroll` — giveaway controls\n"
            "`/autogcreate` / `/autoglist` / `/autogstop` — automatic giveaways"
        ),
        inline=False,
    )
    fourth.add_field(
        name="Application controls",
        value=(
            "In the high-staff channel, Owner, Co Owner, and Manager can use the two control buttons "
            "to pause or reopen Partner Manager and Helper applications separately."
        ),
        inline=False,
    )
    fourth.add_field(
        name="API, levels & general",
        value=(
            "`/api status` / `set-url` / `set-key` / `test` / `sync` — private Minecraft bridge controls\n"
            "`/rank` / `/leaderboard` — chat XP and level standings\n"
            "`/ping` / `/hello` / `/about` / `/help` — bot information\n"
            "`/coinflip` / `/roll` — fun tools\n"
            "`/avatar` / `/userinfo` / `/serverinfo` — Discord information"
        ),
        inline=False,
    )
    fourth.set_footer(text=GUIDE_MARKER)
    return [first, second, third, fourth]


class StaffGuide(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ready_lock = asyncio.Lock()
        self.checked = False

    def find_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        wanted = normalise_role_name(CHANNEL_NAME)
        return discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel)
            and normalise_role_name(channel.name) == wanted,
            guild.channels,
        )

    async def ensure_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel = self.find_channel(guild)
        if channel:
            return channel
        try:
            return await guild.create_text_channel(
                CHANNEL_NAME,
                overwrites=staff_overwrites(guild),
                reason="Density Bot staff command guide",
            )
        except discord.HTTPException:
            log.exception("Could not create #%s in %s", CHANNEL_NAME, guild.name)
            return None

    async def ensure_guide(self, guild: discord.Guild | None = None) -> list[discord.Message]:
        messages: list[discord.Message] = []
        guilds = [guild] if guild else list(self.bot.guilds)
        for current in guilds:
            channel = await self.ensure_channel(current)
            if channel is None:
                continue
            old_messages: list[discord.Message] = []
            try:
                async for message in channel.history(limit=50):
                    if message.author.id != self.bot.user.id:
                        continue
                    if any(embed.footer and embed.footer.text == GUIDE_MARKER for embed in message.embeds):
                        old_messages.append(message)
            except discord.HTTPException:
                log.exception("Could not inspect #%s", channel.name)
                continue
            old_messages.reverse()
            embeds = guide_embeds()
            for index, embed in enumerate(embeds):
                if index < len(old_messages):
                    message = old_messages[index]
                    await message.edit(embed=embed, content=None)
                else:
                    message = await channel.send(embed=embed)
                messages.append(message)
            for message in old_messages[len(embeds) :]:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
        return messages

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with self.ready_lock:
            if self.checked:
                return
            self.checked = True
            try:
                await self.ensure_guide()
            except discord.HTTPException:
                self.checked = False
                log.exception("Could not post the staff command guide")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffGuide(bot))
