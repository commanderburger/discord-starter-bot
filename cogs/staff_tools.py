import asyncio
import json
import logging
import os
import re
import shlex
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.giveaways import GiveawayCreateModal, Giveaways, parse_duration as parse_giveaway_duration
from cogs.levels import load_data as load_level_data
from cogs.moderation import Moderation, parse_duration as parse_timeout_duration
from cogs.permissions import (
    member_has_named_role,
    member_has_staff_role,
    normalise_role_name,
    role_is_staff,
    senior_role_names,
)
from cogs.tickets import (
    CloseTicketView,
    Tickets,
    ticket_claimed_by,
    ticket_owner_id,
    ticket_type_id,
)
from cogs.welcome import SERVER_NAME, ordinal


log = logging.getLogger("starter-bot.staff-tools")
DATA_FILE = Path(os.getenv("BOT_DATA_DIR", "/data")) / "staff-tools.json"
LEVEL_DATA_FILE = Path(os.getenv("BOT_DATA_DIR", "/data")) / "levels.json"
STAFF_ACTIVITY_CHANNEL = os.getenv("STAFF_ACTIVITY_CHANNEL", "staff-activity")
STAFF_PUNISHMENTS_CHANNEL = os.getenv("STAFF_PUNISHMENTS_CHANNEL", "staff-punishments")
STAFF_PING_ROLE = os.getenv("STAFF_ACTIVITY_PING_ROLE", "Staff Team")
ACTIVITY_INTERVAL_SECONDS = max(86_400, int(os.getenv("STAFF_ACTIVITY_INTERVAL_SECONDS", "432000")))
ACTIVITY_WINDOW_SECONDS = max(3600, int(os.getenv("STAFF_ACTIVITY_WINDOW_SECONDS", "86400")))
DONUT_STATS_URL = os.getenv("DONUT_STATS_API_URL", "https://api.donutsmp.net/v1/stats/{ign}")
DONUT_API_KEY = os.getenv("DONUT_API_KEY", "").strip()
TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/London"))
CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d+)>")
MONEY_RE = re.compile(r"^(\d+(?:\.\d+)?)([kmbt]?)$", re.IGNORECASE)
GUIDE_REFRESHED = "Panels, ticket state, staff guide, and scheduled displays refreshed."


def empty_data() -> dict:
    return {"guilds": {}}


def load_data() -> dict:
    try:
        value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty_data()
    if not isinstance(value, dict) or not isinstance(value.get("guilds"), dict):
        return empty_data()
    return value


def save_data(value: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, DATA_FILE)
    try:
        DATA_FILE.chmod(0o600)
    except OSError:
        pass


def guild_record(data: dict, guild_id: int) -> dict:
    return data.setdefault("guilds", {}).setdefault(
        str(guild_id),
        {
            "points": {},
            "strikes": {},
            "vouches": {},
            "afk": {},
            "loa": [],
            "sticky": {},
            "payments": {},
            "claims": {},
            "activity": {},
        },
    )


def context_is_staff(ctx: commands.Context) -> bool:
    return bool(
        ctx.guild
        and isinstance(ctx.author, discord.Member)
        and (
            ctx.author.id == ctx.guild.owner_id
            or ctx.author.guild_permissions.administrator
            or member_has_staff_role(ctx.author)
        )
    )


def context_is_senior(ctx: commands.Context) -> bool:
    return bool(
        ctx.guild
        and isinstance(ctx.author, discord.Member)
        and (
            ctx.author.id == ctx.guild.owner_id
            or member_has_named_role(ctx.author, senior_role_names())
        )
    )


def staff_command():
    async def predicate(ctx: commands.Context) -> bool:
        if context_is_staff(ctx):
            return True
        raise commands.CheckFailure("This command is for configured staff roles only.")

    return commands.check(predicate)


def senior_command():
    async def predicate(ctx: commands.Context) -> bool:
        if context_is_senior(ctx):
            return True
        raise commands.CheckFailure("Only Owner, Co Owner and Manager roles can use this command.")

    return commands.check(predicate)


def parse_money(value: str) -> int:
    match = MONEY_RE.fullmatch(value.strip().replace(",", ""))
    if not match:
        raise ValueError("Use a price such as `125m`, `750k`, or `1200000`.")
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}
    return int(float(match.group(1)) * multiplier[match.group(2).casefold()])


