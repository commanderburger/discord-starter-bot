import asyncio
import io
import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands

from cogs.permissions import (
    SeniorStaffOnly,
    member_is_staff,
    normalise_role_name,
    role_is_staff,
    senior_only,
)


log = logging.getLogger("starter-bot.tickets")
PANEL_MARKER = "Density SMP Tickets v1"
TICKET_CHANNEL_NAMES = os.getenv("TICKET_CHANNEL_NAMES", "ticket,tickets")
TICKET_CATEGORY_NAME = os.getenv("TICKET_CATEGORY_NAME", "Tickets")
TICKET_TYPES = {
    "support": ("Support", "❓", "Tell us what you need help with and a staff member will reply."),
    "partnership": ("Partnerships", "🤝", "Tell us about your partnership proposal."),
    "bug": ("Bug Report", "🛠️", "Explain the bug, what you expected and how to reproduce it."),
}
TICKET_LOG_CHANNELS = {
    "support": os.getenv("TICKET_LOG_SUPPORT_CHANNEL", "ticket-logs-support"),
    "partnership": os.getenv("TICKET_LOG_PARTNERSHIPS_CHANNEL", "ticket-logs-partnerships"),
    "bug": os.getenv("TICKET_LOG_BUG_REPORT_CHANNEL", "ticket-logs-bug-report"),
}
MAX_TRANSCRIPT_BYTES = 7_500_000


def safe_channel_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]", "-", value.casefold())
    return re.sub(r"-+", "-", cleaned).strip("-")[:40] or "member"


def ticket_owner_id(channel: discord.TextChannel) -> int | None:
    match = re.search(r"density-ticket-owner:(\d+)", channel.topic or "")
    return int(match.group(1)) if match else None


def ticket_type_id(channel: discord.TextChannel) -> str | None:
    match = re.search(r"density-ticket-type:([a-z]+)", channel.topic or "")
    return match.group(1) if match else None


def find_named_text_channel(guild: discord.Guild, configured: str) -> discord.TextChannel | None:
    wanted = {
        normalise_role_name(name)
        for name in configured.split(",")
        if normalise_role_name(name)
    }
    return discord.utils.find(
        lambda channel: isinstance(channel, discord.TextChannel)
        and normalise_role_name(channel.name) in wanted,
        guild.channels,
    )


