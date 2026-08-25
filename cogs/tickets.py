import asyncio
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
    senior_only,
    staff_role_names,
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


def safe_channel_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]", "-", value.casefold())
    return re.sub(r"-+", "-", cleaned).strip("-")[:40] or "member"


def ticket_owner_id(channel: discord.TextChannel) -> int | None:
    match = re.search(r"density-ticket-owner:(\d+)", channel.topic or "")
    return int(match.group(1)) if match else None


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
        await self.cog.create_ticket(interaction, self.values[0])


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
        owner = interaction.guild.get_member(owner_id) if interaction.guild else None
        if owner:
            overwrite = interaction.channel.overwrites_for(owner)
            overwrite.send_messages = False
            await interaction.channel.set_permissions(owner, overwrite=overwrite)
        new_name = interaction.channel.name
        if not new_name.startswith("closed-"):
            new_name = f"closed-{new_name}"[:100]
        await interaction.channel.edit(
            name=new_name,
            topic=(interaction.channel.topic or "").replace(
                "density-ticket-open:true",
                "density-ticket-open:false",
            ),
            reason=f"Ticket closed by {interaction.user}",
        )
        await interaction.channel.send(
            f"🔒 Ticket closed by {interaction.user.mention}. Staff can review it before deleting it.",
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )
        await interaction.followup.send("Ticket closed.", ephemeral=True)

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
        names = staff_role_names()
        return {
            role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
            for role in guild.roles
            if normalise_role_name(role.name) in names
        }

    async def create_ticket(self, interaction: discord.Interaction, ticket_type: str) -> None:
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
        await channel.send(
            content=interaction.user.mention,
            embed=embed,
            view=CloseTicketView(self),
            allowed_mentions=discord.AllowedMentions(users=[interaction.user], roles=False, everyone=False),
        )
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
