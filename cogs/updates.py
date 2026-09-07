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
UPDATE_PING_ROLE_NAME = os.getenv("UPDATE_PING_ROLE_NAME", "Update Pings")
UPDATE_PING_EMOJI = "🔔"
UPDATE_PING_PANEL_TITLE = "🔔 Update notifications"
RELEASE_ID = "2026-08-28-ticket-claim-button-v19"


def load_announced_releases() -> set[str]:
    try:
        value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    releases = value.get("announced", []) if isinstance(value, dict) else []
    return {str(release) for release in releases}


def load_update_ping_messages() -> dict[int, int]:
    try:
        value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    messages = value.get("update_ping_messages", {}) if isinstance(value, dict) else {}
    if not isinstance(messages, dict):
        return {}
    result: dict[int, int] = {}
    for guild_id, message_id in messages.items():
        try:
            result[int(guild_id)] = int(message_id)
        except (TypeError, ValueError):
            continue
    return result


def save_release_state(releases: set[str], update_ping_messages: dict[int, int]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "announced": sorted(releases),
                "update_ping_messages": {
                    str(guild_id): message_id
                    for guild_id, message_id in update_ping_messages.items()
                },
            },
            indent=2,
        ),
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
        self.update_ping_messages = load_update_ping_messages()

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

    @staticmethod
    def update_ping_role(guild: discord.Guild) -> discord.Role | None:
        wanted = normalise_role_name(UPDATE_PING_ROLE_NAME)
        return discord.utils.find(
            lambda role: normalise_role_name(role.name) == wanted,
            guild.roles,
        )

    async def ensure_update_ping_role(self, guild: discord.Guild) -> discord.Role | None:
        role = self.update_ping_role(guild)
        if role is not None:
            return role
        try:
            return await guild.create_role(
                name=UPDATE_PING_ROLE_NAME,
                reason="Density Bot update notification opt-in",
            )
        except discord.Forbidden:
            log.warning("Missing permission to create the %s role in %s", UPDATE_PING_ROLE_NAME, guild.name)
        except discord.HTTPException:
            log.exception("Could not create the %s role in %s", UPDATE_PING_ROLE_NAME, guild.name)
        return None

    async def ensure_update_ping_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        role: discord.Role,
    ) -> None:
        message: discord.Message | None = None
        message_id = self.update_ping_messages.get(guild.id)
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            embed = discord.Embed(
                title=UPDATE_PING_PANEL_TITLE,
                description=(
                    f"React with {UPDATE_PING_EMOJI} to receive the {role.mention} role and be notified "
                    "when important server or bot updates are posted.\n\n"
                    "Remove your reaction whenever you want to remove the role."
                ),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="Density SMP • Optional update notifications")
            try:
                message = await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                log.warning("Missing permission to post the update-role panel in #%s", channel.name)
                return
            except discord.HTTPException:
                log.exception("Could not post the update-role panel in #%s", channel.name)
                return
            self.update_ping_messages[guild.id] = message.id
        try:
            await message.add_reaction(UPDATE_PING_EMOJI)
        except discord.HTTPException:
            log.exception("Could not add the update-role reaction in #%s", channel.name)

    async def set_update_ping_role(self, payload: discord.RawReactionActionEvent, *, add: bool) -> None:
        if payload.guild_id is None or str(payload.emoji) != UPDATE_PING_EMOJI:
            return
        if self.update_ping_messages.get(payload.guild_id) != payload.message_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member if add else guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if member.bot:
            return
        role = self.update_ping_role(guild)
        if role is None:
            return
        try:
            if add and role not in member.roles:
                await member.add_roles(role, reason="Opted into update notifications")
            elif not add and role in member.roles:
                await member.remove_roles(role, reason="Opted out of update notifications")
        except discord.Forbidden:
            log.warning("Could not update %s for %s; check the bot role order", role.name, member)
        except discord.HTTPException:
            log.exception("Discord rejected an update-role change for %s", member)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self.set_update_ping_role(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self.set_update_ping_role(payload, add=False)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with self.ready_lock:
            if self.checked:
                return
            self.checked = True
            announced = load_announced_releases()
            for guild in self.bot.guilds:
                channel = await self.ensure_updates_channel(guild)
                role = await self.ensure_update_ping_role(guild)
                if channel is not None and role is not None:
                    await self.ensure_update_ping_panel(guild, channel, role)
                release_key = f"{guild.id}:{RELEASE_ID}"
                if channel is None or release_key in announced:
                    continue
                embed = discord.Embed(
                    title="Density Bot Update",
                    description=(
                        "• Added a persistent **Claim ticket** button to ticket controls.\n"
                        "• Only configured staff roles can use the button.\n"
                        "• The ticket records and announces which staff member claimed it.\n"
                        "• Other staff cannot take an already claimed ticket.\n"
                        "• Existing open tickets receive the new button when the bot starts."
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
                save_release_state(announced, self.update_ping_messages)
            except OSError:
                log.exception("Could not save release announcement state")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Updates(bot))