class TicketTypeSelect(discord.ui.Select):
    def __init__(self, cog: "Tickets") -> None:
        self.cog = cog
        options = [
            discord.SelectOption(
                label="Support",
                value="support",
                description="Get help from the Density SMP staff team",
                emoji="❓",
            ),
            discord.SelectOption(
                label="Partnerships",
                value="partnership",
                description="Discuss a server or creator partnership",
                emoji="🤝",
            ),
            discord.SelectOption(
                label="Bug Report",
                value="bug",
                description="Report a Minecraft, Discord or website problem",
                emoji="🛠️",
            ),
        ]
        super().__init__(
            placeholder="Choose the type of ticket you need…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="density-ticket-type-v1",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ticket_type = self.values[0]
        if ticket_type == "bug":
            await interaction.response.send_modal(BugReportModal(self.cog))
            return
        await self.cog.create_ticket(interaction, ticket_type)


class BugReportModal(discord.ui.Modal):
    def __init__(self, cog: "Tickets") -> None:
        super().__init__(
            title="Open a bug report",
            custom_id="density-bug-report-modal-v1",
        )
        self.cog = cog
        self.issue = discord.ui.TextInput(
            label="What is the issue?",
            placeholder="Explain the bug and what you expected to happen",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=1000,
            required=True,
        )
        self.ign = discord.ui.TextInput(
            label="What is your Minecraft IGN?",
            placeholder="Your exact in-game name",
            min_length=1,
            max_length=32,
            required=True,
        )
        self.clip_link = discord.ui.TextInput(
            label="Clip link (optional)",
            placeholder="YouTube, Medal, Streamable or another clip link",
            max_length=500,
            required=False,
        )
        self.add_item(self.issue)
        self.add_item(self.ign)
        self.add_item(self.clip_link)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_ticket(
            interaction,
            "bug",
            answers={
                "issue": str(self.issue.value).strip(),
                "ign": str(self.ign.value).strip(),
                "clip_link": str(self.clip_link.value).strip(),
            },
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Bug report form failed", exc_info=error)
        message = "I could not create that bug report. Please try again or contact staff."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect(cog))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        del item
        log.exception("Ticket creation failed", exc_info=error)
        message = "I could not create that ticket. Check my channel permissions and try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class CloseTicketView(discord.ui.View):
    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Close ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="density-ticket-close-v1",
    )
    async def close_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
            return
        owner_id = ticket_owner_id(interaction.channel)
        if owner_id is None:
            await interaction.response.send_message("This is not one of my ticket channels.", ephemeral=True)
            return
        if interaction.user.id != owner_id and not member_is_staff(interaction):
            await interaction.response.send_message(
                "Only the ticket owner or a staff member can close this ticket.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        ticket_type = ticket_type_id(interaction.channel)
        if interaction.guild is None or ticket_type not in TICKET_TYPES:
            await interaction.followup.send(
                "I could not identify this ticket type, so I did not close it.",
                ephemeral=True,
            )
            return
        try:
            log_message = await self.cog.file_ticket_log(
                interaction.channel,
                owner_id,
                interaction.user,
                ticket_type,
            )
        except RuntimeError as error:
            await interaction.followup.send(
                f"I did not close this ticket: {error}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Ticket transcript filed in {log_message.channel.mention}. Deleting this ticket now.",
            ephemeral=True,
        )
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        del item
        log.exception("Ticket close failed", exc_info=error)
        message = "I could not close that ticket. Check my channel permissions and try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.panel_lock = asyncio.Lock()
        self.panel_checked = False
        bot.add_view(TicketPanelView(self))
        bot.add_view(CloseTicketView(self))

    def ticket_category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        wanted = normalise_role_name(TICKET_CATEGORY_NAME)
        return discord.utils.find(
            lambda category: normalise_role_name(category.name) == wanted,
            guild.categories,
        )

    def staff_overwrites(self, guild: discord.Guild) -> dict[discord.Role, discord.PermissionOverwrite]:
        return {
            role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
            for role in guild.roles
            if role_is_staff(role)
        }

    def ticket_log_channel(
        self,
        guild: discord.Guild,
        ticket_type: str,
    ) -> discord.TextChannel | None:
        channel_name = TICKET_LOG_CHANNELS.get(ticket_type)
        if channel_name is None:
            return None
        wanted = normalise_role_name(channel_name)
        return discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel)
            and normalise_role_name(channel.name) == wanted,
            guild.channels,
        )

    async def build_transcript(
        self,
        channel: discord.TextChannel,
        owner_id: int,
        closed_by: discord.abc.User,
        ticket_type: str,
    ) -> tuple[bytes, int]:
        label = TICKET_TYPES[ticket_type][0]
        owner = channel.guild.get_member(owner_id)
        owner_text = f"{owner} ({owner_id})" if owner else str(owner_id)
        lines = [
            "DENSITY SMP TICKET TRANSCRIPT",
            f"Ticket: #{channel.name} ({channel.id})",
            f"Type: {label}",
            f"Opened by: {owner_text}",
            f"Closed by: {closed_by} ({closed_by.id})",
            f"Created: {channel.created_at.isoformat()}",
            f"Closed: {discord.utils.utcnow().isoformat()}",
            "",
            "MESSAGES",
            "========",
        ]
        message_count = 0
        async for message in channel.history(limit=5000, oldest_first=True):
            message_count += 1
            content = message.clean_content or "<no text>"
            lines.append(
                f"[{message.created_at.isoformat()}] "
                f"{message.author} ({message.author.id}): {content}"
            )
            for attachment in message.attachments:
                lines.append(f"  [attachment] {attachment.filename}: {attachment.url}")
            for embed in message.embeds:
                if embed.title:
                    lines.append(f"  [embed title] {embed.title}")
                if embed.description:
                    lines.append(f"  [embed description] {embed.description}")
                for field in embed.fields:
                    lines.append(f"  [embed field] {field.name}: {field.value}")

        transcript = "\n".join(lines).encode("utf-8")
        if len(transcript) > MAX_TRANSCRIPT_BYTES:
            ending = b"\n\n[Transcript truncated because it exceeded the Discord upload limit.]\n"
            transcript = transcript[: MAX_TRANSCRIPT_BYTES - len(ending)]
            transcript = transcript.decode("utf-8", errors="ignore").encode("utf-8") + ending
        return transcript, message_count

    async def file_ticket_log(
        self,
        channel: discord.TextChannel,
        owner_id: int,
        closed_by: discord.abc.User,
        ticket_type: str,
    ) -> discord.Message:
        log_channel = self.ticket_log_channel(channel.guild, ticket_type)
        if log_channel is None:
            expected_name = TICKET_LOG_CHANNELS.get(ticket_type, "ticket log channel")
            raise RuntimeError(f"Could not find #{expected_name}")

        transcript, message_count = await self.build_transcript(
            channel,
            owner_id,
            closed_by,
            ticket_type,
        )
        owner = channel.guild.get_member(owner_id)
        label = TICKET_TYPES[ticket_type][0]
        summary = discord.Embed(
            title=f"{label} ticket closed",
            description=(
                f"**Ticket:** `#{channel.name}`\n"
                f"**Opened by:** {owner.mention if owner else owner_id}\n"
                f"**Closed by:** {closed_by.mention}\n"
                f"**Messages:** {message_count}"
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        summary.set_footer(text=f"Ticket ID: {channel.id}")
        filename = f"{safe_channel_name(channel.name)}-{channel.id}.txt"
        return await log_channel.send(
            embed=summary,
            file=discord.File(io.BytesIO(transcript), filename=filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def file_ticket_opened(
        self,
        channel: discord.TextChannel,
        opened_by: discord.Member,
        ticket_type: str,
    ) -> discord.Message:
        log_channel = self.ticket_log_channel(channel.guild, ticket_type)
        if log_channel is None:
            expected_name = TICKET_LOG_CHANNELS.get(ticket_type, "ticket log channel")
            raise RuntimeError(f"Could not find #{expected_name}")
        label, emoji, _ = TICKET_TYPES[ticket_type]
        embed = discord.Embed(
            title=f"{emoji} {label} ticket opened",
            description=(
                f"**Ticket:** {channel.mention}\n"
                f"**Opened by:** {opened_by.mention}\n"
                f"**User ID:** `{opened_by.id}`"
            ),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Ticket ID: {channel.id}")
        return await log_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def create_ticket(
        self,
        interaction: discord.Interaction,
        ticket_type: str,
        answers: dict[str, str] | None = None,
    ) -> None:
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
            or ticket_type not in TICKET_TYPES
        ):
            await interaction.response.send_message("Tickets can only be opened in Density SMP.", ephemeral=True)
            return

        existing = discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel)
            and ticket_owner_id(channel) == interaction.user.id
            and "density-ticket-open:true" in (channel.topic or ""),
            interaction.guild.channels,
        )
        if existing:
            await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        log_channel = self.ticket_log_channel(interaction.guild, ticket_type)
        if log_channel is None:
            expected_name = TICKET_LOG_CHANNELS[ticket_type]
            await interaction.followup.send(
                f"Tickets are temporarily unavailable because #{expected_name} is missing.",
                ephemeral=True,
            )
            return
        category = self.ticket_category(interaction.guild)
        if category is None:
            category = await interaction.guild.create_category(
                TICKET_CATEGORY_NAME,
                reason="Density SMP ticket system setup",
            )

        bot_member = interaction.guild.me
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
            **self.staff_overwrites(interaction.guild),
        }
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
            )

        label, emoji, instructions = TICKET_TYPES[ticket_type]
        channel = await interaction.guild.create_text_channel(
            f"ticket-{ticket_type}-{safe_channel_name(interaction.user.display_name)}"[:100],
            category=category,
            topic=(
                f"density-ticket-owner:{interaction.user.id} "
                f"density-ticket-type:{ticket_type} density-ticket-open:true"
            ),
            overwrites=overwrites,
            reason=f"{label} ticket opened by {interaction.user}",
        )
        embed = discord.Embed(
            title=f"{emoji} {label} ticket",
            description=(
                f"Welcome {interaction.user.mention}! {instructions}\n\n"
                "Please include all useful details. A staff member will be with you soon."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Density SMP Tickets")
        if ticket_type == "bug" and answers:
            embed.add_field(name="Minecraft IGN", value=answers["ign"], inline=False)
            embed.add_field(name="Issue", value=answers["issue"], inline=False)
            embed.add_field(
                name="Clip",
                value=(
                    answers["clip_link"]
                    or "No clip link supplied — please upload a clip in this ticket if possible."
                ),
                inline=False,
            )
        await channel.send(
            content=interaction.user.mention,
            embed=embed,
            view=CloseTicketView(self),
            allowed_mentions=discord.AllowedMentions(users=[interaction.user], roles=False, everyone=False),
        )
        await self.file_ticket_opened(channel, interaction.user, ticket_type)
        await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)

    def panel_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Tickets",
            description=(
                "Choose the option below that best matches what you need.\n\n"
                "❓ **Support** — Help with Density SMP\n"
                "🤝 **Partnerships** — Partnership enquiries\n"
                "🛠️ **Bug Report** — Report a problem"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=PANEL_MARKER)
        return embed

    async def send_panel(self, channel: discord.TextChannel) -> discord.Message:
        return await channel.send(embed=self.panel_embed(), view=TicketPanelView(self))

    async def ensure_panel(self) -> None:
        async with self.panel_lock:
            for guild in self.bot.guilds:
                channel = find_named_text_channel(guild, TICKET_CHANNEL_NAMES)
                if channel is None:
                    log.warning("No ticket channel found in %s", guild.name)
                    continue
                found = False
                try:
                    async for message in channel.history(limit=50):
                        if message.author.id != self.bot.user.id:
                            continue
                        if any(embed.footer and embed.footer.text == PANEL_MARKER for embed in message.embeds):
                            found = True
                            break
                except discord.HTTPException:
                    log.warning("Could not read ticket channel history in %s", guild.name)
                    continue
                if not found:
                    await self.send_panel(channel)
                    log.info("Posted the Density SMP ticket panel in #%s", channel.name)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.panel_checked:
            return
        self.panel_checked = True
        try:
            await self.ensure_panel()
        except discord.HTTPException:
            self.panel_checked = False
            log.exception("Could not set up the ticket panel")

    @app_commands.command(name="ticketsetup", description="Post the Density SMP ticket panel here")
    @senior_only()
    async def ticketsetup(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use this in the ticket text channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        message = await self.send_panel(interaction.channel)
        await interaction.followup.send(f"Ticket panel posted: {message.jump_url}", ephemeral=True)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, SeniorStaffOnly):
            message = "Only Manager, Co-Owner and Owner roles can set up the ticket panel."
        elif isinstance(error, discord.Forbidden):
            message = "I am missing permission to create or manage ticket channels."
        else:
            log.exception("Ticket command failed", exc_info=error)
            message = f"That ticket action failed: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