def format_money(value: int) -> str:
    for suffix, divisor in (("T", 1_000_000_000_000), ("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if value >= divisor:
            number = value / divisor
            return f"${number:.2f}{suffix}".replace(".00", "")
    return f"${value:,}"


def find_channel(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    wanted = normalise_role_name(name)
    return discord.utils.find(
        lambda channel: isinstance(channel, discord.TextChannel)
        and normalise_role_name(channel.name) == wanted,
        guild.channels,
    )


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
                add_reactions=True,
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
            add_reactions=True,
            embed_links=True,
            attach_files=True,
        )
    return overwrites


def target_error(ctx: commands.Context, member: discord.Member) -> str | None:
    if ctx.guild is None or not isinstance(ctx.author, discord.Member):
        return "Use this command in the server."
    if member.id == ctx.author.id:
        return "You cannot use that moderation action on yourself."
    if member.id == ctx.guild.owner_id:
        return "The server owner cannot be moderated."
    if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
        return "That member has an equal or higher role than you."
    if ctx.guild.me and member.top_role >= ctx.guild.me.top_role:
        return "Move the bot role above that member's role first."
    return None


def update_topic_token(topic: str | None, key: str, value: str | None) -> str:
    current = re.sub(rf"(?:^|\s){re.escape(key)}:[^\s]+", "", topic or "").strip()
    current = re.sub(r"\s+", " ", current)
    return f"{current} {key}:{value}".strip() if value is not None else current


def format_stats_value(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f"{value:,}"
    return str(value)


class TicketCloseRequestView(discord.ui.View):
    def __init__(self, cog: "StaffTools", requester_id: int, owner_id: int, requester_is_staff: bool) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.requester_id = requester_id
        self.owner_id = owner_id
        self.requester_is_staff = requester_is_staff

    @discord.ui.button(label="Confirm close", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
            return
        if self.requester_is_staff:
            allowed = interaction.user.id == self.owner_id
        else:
            allowed = isinstance(interaction.user, discord.Member) and member_has_staff_role(interaction.user)
        if not allowed:
            await interaction.response.send_message("The other side of the ticket must confirm this request.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.close_ticket_channel(interaction.channel, interaction.user)

    @discord.ui.button(label="Keep open", style=discord.ButtonStyle.secondary)
    async def keep_open(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id not in {self.requester_id, self.owner_id} and not (
            isinstance(interaction.user, discord.Member) and member_has_staff_role(interaction.user)
        ):
            await interaction.response.send_message("You cannot decide this request.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="The close request was cancelled.", view=self)


class PaymentTrackerView(discord.ui.View):
    def __init__(self, cog: "StaffTools") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Mark paid", style=discord.ButtonStyle.success, custom_id="density-payment-paid-v1")
    async def mark_paid(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.set_payment_status(interaction, True)

    @discord.ui.button(label="Mark unpaid", style=discord.ButtonStyle.secondary, custom_id="density-payment-unpaid-v1")
    async def mark_unpaid(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.set_payment_status(interaction, False)


class DashboardButtonView(discord.ui.View):
    def __init__(self, requester_id: int, label: str, callback, emoji: str | None = None) -> None:
        super().__init__(timeout=300)
        self.requester_id = requester_id
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, emoji=emoji)
        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the staff member who opened this dashboard can use it.", ephemeral=True)
        return False


class EmbedEditorModal(discord.ui.Modal):
    def __init__(self, cog: "StaffTools", message_id: int | None = None) -> None:
        super().__init__(title="Edit an embed" if message_id else "Create an embed")
        self.cog = cog
        self.message_id = message_id
        self.embed_title = discord.ui.TextInput(label="Title", max_length=256, required=False)
        self.description = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, max_length=4000, required=True
        )
        self.colour = discord.ui.TextInput(
            label="Colour", placeholder="#5865F2 or blue", default="#5865F2", max_length=20, required=False
        )
        self.footer = discord.ui.TextInput(label="Footer (optional)", max_length=200, required=False)
        self.add_item(self.embed_title)
        self.add_item(self.description)
        self.add_item(self.colour)
        self.add_item(self.footer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        colour_text = str(self.colour).strip().casefold()
        named = {
            "blue": discord.Color.blurple(),
            "green": discord.Color.green(),
            "red": discord.Color.red(),
            "gold": discord.Color.gold(),
            "purple": discord.Color.purple(),
        }
        colour = named.get(colour_text)
        if colour is None:
            try:
                colour = discord.Color(int(colour_text.removeprefix("#"), 16))
            except ValueError:
                await interaction.response.send_message("Use a hex colour like `#5865F2` or a named colour.", ephemeral=True)
                return
        embed = discord.Embed(
            title=str(self.embed_title).strip() or None,
            description=str(self.description).strip(),
            color=colour,
        )
        if str(self.footer).strip():
            embed.set_footer(text=str(self.footer).strip())
        if self.message_id is None:
            await interaction.response.send_message(embed=embed)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use this in the channel containing the message.", ephemeral=True)
            return
        try:
            message = await interaction.channel.fetch_message(self.message_id)
            if message.author.id != interaction.client.user.id:
                raise ValueError("I can only edit my own messages.")
            await message.edit(content=None, embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as error:
            await interaction.response.send_message(f"I could not edit that message: {error}", ephemeral=True)
            return
        await interaction.response.send_message(f"Embed updated: {message.jump_url}", ephemeral=True)


class QuickdropCreateModal(discord.ui.Modal, title="Create a quickdrop"):
    prize = discord.ui.TextInput(label="Prize", max_length=250)
    duration = discord.ui.TextInput(label="Duration", placeholder="10m, 2h, 1d", default="10m", max_length=20)
    winners = discord.ui.TextInput(label="Winners", default="1", max_length=2)

    def __init__(self, cog: Giveaways) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
            return
        try:
            seconds = parse_giveaway_duration(str(self.duration))
            winners = int(str(self.winners))
            if not 1 <= winners <= 20:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Use a valid duration and 1–20 winners.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        message, _ = await self.cog.create_giveaway(
            interaction.channel,
            interaction.user.id,
            str(self.prize),
            seconds,
            winners,
            giveaway_type="Quickdrop",
        )
        await interaction.followup.send(f"Quickdrop created: {message.jump_url}", ephemeral=True)


class ManageRoleModal(discord.ui.Modal):
    member_value = discord.ui.TextInput(label="Member ID", placeholder="Right-click member → Copy User ID")
    role_value = discord.ui.TextInput(label="Role name or ID", placeholder="Builder, Helper, 123456789...")

    def __init__(self, add_role: bool) -> None:
        super().__init__(title="Promote member" if add_role else "Demote member")
        self.add_role = add_role

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        if interaction.user.id != interaction.guild.owner_id and not member_has_named_role(interaction.user, senior_role_names()):
            await interaction.response.send_message("Only Owner, Co Owner and Manager can use this.", ephemeral=True)
            return
        raw_member = str(self.member_value).strip().strip("<@!>")
        member = interaction.guild.get_member(int(raw_member)) if raw_member.isdigit() else None
        raw_role = str(self.role_value).strip().strip("<@&>")
        role = interaction.guild.get_role(int(raw_role)) if raw_role.isdigit() else discord.utils.find(
            lambda item: normalise_role_name(item.name) == normalise_role_name(raw_role), interaction.guild.roles
        )
        if member is None or role is None or role.is_default():
            await interaction.response.send_message("I could not find that member or role.", ephemeral=True)
            return
        if role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("You cannot manage a role equal to or above your own.", ephemeral=True)
            return
        if interaction.guild.me and role >= interaction.guild.me.top_role:
            await interaction.response.send_message("Move the bot role above that role first.", ephemeral=True)
            return
        if self.add_role:
            await member.add_roles(role, reason=f"Promoted by {interaction.user}")
            action = "Added"
        else:
            await member.remove_roles(role, reason=f"Demoted by {interaction.user}")
            action = "Removed"
        await interaction.response.send_message(f"{action} {role.mention} {'to' if self.add_role else 'from'} {member.mention}.", ephemeral=True)


class ManageView(discord.ui.View):
    def __init__(self, requester_id: int) -> None:
        super().__init__(timeout=300)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the manager who opened this dashboard can use it.", ephemeral=True)
        return False

    @discord.ui.button(label="Promote", style=discord.ButtonStyle.success, emoji="⬆️")
    async def promote(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ManageRoleModal(True))

    @discord.ui.button(label="Demote", style=discord.ButtonStyle.danger, emoji="⬇️")
    async def demote(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ManageRoleModal(False))


class StaffTools(commands.Cog):
    v_group = app_commands.Group(name="v", description="Manage staff vouches")
    point_group = app_commands.Group(name="point", description="Manage builder/staff points")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.data_lock = asyncio.Lock()
        self.snipes: dict[int, dict] = {}
        self.sticky_cooldowns: dict[int, float] = {}
        self.ready_once = False
        self.bot.add_view(PaymentTrackerView(self))
        self.activity_loop.start()

    def cog_unload(self) -> None:
        self.activity_loop.cancel()

    async def persist(self, data: dict) -> None:
        await asyncio.to_thread(save_data, data)

    async def ensure_staff_channel(self, guild: discord.Guild, name: str) -> discord.TextChannel | None:
        channel = find_channel(guild, name)
        if channel:
            return channel
        try:
            return await guild.create_text_channel(
                name,
                overwrites=staff_overwrites(guild),
                reason=f"Density Bot {name} setup",
            )
        except discord.HTTPException:
            log.exception("Could not create #%s in %s", name, guild.name)
            return None

    async def send_punishment_log(
        self,
        guild: discord.Guild,
        *,
        action: str,
        member: discord.Member,
        reason: str,
        source: discord.abc.User | None = None,
        count: int | None = None,
    ) -> None:
        channel = await self.ensure_staff_channel(guild, STAFF_PUNISHMENTS_CHANNEL)
        if channel is None:
            return
        embed = discord.Embed(
            title=f"Staff strike {action}",
            description=(
                f"**Staff member:** {member.mention} (`{member.id}`)\n"
                f"**Reason:** {reason}\n"
                f"**By:** {source.mention if source else 'Density Bot activity check'}"
                + (f"\n**Current strikes:** {count}" if count is not None else "")
            ),
            color=discord.Color.red() if action == "added" else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def add_strike_record(
        self,
        guild: discord.Guild,
        member: discord.Member,
        reason: str,
        source: discord.abc.User | None,
    ) -> int:
        async with self.data_lock:
            data = load_data()
            record = guild_record(data, guild.id)
            entries = record.setdefault("strikes", {}).setdefault(str(member.id), [])
            entries.append(
                {
                    "reason": reason[:500],
                    "moderator_id": source.id if source else 0,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            count = len(entries)
            await self.persist(data)
        await self.send_punishment_log(
            guild, action="added", member=member, reason=reason, source=source, count=count
        )
        return count

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.ready_once:
            return
        self.ready_once = True
        for guild in self.bot.guilds:
            await self.ensure_staff_channel(guild, STAFF_ACTIVITY_CHANNEL)
            await self.ensure_staff_channel(guild, STAFF_PUNISHMENTS_CHANNEL)
            async with self.data_lock:
                data = load_data()
                activity = guild_record(data, guild.id).setdefault("activity", {})
                if not activity.get("next_post_at"):
                    activity["next_post_at"] = int(datetime.now(UTC).timestamp()) + 15
                    await self.persist(data)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild and not message.author.bot:
            self.snipes[message.channel.id] = {
                "author_id": message.author.id,
                "author": str(message.author),
                "content": message.content or "<no text>",
                "attachment": message.attachments[0].url if message.attachments else None,
                "deleted_at": datetime.now(UTC).isoformat(),
            }

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or not isinstance(message.author, discord.Member):
            return
        data = load_data()
        record = guild_record(data, message.guild.id)
        afk = record.setdefault("afk", {})
        own = afk.pop(str(message.author.id), None)
        if own:
            await self.persist(data)
            await message.channel.send(f"Welcome back {message.author.mention}; I removed your AFK status.", delete_after=8)
        notices: list[str] = []
        for member in message.mentions[:5]:
            entry = afk.get(str(member.id))
            if isinstance(entry, dict):
                notices.append(f"{member.display_name} is AFK: {entry.get('reason', 'AFK')}")
        if notices:
            await message.channel.send("\n".join(notices), delete_after=12)

        sticky = record.setdefault("sticky", {}).get(str(message.channel.id))
        if not isinstance(sticky, dict) or message.id == int(sticky.get("message_id", 0)):
            return
        now = asyncio.get_running_loop().time()
        if now - self.sticky_cooldowns.get(message.channel.id, 0.0) < 5:
            return
        self.sticky_cooldowns[message.channel.id] = now
        old_id = int(sticky.get("message_id", 0))
        try:
            if old_id:
                old = await message.channel.fetch_message(old_id)
                await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        try:
            sent = await message.channel.send(
                embed=discord.Embed(
                    title="📌 Sticky message",
                    description=str(sticky.get("content", "")),
                    color=discord.Color.blurple(),
                )
            )
        except discord.HTTPException:
            return
        async with self.data_lock:
            current = load_data()
            current_sticky = guild_record(current, message.guild.id).setdefault("sticky", {}).get(str(message.channel.id))
            if isinstance(current_sticky, dict):
                current_sticky["message_id"] = sent.id
                await self.persist(current)

    async def close_ticket_channel(self, channel: discord.TextChannel, closed_by: discord.abc.User) -> None:
        tickets = self.bot.get_cog("Tickets")
        owner_id = ticket_owner_id(channel)
        ticket_type = ticket_type_id(channel)
        if not isinstance(tickets, Tickets) or owner_id is None or ticket_type is None:
            raise RuntimeError("This is not one of my ticket channels.")
        await tickets.file_ticket_log(channel, owner_id, closed_by, ticket_type)
        await channel.delete(reason=f"Ticket closed by {closed_by}")

    @commands.command(name="welcome-test")
    @staff_command()
    async def welcome_test(self, ctx: commands.Context) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return
        count = ctx.guild.member_count or len(ctx.guild.members)
        await ctx.send(
            f"Welcome {ctx.author.mention} to **{SERVER_NAME}**! You are the **{ordinal(count)}** member!\n"
            "*This is a test; no member joined.*",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="refresh")
    @senior_command()
    async def refresh(self, ctx: commands.Context) -> None:
        tickets = self.bot.get_cog("Tickets")
        if isinstance(tickets, Tickets):
            await tickets.ensure_panel()
        guide = self.bot.get_cog("StaffGuide")
        if guide and hasattr(guide, "ensure_guide"):
            await guide.ensure_guide(ctx.guild)
        if ctx.guild:
            await self.ensure_staff_channel(ctx.guild, STAFF_ACTIVITY_CHANNEL)
            await self.ensure_staff_channel(ctx.guild, STAFF_PUNISHMENTS_CHANNEL)
        await ctx.reply(GUIDE_REFRESHED)

    def current_ticket(self, ctx: commands.Context) -> tuple[discord.TextChannel | None, int | None]:
        channel = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        owner_id = ticket_owner_id(channel) if channel else None
        return channel, owner_id

    async def claim_ticket_channel(
        self,
        channel: discord.TextChannel,
        staff: discord.Member,
        builder_ign: str = "",
    ) -> tuple[bool, str]:
        """Claim a ticket from either the button or the legacy command."""

        if ticket_owner_id(channel) is None or channel.guild is None:
            return False, "Use this inside a ticket channel."

        claimed_by = ticket_claimed_by(channel)
        if claimed_by is not None and claimed_by != staff.id:
            return False, f"This ticket is already claimed by <@{claimed_by}>."
        if claimed_by == staff.id and not builder_ign:
            return False, "You have already claimed this ticket."

        async with self.data_lock:
            data = load_data()
            claims = guild_record(data, channel.guild.id).setdefault("claims", {})
            current = claims.get(str(channel.id))
            if isinstance(current, dict):
                current_staff_id = int(current.get("staff_id", 0))
                if current_staff_id and current_staff_id != staff.id:
                    return False, f"This ticket is already claimed by <@{current_staff_id}>."
            claims[str(channel.id)] = {
                "staff_id": staff.id,
                "builder_ign": builder_ign[:32],
                "claimed_at": datetime.now(UTC).isoformat(),
            }
            await self.persist(data)

        topic = update_topic_token(channel.topic, "density-ticket-claimed", str(staff.id))
        if builder_ign:
            clean_ign = re.sub(r"[^A-Za-z0-9_]", "", builder_ign)[:32]
            topic = update_topic_token(topic, "density-builder-ign", clean_ign)
        await channel.edit(topic=topic[:1024], reason=f"Ticket claimed by {staff}")
        note = f" for builder IGN `{builder_ign}`" if builder_ign else ""
        return True, f"✅ {staff.mention} claimed this ticket{note}."

    @commands.command(name="claim")
    @staff_command()
    async def claim(self, ctx: commands.Context, *, builder_ign: str = "") -> None:
        channel, owner_id = self.current_ticket(ctx)
        if channel is None or owner_id is None or ctx.guild is None:
            await ctx.reply("Use this inside a ticket channel.")
            return
        claimed, message = await self.claim_ticket_channel(channel, ctx.author, builder_ign)
        if claimed:
            await ctx.send(message)
        else:
            await ctx.reply(message)

    @commands.command(name="unclaim")
    @staff_command()
    async def unclaim(self, ctx: commands.Context) -> None:
        channel, owner_id = self.current_ticket(ctx)
        if channel is None or owner_id is None or ctx.guild is None:
            await ctx.reply("Use this inside a ticket channel.")
            return
        async with self.data_lock:
            data = load_data()
            guild_record(data, ctx.guild.id).setdefault("claims", {}).pop(str(channel.id), None)
            await self.persist(data)
        topic = update_topic_token(channel.topic, "density-ticket-claimed", None)
        topic = update_topic_token(topic, "density-builder-ign", None)
        await channel.edit(topic=topic[:1024], reason=f"Ticket unclaimed by {ctx.author}")
        await ctx.send(f"↩️ {ctx.author.mention} unclaimed this ticket.")

    @commands.command(name="close")
    async def close(self, ctx: commands.Context) -> None:
        channel, owner_id = self.current_ticket(ctx)
        if channel is None or owner_id is None:
            await ctx.reply("Use this inside a ticket channel.")
            return
        if ctx.author.id != owner_id and not context_is_staff(ctx):
            await ctx.reply("Only the ticket owner or staff can close this ticket.")
            return
        tickets = self.bot.get_cog("Tickets")
        if not isinstance(tickets, Tickets):
            await ctx.reply("The ticket system is unavailable.")
            return
        embed = discord.Embed(
            title="Close ticket?",
            description="Press the button below to save the transcript and delete this ticket.",
            color=discord.Color.red(),
        )
        await ctx.send(embed=embed, view=CloseTicketView(tickets))

    @commands.group(name="req", invoke_without_command=True)
    async def req(self, ctx: commands.Context) -> None:
        await ctx.reply("Use `!req close` to ask the other side to confirm closing this ticket.")

    @req.command(name="close")
    async def req_close(self, ctx: commands.Context) -> None:
        channel, owner_id = self.current_ticket(ctx)
        if channel is None or owner_id is None:
            await ctx.reply("Use this inside a ticket channel.")
            return
        requester_is_staff = context_is_staff(ctx)
        if ctx.author.id != owner_id and not requester_is_staff:
            await ctx.reply("Only the ticket owner or staff can request closing.")
            return
        target = f"<@{owner_id}>" if requester_is_staff else "the staff team"
        await ctx.send(
            f"🔒 {target}, {ctx.author.mention} requested that this ticket be closed. Please confirm below.",
            view=TicketCloseRequestView(self, ctx.author.id, owner_id, requester_is_staff),
        )

    @commands.command(name="rename")
    @staff_command()
    async def rename(self, ctx: commands.Context, *, name: str) -> None:
        channel, owner_id = self.current_ticket(ctx)
        if channel is None or owner_id is None or ctx.guild is None:
            await ctx.reply("Use this inside a ticket channel.")
            return
        cleaned = re.sub(r"[^a-z0-9-]", "-", name.casefold())
        cleaned = re.sub(r"-+", "-", cleaned).strip("-")[:90]
        if not cleaned:
            await ctx.reply("Give the ticket a valid name.")
            return
        await channel.edit(name=cleaned, reason=f"Renamed by {ctx.author}")
        async with self.data_lock:
            data = load_data()
            points = guild_record(data, ctx.guild.id).setdefault("points", {})
            points[str(ctx.author.id)] = int(points.get(str(ctx.author.id), 0)) + 1
            total = points[str(ctx.author.id)]
            await self.persist(data)
        await ctx.send(f"Renamed this ticket to `#{cleaned}` and awarded {ctx.author.mention} one point (**{total}** total).")

    def payment_embed(self, channel: discord.TextChannel, entry: dict) -> discord.Embed:
        amount = int(entry.get("amount", 0))
        paid = bool(entry.get("paid"))
        embed = discord.Embed(
            title="Payment tracking",
            description=(
                f"**Ticket:** {channel.mention}\n"
                f"**Price:** {format_money(amount) if amount else 'Not set'}\n"
                f"**Status:** {'✅ Paid' if paid else '⏳ Awaiting payment'}\n"
                f"**Tracked by:** <@{int(entry.get('staff_id', 0))}>"
            ),
            color=discord.Color.green() if paid else discord.Color.blurple(),
        )
        embed.set_footer(text="Density payment tracker")
        return embed

    async def upsert_payment(self, ctx: commands.Context, amount: int | None = None) -> None:
        channel, owner_id = self.current_ticket(ctx)
        if channel is None or owner_id is None or ctx.guild is None:
            await ctx.reply("Use this inside a ticket channel.")
            return
        async with self.data_lock:
            data = load_data()
            payments = guild_record(data, ctx.guild.id).setdefault("payments", {})
            entry = payments.setdefault(str(channel.id), {"amount": 0, "paid": False, "staff_id": ctx.author.id})
            if amount is not None:
                entry["amount"] = amount
            entry["staff_id"] = ctx.author.id
            message_id = int(entry.get("message_id", 0))
            await self.persist(data)
        message = None
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message:
            await message.edit(embed=self.payment_embed(channel, entry), view=PaymentTrackerView(self))
        else:
            message = await channel.send(embed=self.payment_embed(channel, entry), view=PaymentTrackerView(self))
            async with self.data_lock:
                data = load_data()
                guild_record(data, ctx.guild.id).setdefault("payments", {}).setdefault(str(channel.id), entry)["message_id"] = message.id
                await self.persist(data)
        await ctx.reply(f"Payment tracker ready: {message.jump_url}")

    async def set_payment_status(self, interaction: discord.Interaction, paid: bool) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this in a ticket.", ephemeral=True)
            return
        if not member_has_staff_role(interaction.user) and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("This control is for staff only.", ephemeral=True)
            return
        async with self.data_lock:
            data = load_data()
            entry = guild_record(data, interaction.guild.id).setdefault("payments", {}).get(str(interaction.channel.id))
            if not isinstance(entry, dict):
                await interaction.response.send_message("I could not find this payment tracker.", ephemeral=True)
                return
            entry["paid"] = paid
            entry["updated_by"] = interaction.user.id
            await self.persist(data)
        await interaction.response.edit_message(embed=self.payment_embed(interaction.channel, entry), view=PaymentTrackerView(self))

    @commands.command(name="ptrack")
    @staff_command()
    async def ptrack(self, ctx: commands.Context) -> None:
        await self.upsert_payment(ctx)

    @commands.group(name="payment", invoke_without_command=True)
    @staff_command()
    async def payment(self, ctx: commands.Context) -> None:
        await ctx.reply("Use `!payment track 125m` to create or update payment tracking.")

    @payment.command(name="track")
    @staff_command()
    async def payment_track(self, ctx: commands.Context, price: str) -> None:
        try:
            amount = parse_money(price)
        except ValueError as error:
            await ctx.reply(str(error))
            return
        await self.upsert_payment(ctx, amount)

    @commands.group(name="build", invoke_without_command=True)
    async def build(self, ctx: commands.Context) -> None:
        await ctx.reply("Use `!build cancel` to cancel your unclaimed build or digout request.")

    @build.command(name="cancel")
    async def build_cancel(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        candidates = [
            channel
            for channel in ctx.guild.text_channels
            if ticket_owner_id(channel) == ctx.author.id
            and any(word in channel.name.casefold() or word in (channel.topic or "").casefold() for word in ("build", "digout"))
        ]
        if not candidates:
            await ctx.reply("I could not find an unclaimed build or digout request belonging to you.")
            return
        data = load_data()
        claims = guild_record(data, ctx.guild.id).setdefault("claims", {})
        channel = discord.utils.find(lambda item: str(item.id) not in claims, candidates)
        if channel is None:
            await ctx.reply("Your request is already claimed; ask staff before cancelling it.")
            return
        try:
            await self.close_ticket_channel(channel, ctx.author)
        except RuntimeError as error:
            await ctx.reply(str(error))
            return

    @commands.command(name="leaderboard", aliases=["lb"])
    async def points_leaderboard(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        points = guild_record(load_data(), ctx.guild.id).get("points", {})
        ranked = sorted(points.items(), key=lambda item: int(item[1]), reverse=True)[:10]
        lines = [f"**{index}.** <@{user_id}> — **{score}** points" for index, (user_id, score) in enumerate(ranked, 1)]
        await ctx.send(embed=discord.Embed(title="Builder Points Leaderboard", description="\n".join(lines) or "No points yet.", color=discord.Color.gold()))

    @commands.command(name="msglb")
    async def message_leaderboard(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        levels = load_level_data().get("guilds", {}).get(str(ctx.guild.id), {}).get("users", {})
        ranked = sorted(levels.items(), key=lambda item: int(item[1].get("messages", 0)), reverse=True)[:10]
        lines = [f"**{index}.** <@{user_id}> — **{entry.get('messages', 0)}** XP messages" for index, (user_id, entry) in enumerate(ranked, 1)]
        await ctx.send(embed=discord.Embed(title="Message Leaderboard", description="\n".join(lines) or "No message XP yet.", color=discord.Color.blurple()))

    @commands.command(name="strikelb")
    @senior_command()
    async def strike_leaderboard(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        strikes = guild_record(load_data(), ctx.guild.id).get("strikes", {})
        ranked = sorted(strikes.items(), key=lambda item: len(item[1]), reverse=True)[:10]
        lines = [f"**{index}.** <@{user_id}> — **{len(entries)}** strikes" for index, (user_id, entries) in enumerate(ranked, 1) if entries]
        await ctx.send(embed=discord.Embed(title="Staff Strike Leaderboard", description="\n".join(lines) or "No staff strikes.", color=discord.Color.red()))

    @commands.command(name="afk")
    async def afk(self, ctx: commands.Context, *, reason: str = "AFK") -> None:
        if ctx.guild is None:
            return
        async with self.data_lock:
            data = load_data()
            guild_record(data, ctx.guild.id).setdefault("afk", {})[str(ctx.author.id)] = {
                "reason": reason[:300], "since": datetime.now(UTC).isoformat()
            }
            await self.persist(data)
        await ctx.reply(f"I marked you AFK: {reason}")

    @commands.command(name="membercount")
    async def membercount(self, ctx: commands.Context) -> None:
        if ctx.guild:
            await ctx.send(f"👥 **{ctx.guild.name}** has **{ctx.guild.member_count or len(ctx.guild.members):,}** members.")

    @commands.command(name="pfp")
    async def pfp(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        embed = discord.Embed(title=f"{target}'s profile picture", color=discord.Color.blurple())
        embed.set_image(url=target.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="banner")
    async def banner(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        user = await self.bot.fetch_user(target.id)
        if not user.banner:
            await ctx.reply("That user does not have a profile banner.")
            return
        embed = discord.Embed(title=f"{target}'s banner", color=user.accent_color or discord.Color.blurple())
        embed.set_image(url=user.banner.url)
        await ctx.send(embed=embed)

    @commands.command(name="stats")
    async def player_stats(self, ctx: commands.Context, ign: str) -> None:
        if not DONUT_API_KEY:
            await ctx.reply("DonutSMP player stats are not configured yet. Add `DONUT_API_KEY` to the bot's TrueNAS environment.")
            return
        url = DONUT_STATS_URL.format(ign=quote(ign.strip()))
        headers = {"Authorization": f"Bearer {DONUT_API_KEY}"}
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as response:
                    payload = await response.json(content_type=None)
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError) as error:
            await ctx.reply(f"I could not load DonutSMP stats for `{ign}`: {error}")
            return
        result = payload.get("result", payload.get("data", payload)) if isinstance(payload, dict) else {}
        if not isinstance(result, dict):
            await ctx.reply(f"No player stats were returned for `{ign}`.")
            return
        preferred = ("money", "shards", "kills", "deaths", "playtime", "placed_blocks", "broken_blocks", "mobs_killed", "money_made_from_sell")
        fields = [(key, result.get(key)) for key in preferred if result.get(key) is not None]
        embed = discord.Embed(title=f"DonutSMP Stats — {ign}", color=discord.Color.blurple())
        for key, value in fields[:12]:
            embed.add_field(name=key.replace("_", " ").title(), value=format_stats_value(value), inline=True)
        if not fields:
            embed.description = "The player was found, but the API did not return recognised stats."
        await ctx.send(embed=embed)

    @commands.command(name="translate")
    async def translate(self, ctx: commands.Context, language: str = "English") -> None:
        if not ctx.message.reference or not ctx.message.reference.message_id:
            await ctx.reply("Reply to a message, then use `!translate` or `!translate Spanish`.")
            return
        try:
            original = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await ctx.reply("I could not read the replied-to message.")
            return
        codes = {"english": "en", "spanish": "es", "french": "fr", "german": "de", "italian": "it", "portuguese": "pt", "dutch": "nl", "polish": "pl", "turkish": "tr", "arabic": "ar", "japanese": "ja", "chinese": "zh-CN"}
        target = codes.get(language.casefold(), language[:10])
        params = {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": original.content[:4000]}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get("https://translate.googleapis.com/translate_a/single", params=params) as response:
                    value = await response.json(content_type=None)
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
            translated = "".join(part[0] for part in value[0] if part and part[0])
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError, TypeError, IndexError) as error:
            await ctx.reply(f"Translation failed: {error}")
            return
        await ctx.send(embed=discord.Embed(title=f"Translation → {language}", description=translated[:4000], color=discord.Color.blurple()))

    @commands.command(name="loa")
    async def loa(self, ctx: commands.Context, duration: str, *, reason: str) -> None:
        match = re.fullmatch(r"(\d+)([hdw])", duration.casefold())
        if not match:
            await ctx.reply("Use a duration such as `3d`, `1w`, or `12h`.")
            return
        seconds = int(match.group(1)) * {"h": 3600, "d": 86400, "w": 604800}[match.group(2)]
        ends_at = int(datetime.now(UTC).timestamp()) + seconds
        if ctx.guild is None:
            return
        channel = find_channel(ctx.guild, "loa-requests") or ctx.channel
        embed = discord.Embed(
            title="Leave of absence request",
            description=f"**Member:** {ctx.author.mention}\n**Duration:** {duration}\n**Ends:** <t:{ends_at}:F>\n**Reason:** {reason}",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        await channel.send(embed=embed)
        async with self.data_lock:
            data = load_data()
            guild_record(data, ctx.guild.id).setdefault("loa", []).append({"user_id": ctx.author.id, "ends_at": ends_at, "reason": reason[:500]})
            await self.persist(data)
        await ctx.reply(f"Your LOA request was posted in {channel.mention}.")

    @commands.command(name="snipe")
    async def snipe(self, ctx: commands.Context) -> None:
        entry = self.snipes.get(ctx.channel.id)
        if not entry:
            await ctx.reply("There is no recently deleted message to show.")
            return
        embed = discord.Embed(title="Last deleted message", description=entry["content"][:4000], color=discord.Color.orange())
        embed.set_author(name=entry["author"])
        if entry.get("attachment"):
            embed.add_field(name="Attachment", value=entry["attachment"], inline=False)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.group(name="vouch", invoke_without_command=True)
    async def vouch(self, ctx: commands.Context, member: discord.Member | None = None, *, reason: str = "") -> None:
        if ctx.guild is None or member is None or not reason:
            await ctx.reply("Use `!vouch @user reason` or `!vouch list @user`.")
            return
        if member.id == ctx.author.id:
            await ctx.reply("You cannot vouch for yourself.")
            return
        async with self.data_lock:
            data = load_data()
            entries = guild_record(data, ctx.guild.id).setdefault("vouches", {}).setdefault(str(member.id), [])
            if any(int(entry.get("author_id", 0)) == ctx.author.id for entry in entries):
                await ctx.reply("You have already vouched for this member.")
                return
            entries.append({"author_id": ctx.author.id, "reason": reason[:500], "created_at": datetime.now(UTC).isoformat()})
            await self.persist(data)
        await ctx.send(f"✅ {ctx.author.mention} vouched for {member.mention}: {reason}")

    @vouch.command(name="list")
    async def vouch_list(self, ctx: commands.Context, member: discord.Member) -> None:
        if ctx.guild is None:
            return
        entries = guild_record(load_data(), ctx.guild.id).get("vouches", {}).get(str(member.id), [])
        lines = [f"**{index}.** <@{entry.get('author_id')}> — {entry.get('reason')}" for index, entry in enumerate(entries[-20:], 1)]
        await ctx.send(embed=discord.Embed(title=f"Vouches for {member}", description="\n".join(lines) or "No vouches.", color=discord.Color.green()))

    @v_group.command(name="remove", description="Remove a specific vouch from a member")
    @app_commands.describe(user="Member whose vouch should be removed", index="Number shown by !vouch list")
    async def v_remove(self, interaction: discord.Interaction, user: discord.Member, index: app_commands.Range[int, 1, 100] = 1) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not (
            interaction.user.id == interaction.guild.owner_id or member_has_staff_role(interaction.user)
        ):
            await interaction.response.send_message("This command is for staff only.", ephemeral=True)
            return
        async with self.data_lock:
            data = load_data()
            entries = guild_record(data, interaction.guild.id).setdefault("vouches", {}).get(str(user.id), [])
            if index > len(entries):
                await interaction.response.send_message("That vouch number does not exist. Use `!vouch list` first.", ephemeral=True)
                return
            removed = entries.pop(index - 1)
            await self.persist(data)
        await interaction.response.send_message(f"Removed vouch #{index} from {user.mention}: {removed.get('reason')}", ephemeral=True)

    @point_group.command(name="remove", description="Remove points from a staff member or builder")
    async def point_remove(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1000000]) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not (
            interaction.user.id == interaction.guild.owner_id or member_has_named_role(interaction.user, senior_role_names())
        ):
            await interaction.response.send_message("Only Owner, Co Owner and Manager can remove points.", ephemeral=True)
            return
        async with self.data_lock:
            data = load_data()
            points = guild_record(data, interaction.guild.id).setdefault("points", {})
            points[str(user.id)] = max(0, int(points.get(str(user.id), 0)) - amount)
            total = points[str(user.id)]
            await self.persist(data)
        await interaction.response.send_message(f"Removed {amount} point(s) from {user.mention}. New total: **{total}**.", ephemeral=True)

    @commands.command(name="suggest")
    async def suggest(self, ctx: commands.Context, *, suggestion: str) -> None:
        if ctx.guild is None:
            return
        channel = find_channel(ctx.guild, "suggestions")
        if channel is None:
            channel = await ctx.guild.create_text_channel("suggestions", reason="Density Bot suggestions")
        embed = discord.Embed(title="New suggestion", description=suggestion[:4000], color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        message = await channel.send(embed=embed)
        await message.add_reaction("👍")
        await message.add_reaction("👎")
        await ctx.reply(f"Suggestion posted: {message.jump_url}")

    @commands.command(name="lock")
    @staff_command()
    async def lock(self, ctx: commands.Context) -> None:
        if not isinstance(ctx.channel, discord.TextChannel):
            return
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Locked by {ctx.author}")
        await ctx.send("🔒 Channel locked.")

    @commands.command(name="unlock")
    @staff_command()
    async def unlock(self, ctx: commands.Context) -> None:
        if not isinstance(ctx.channel, discord.TextChannel):
            return
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {ctx.author}")
        await ctx.send("🔓 Channel unlocked.")

    @commands.command(name="steal")
    @staff_command()
    async def steal(self, ctx: commands.Context, emoji: str, *, new_name: str = "") -> None:
        if ctx.guild is None:
            return
        match = CUSTOM_EMOJI_RE.fullmatch(emoji.strip())
        if not match:
            await ctx.reply("Send one custom Discord emoji, for example `!steal <:name:123456789>`.")
            return
        animated, old_name, emoji_id = match.groups()
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    await ctx.reply("I could not download that emoji.")
                    return
                image = await response.read()
        created = await ctx.guild.create_custom_emoji(name=(new_name or old_name)[:32], image=image, reason=f"Stolen by {ctx.author}")
        await ctx.send(f"Added {created} as `:{created.name}:`.")

    async def set_sticky(self, ctx: commands.Context, content: str) -> None:
        if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel):
            return
        async with self.data_lock:
            data = load_data()
            sticky = guild_record(data, ctx.guild.id).setdefault("sticky", {})
            old = sticky.get(str(ctx.channel.id), {})
            sticky[str(ctx.channel.id)] = {"content": content[:4000], "message_id": int(old.get("message_id", 0))}
            await self.persist(data)
        await ctx.reply("📌 Sticky message saved. It will remain at the bottom of this channel.")

    @commands.command(name="sticky")
    @staff_command()
    async def sticky(self, ctx: commands.Context, *, message: str) -> None:
        await self.set_sticky(ctx, message)

    @commands.command(name="editsticky")
    @staff_command()
    async def editsticky(self, ctx: commands.Context, *, message: str) -> None:
        await self.set_sticky(ctx, message)

    @commands.command(name="unsticky", aliases=["removesticky"])
    @staff_command()
    async def unsticky(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        async with self.data_lock:
            data = load_data()
            removed = guild_record(data, ctx.guild.id).setdefault("sticky", {}).pop(str(ctx.channel.id), None)
            await self.persist(data)
        if isinstance(removed, dict) and int(removed.get("message_id", 0)):
            try:
                message = await ctx.channel.fetch_message(int(removed["message_id"]))
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await ctx.reply("Sticky removed.")

    @commands.command(name="purge")
    @staff_command()
    async def purge(self, ctx: commands.Context, amount: int) -> None:
        if not isinstance(ctx.channel, discord.TextChannel) or not 1 <= amount <= 1000:
            await ctx.reply("Choose an amount from 1 to 1000.")
            return
        deleted_count = 0
        remaining = amount + 1
        while remaining > 0:
            batch = await ctx.channel.purge(limit=min(100, remaining))
            if not batch:
                break
            deleted_count += len(batch)
            remaining -= len(batch)
            if len(batch) < min(100, remaining + len(batch)):
                break
        moderation = self.bot.get_cog("Moderation")
        if isinstance(moderation, Moderation):
            await moderation.send_mod_log(ctx.guild, "Messages purged", f"{ctx.author.mention} deleted **{max(0, deleted_count - 1)}** message(s) in {ctx.channel.mention}.")
        notice = await ctx.send(f"Deleted **{max(0, deleted_count - 1)}** message(s).")
        await asyncio.sleep(4)
        try:
            await notice.delete()
        except discord.HTTPException:
            pass

    @commands.command(name="to")
    @staff_command()
    async def timeout_member(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason provided") -> None:
        if error := target_error(ctx, member):
            await ctx.reply(error)
            return
        try:
            parsed = parse_timeout_duration(duration)
        except ValueError as error:
            await ctx.reply(str(error))
            return
        until = datetime.now(UTC) + parsed
        await member.timeout(until, reason=f"{reason} — by {ctx.author}")
        await ctx.send(f"Timed out {member.mention} until <t:{int(until.timestamp())}:R>. Reason: {reason}")

    @commands.command(name="rto")
    @staff_command()
    async def remove_timeout(self, ctx: commands.Context, member: discord.Member, *, reason: str = "Timeout removed") -> None:
        if error := target_error(ctx, member):
            await ctx.reply(error)
            return
        await member.timeout(None, reason=f"{reason} — by {ctx.author}")
        await ctx.send(f"Removed {member.mention}'s timeout.")

    @commands.command(name="ban")
    @senior_command()
    async def prefix_ban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided") -> None:
        if error := target_error(ctx, member):
            await ctx.reply(error)
            return
        await member.ban(reason=f"{reason} — by {ctx.author}")
        await ctx.send(f"Banned **{member}**. Reason: {reason}")

    @commands.command(name="kick")
    @senior_command()
    async def prefix_kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided") -> None:
        if error := target_error(ctx, member):
            await ctx.reply(error)
            return
        await member.kick(reason=f"{reason} — by {ctx.author}")
        await ctx.send(f"Kicked **{member}**. Reason: {reason}")

    @commands.command(name="strike")
    @senior_command()
    async def strike(self, ctx: commands.Context, member: discord.Member, *, reason: str) -> None:
        count = await self.add_strike_record(ctx.guild, member, reason, ctx.author)
        await ctx.send(f"Added a strike to {member.mention}. They now have **{count}** strike(s).")

    @commands.command(name="unstrike")
    @senior_command()
    async def unstrike(self, ctx: commands.Context, member: discord.Member, *, reason: str = "Latest strike removed") -> None:
        async with self.data_lock:
            data = load_data()
            entries = guild_record(data, ctx.guild.id).setdefault("strikes", {}).get(str(member.id), [])
            if not entries:
                await ctx.reply("That member has no strikes.")
                return
            entries.pop()
            count = len(entries)
            await self.persist(data)
        await self.send_punishment_log(ctx.guild, action="removed", member=member, reason=reason, source=ctx.author, count=count)
        await ctx.send(f"Removed the latest strike from {member.mention}. **{count}** remain.")

    @commands.command(name="clearstrikes")
    @senior_command()
    async def clearstrikes(self, ctx: commands.Context, member: discord.Member) -> None:
        async with self.data_lock:
            data = load_data()
            removed = len(guild_record(data, ctx.guild.id).setdefault("strikes", {}).pop(str(member.id), []))
            await self.persist(data)
        await self.send_punishment_log(ctx.guild, action="cleared", member=member, reason=f"Cleared {removed} strike(s)", source=ctx.author, count=0)
        await ctx.send(f"Cleared **{removed}** strike(s) from {member.mention}.")

    @commands.command(name="role")
    @staff_command()
    async def role(self, ctx: commands.Context, member: discord.Member, *, role_id_or_name: str) -> None:
        raw = role_id_or_name.strip().strip("<@&>")
        role = ctx.guild.get_role(int(raw)) if raw.isdigit() else discord.utils.find(
            lambda item: normalise_role_name(item.name) == normalise_role_name(raw), ctx.guild.roles
        )
        if role is None or role.is_default():
            await ctx.reply("I could not find that role.")
            return
        if isinstance(ctx.author, discord.Member) and role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            await ctx.reply("You cannot manage a role equal to or above your own.")
            return
        if ctx.guild.me and role >= ctx.guild.me.top_role:
            await ctx.reply("Move the bot role above that role first.")
            return
        if role in member.roles:
            await member.remove_roles(role, reason=f"Role toggled by {ctx.author}")
            action = "Removed"
        else:
            await member.add_roles(role, reason=f"Role toggled by {ctx.author}")
            action = "Added"
        await ctx.send(f"{action} {role.mention} {'from' if action == 'Removed' else 'to'} {member.mention}.")

    @commands.command(name="setnick")
    @staff_command()
    async def setnick(self, ctx: commands.Context, member: discord.Member, *, nickname: str = "") -> None:
        await member.edit(nick=nickname[:32] or None, reason=f"Nickname changed by {ctx.author}")
        await ctx.send(f"{'Reset' if not nickname else 'Updated'} {member.mention}'s nickname.")

    @commands.command(name="manage")
    @senior_command()
    async def manage(self, ctx: commands.Context) -> None:
        await ctx.send(
            embed=discord.Embed(title="Staff & builder management", description="Choose whether to add or remove a role, then enter the member and role IDs.", color=discord.Color.blurple()),
            view=ManageView(ctx.author.id),
        )

    @commands.command(name="ecreate")
    @staff_command()
    async def ecreate(self, ctx: commands.Context) -> None:
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(EmbedEditorModal(self))
        await ctx.send("Open the embed creator:", view=DashboardButtonView(ctx.author.id, "Create embed", callback, "📝"))

    @commands.command(name="eedit")
    @staff_command()
    async def eedit(self, ctx: commands.Context, message_id: int) -> None:
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(EmbedEditorModal(self, message_id))
        await ctx.send("Open the embed editor:", view=DashboardButtonView(ctx.author.id, "Edit embed", callback, "✏️"))

    @commands.command(name="edelete")
    @staff_command()
    async def edelete(self, ctx: commands.Context, message_id: int) -> None:
        try:
            message = await ctx.channel.fetch_message(message_id)
            if message.author.id != self.bot.user.id:
                raise ValueError("I can only delete my own messages.")
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as error:
            await ctx.reply(f"I could not delete that message: {error}")
            return
        await ctx.reply("Message deleted.")

    def giveaways_cog(self) -> Giveaways | None:
        cog = self.bot.get_cog("Giveaways")
        return cog if isinstance(cog, Giveaways) else None

    @commands.command(name="gcreate")
    @staff_command()
    async def prefix_gcreate(self, ctx: commands.Context) -> None:
        cog = self.giveaways_cog()
        if cog is None:
            await ctx.reply("The giveaway system is unavailable.")
            return
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(GiveawayCreateModal(cog))
        await ctx.send("Open the giveaway dashboard:", view=DashboardButtonView(ctx.author.id, "Create giveaway", callback, "🎉"))

    @commands.command(name="qcreate")
    @staff_command()
    async def qcreate(self, ctx: commands.Context) -> None:
        cog = self.giveaways_cog()
        if cog is None:
            await ctx.reply("The giveaway system is unavailable.")
            return
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(QuickdropCreateModal(cog))
        await ctx.send("Open the quickdrop dashboard:", view=DashboardButtonView(ctx.author.id, "Create quickdrop", callback, "⚡"))

    async def end_giveaway_from_ctx(self, ctx: commands.Context, message_id: str, reroll: bool = False) -> None:
        cog = self.giveaways_cog()
        await ctx.reply(await cog.end_giveaway(message_id, reroll=reroll) if cog else "The giveaway system is unavailable.")

    @commands.command(name="gend", aliases=["qend"])
    @staff_command()
    async def prefix_gend(self, ctx: commands.Context, message_id: str) -> None:
        await self.end_giveaway_from_ctx(ctx, message_id)

    @commands.command(name="greroll", aliases=["qreroll"])
    @staff_command()
    async def prefix_greroll(self, ctx: commands.Context, message_id: str) -> None:
        await self.end_giveaway_from_ctx(ctx, message_id, True)

    @commands.command(name="gedit", aliases=["qedit"])
    @staff_command()
    async def prefix_gedit(self, ctx: commands.Context, message_id: str, duration: str, winners: int, *, prize: str) -> None:
        cog = self.giveaways_cog()
        if cog is None:
            await ctx.reply("The giveaway system is unavailable.")
            return
        try:
            seconds = parse_giveaway_duration(duration)
        except ValueError as error:
            await ctx.reply(str(error))
            return
        await ctx.reply(await cog.edit_giveaway(message_id, seconds, winners, prize))

    @commands.command(name="autogcreate")
    @staff_command()
    async def prefix_autogcreate(self, ctx: commands.Context, *, schedule: str = "") -> None:
        cog = self.giveaways_cog()
        if cog is None or ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel):
            await ctx.reply("The giveaway system is unavailable here.")
            return
        try:
            parts = shlex.split(schedule)
            has_start_time = bool(parts and re.fullmatch(r"\d{1,2}:\d{2}", parts[0]))
            minimum = 5 if has_start_time else 4
            if len(parts) < minimum:
                raise ValueError(
                    "Use `!autogcreate [HH:MM] repeat duration winners prize`, for example "
                    "`!autogcreate 18:00 5d 1h 1 20m` or `!autogcreate 5d 1h 1 20m`."
                )
            if has_start_time:
                start_time, repeat, duration, winner_text = parts[:4]
                prize = " ".join(parts[4:])
            else:
                start_time = ""
                repeat, duration, winner_text = parts[:3]
                prize = " ".join(parts[3:])
            winners = int(winner_text)
            interval = parse_giveaway_duration(repeat)
            giveaway_duration = parse_giveaway_duration(duration)
            if not 1 <= winners <= 20:
                raise ValueError("Winners must be from 1 to 20.")
            now = datetime.now(TIMEZONE)
            if start_time:
                match = re.fullmatch(r"(\d{1,2}):(\d{2})", start_time)
                if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
                    raise ValueError("Start time must use 24-hour `HH:MM` format.")
                start = now.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
                if start <= now:
                    start += timedelta(days=1)
            else:
                start = now
        except (ValueError, IndexError) as error:
            await ctx.reply(str(error))
            return
        schedule_id = uuid.uuid4().hex[:8]
        await cog.save_auto_schedule(
            schedule_id=schedule_id,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            host_id=ctx.author.id,
            interval_seconds=interval,
            duration_seconds=giveaway_duration,
            prize=prize,
            winner_count=winners,
            next_at=int(start.timestamp()),
        )
        await ctx.reply(f"Auto giveaway `{schedule_id}` will first post <t:{int(start.timestamp())}:F> and repeat every `{repeat}`.")

    @commands.command(name="rautogcreate")
    @staff_command()
    async def rautogcreate(self, ctx: commands.Context, job_id: str = "") -> None:
        cog = self.giveaways_cog()
        if cog is None or ctx.guild is None:
            await ctx.reply("The giveaway system is unavailable.")
            return
        from cogs.giveaways import load_data as load_giveaway_data
        schedules = load_giveaway_data().get("auto", {})
        if not job_id:
            lines = [f"`{key}` — {entry.get('prize')} — next <t:{entry.get('next_at')}:R>" for key, entry in schedules.items() if isinstance(entry, dict) and entry.get("active") and entry.get("guild_id") == ctx.guild.id]
            await ctx.reply("\n".join(lines) or "No automatic giveaway jobs are active.")
            return
        async with cog.data_lock:
            data = load_giveaway_data()
            entry = data.get("auto", {}).get(job_id)
            if not isinstance(entry, dict) or entry.get("guild_id") != ctx.guild.id:
                await ctx.reply("That automatic giveaway job was not found.")
                return
            entry["active"] = False
            await cog.save(data)
        await ctx.reply(f"Removed automatic giveaway job `{job_id}`.")

    @tasks.loop(seconds=60)
    async def activity_loop(self) -> None:
        now = int(datetime.now(UTC).timestamp())
        for guild in self.bot.guilds:
            data = load_data()
            activity = guild_record(data, guild.id).setdefault("activity", {})
            active = activity.get("active_check")
            if isinstance(active, dict) and not active.get("closed") and int(active.get("closes_at", 0)) <= now:
                await self.finish_activity_check(guild, active)
                data = load_data()
                activity = guild_record(data, guild.id).setdefault("activity", {})
            if int(activity.get("next_post_at", now + 60)) <= now and not (
                isinstance(activity.get("active_check"), dict) and not activity["active_check"].get("closed")
            ):
                await self.post_activity_check(guild)

    @activity_loop.before_loop
    async def before_activity_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def post_activity_check(self, guild: discord.Guild) -> None:
        channel = await self.ensure_staff_channel(guild, STAFF_ACTIVITY_CHANNEL)
        if channel is None:
            return
        ping_role = discord.utils.find(
            lambda role: normalise_role_name(role.name) == normalise_role_name(STAFF_PING_ROLE),
            guild.roles,
        )
        eligible = [
            member.id
            for member in guild.members
            if not member.bot and member_has_staff_role(member) and not member_has_named_role(member, senior_role_names())
        ]
        content = (
            f"{ping_role.mention if ping_role else '@Staff Team'} **activity check**\n\n"
            "(whoever doesn't react to the ✅ within 24 hours will get a strike)"
        )
        message = await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions(roles=[ping_role] if ping_role else False, users=False, everyone=False),
        )
        await message.add_reaction("✅")
        now = int(datetime.now(UTC).timestamp())
        async with self.data_lock:
            data = load_data()
            activity = guild_record(data, guild.id).setdefault("activity", {})
            activity["active_check"] = {
                "message_id": message.id,
                "channel_id": channel.id,
                "posted_at": now,
                "closes_at": now + ACTIVITY_WINDOW_SECONDS,
                "eligible_ids": eligible,
                "closed": False,
            }
            activity["next_post_at"] = now + ACTIVITY_INTERVAL_SECONDS
            await self.persist(data)
        log.info("Posted staff activity check in %s for %d eligible member(s)", guild.name, len(eligible))

    async def finish_activity_check(self, guild: discord.Guild, check: dict) -> None:
        channel = guild.get_channel(int(check.get("channel_id", 0)))
        if not isinstance(channel, discord.TextChannel):
            async with self.data_lock:
                data = load_data()
                current = guild_record(data, guild.id).setdefault("activity", {}).get("active_check")
                if isinstance(current, dict):
                    current["closed"] = True
                    current["closed_at"] = int(datetime.now(UTC).timestamp())
                    current["error"] = "Activity channel disappeared; no strikes were applied."
                    await self.persist(data)
            return
        try:
            message = await channel.fetch_message(int(check.get("message_id", 0)))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Skipped activity strikes because the check message could not be read in %s", guild.name)
            reacted: set[int] | None = None
        else:
            reacted = set()
            reaction = discord.utils.find(lambda item: str(item.emoji) == "✅", message.reactions)
            if reaction:
                async for user in reaction.users(limit=None):
                    if not user.bot:
                        reacted.add(user.id)
        missed: list[discord.Member] = []
        if reacted is not None:
            for member_id in check.get("eligible_ids", []):
                member = guild.get_member(int(member_id))
                if (
                    member
                    and member.id not in reacted
                    and member_has_staff_role(member)
                    and not member_has_named_role(member, senior_role_names())
                ):
                    missed.append(member)
            for member in missed:
                await self.add_strike_record(guild, member, "Missed the 24-hour staff activity check", None)
        async with self.data_lock:
            data = load_data()
            activity = guild_record(data, guild.id).setdefault("activity", {})
            current = activity.get("active_check")
            if isinstance(current, dict) and int(current.get("message_id", 0)) == int(check.get("message_id", 0)):
                current["closed"] = True
                current["closed_at"] = int(datetime.now(UTC).timestamp())
                current["missed_ids"] = [member.id for member in missed]
                await self.persist(data)
        try:
            await channel.send(
                f"✅ Activity check closed. **{len(missed)}** eligible staff member(s) missed it and received a strike.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, commands.CheckFailure):
            message = str(original) or "You do not have permission to use that command."
        elif isinstance(original, commands.MissingRequiredArgument):
            message = f"Missing `{original.param.name}`. Use `!help` or check `#staff-commands` for the format."
        elif isinstance(original, (commands.BadArgument, commands.MemberNotFound, commands.RoleNotFound)):
            message = f"I could not understand that member, role, number, or time: {original}"
        elif isinstance(original, discord.Forbidden):
            message = "Discord blocked that action. Check the bot role and channel permissions."
        else:
            log.exception("Staff command failed", exc_info=original)
            message = f"That command failed: {original}"
        try:
            await ctx.reply(message, mention_author=False)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffTools(bot))
