import asyncio
import io
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
from discord.ext import commands

from cogs.permissions import member_is_senior, normalise_role_name, role_is_staff


log = logging.getLogger("starter-bot.applications")
DATA_FILE = Path(os.getenv("BOT_DATA_DIR", "/data")) / "staff-applications.json"
PANEL_CHANNEL_NAME = os.getenv("STAFF_APPLICATION_PANEL_CHANNEL", "application-for-staff")
APPLICATION_CATEGORY_NAME = os.getenv("STAFF_APPLICATION_CATEGORY", "Applications")
PENDING_CHANNEL_NAME = os.getenv("STAFF_APPLICATION_PENDING_CHANNEL", "pending")
ACCEPTED_CHANNEL_NAME = os.getenv("STAFF_APPLICATION_ACCEPTED_CHANNEL", "accepted")
DENIED_CHANNEL_NAME = os.getenv("STAFF_APPLICATION_DENIED_CHANNEL", "denied")
PARTNER_MANAGER_ROLE_NAME = os.getenv("PARTNER_MANAGER_ROLE", "Partner Manager")
STAFF_TEAM_ROLE_NAME = os.getenv("STAFF_TEAM_ROLE", "Staff Team")
PANEL_MARKER = "Density Partner Manager Applications v1"
try:
    REAPPLY_DAYS = max(1, int(os.getenv("STAFF_APPLICATION_REAPPLY_DAYS", "14")))
except ValueError:
    REAPPLY_DAYS = 14


QUESTIONS = (
    "What is your Minecraft IGN?",
    "How old are you?",
    "What is your timezone?",
    "Why do you want to become a Partner Manager for Density SMP?",
    (
        "What partnership or staff experience do you have in other Discord servers? "
        "Describe your roles and attach permanent invite links to those servers."
    ),
    "How active can you be each day and each week?",
    "Can you complete at least five successful partnerships every week? Explain how you will meet this target.",
    "How would you find and approach a possible partner server?",
    "Explain what you understand about Density SMP's partnership member and ping rules.",
    "What would you do if you were unsure whether a partnership should be accepted?",
    "Why should we choose you, and is there anything else we should know?",
)


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
        {"applications": {}, "denied_until": {}, "panel_message_id": None},
    )


def find_text_channel(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    wanted = normalise_role_name(name)
    return discord.utils.find(
        lambda channel: isinstance(channel, discord.TextChannel)
        and normalise_role_name(channel.name) == wanted,
        guild.channels,
    )


def find_category(guild: discord.Guild, name: str) -> discord.CategoryChannel | None:
    wanted = normalise_role_name(name)
    return discord.utils.find(
        lambda category: normalise_role_name(category.name) == wanted,
        guild.categories,
    )


def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    wanted = normalise_role_name(name)
    return discord.utils.find(lambda role: normalise_role_name(role.name) == wanted, guild.roles)


def private_staff_overwrites(
    guild: discord.Guild,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
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


def public_panel_overwrites(
    guild: discord.Guild,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
        ),
    }
    for role in guild.roles:
        if role_is_staff(role):
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
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


def application_text(record: dict) -> str:
    answers = record.get("answers", [])
    lines = [
        "Density SMP Partner Manager Application",
        f"Applicant: {record.get('applicant_name', 'Unknown')}",
        f"Discord user ID: {record.get('applicant_id', 'Unknown')}",
        f"Submitted: {record.get('submitted_at', 'Unknown')}",
        "",
    ]
    for index, question in enumerate(QUESTIONS):
        answer = answers[index] if index < len(answers) else "No answer"
        lines.extend((f"Question {index + 1}: {question}", f"Answer: {answer}", ""))
    return "\n".join(lines)


def application_file(record: dict) -> discord.File:
    content = application_text(record).encode("utf-8")
    applicant_id = record.get("applicant_id", "unknown")
    return discord.File(io.BytesIO(content), filename=f"partner-manager-{applicant_id}.txt")


def application_embed(record: dict, status: str, reviewer: discord.abc.User | None = None) -> discord.Embed:
    colours = {
        "pending": discord.Color.blurple(),
        "accepted": discord.Color.green(),
        "denied": discord.Color.red(),
    }
    titles = {
        "pending": "Partner Manager Application • Pending",
        "accepted": "Partner Manager Application • Accepted",
        "denied": "Partner Manager Application • Denied",
    }
    applicant_id = int(record["applicant_id"])
    embed = discord.Embed(
        title=titles[status],
        description=f"Applicant: <@{applicant_id}> (`{applicant_id}`)",
        color=colours[status],
        timestamp=datetime.fromisoformat(record["submitted_at"]),
    )
    answers = record.get("answers", [])
    for index, question in enumerate(QUESTIONS):
        answer = answers[index] if index < len(answers) else "No answer"
        shortened = answer if len(answer) <= 400 else f"{answer[:397]}..."
        embed.add_field(name=f"{index + 1}. {question}", value=shortened or "No answer", inline=False)
    if reviewer is not None:
        embed.add_field(name="Reviewed by", value=f"{reviewer.mention} (`{reviewer.id}`)", inline=False)
    if status == "denied" and record.get("denial_reason"):
        embed.add_field(name="Reason", value=str(record["denial_reason"])[:1024], inline=False)
    embed.set_footer(text=f"Application ID: {record['id']} • Full answers attached")
    return embed


class ApplicationPanelView(discord.ui.View):
    def __init__(self, cog: "Applications") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Apply for Partner Manager",
        emoji="🤝",
        style=discord.ButtonStyle.primary,
        custom_id="density-partner-manager-apply-v1",
    )
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.start_application(interaction)


class DenialReasonModal(discord.ui.Modal, title="Deny Partner Manager Application"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Explain why the application was denied.",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(self, cog: "Applications", pending_message_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.pending_message_id = pending_message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.deny_application(interaction, self.pending_message_id, str(self.reason.value))


class ApplicationReviewView(discord.ui.View):
    def __init__(self, cog: "Applications") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Accept",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="density-partner-manager-accept-v1",
    )
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not member_is_senior(interaction):
            await interaction.response.send_message(
                "Only Owner, Co-Owner, or Manager can review staff applications.",
                ephemeral=True,
            )
            return
        if interaction.message is None:
            await interaction.response.send_message("I could not find this application.", ephemeral=True)
            return
        await self.cog.accept_application(interaction, interaction.message.id)

    @discord.ui.button(
        label="Deny",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="density-partner-manager-deny-v1",
    )
    async def deny(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not member_is_senior(interaction):
            await interaction.response.send_message(
                "Only Owner, Co-Owner, or Manager can review staff applications.",
                ephemeral=True,
            )
            return
        if interaction.message is None:
            await interaction.response.send_message("I could not find this application.", ephemeral=True)
            return
        await interaction.response.send_modal(DenialReasonModal(self.cog, interaction.message.id))


class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.data = load_data()
        self.in_progress: set[tuple[int, int]] = set()
        self.ready_lock = asyncio.Lock()
        self.decision_lock = asyncio.Lock()
        self.ready_complete = False
        self.bot.add_view(ApplicationPanelView(self))
        self.bot.add_view(ApplicationReviewView(self))

    def record_for_message(self, guild_id: int, message_id: int) -> dict | None:
        guild_data = guild_record(self.data, guild_id)
        return next(
            (
                record
                for record in guild_data.get("applications", {}).values()
                if record.get("pending_message_id") == message_id and record.get("status") == "pending"
            ),
            None,
        )

    async def ensure_channels(self, guild: discord.Guild) -> dict[str, discord.TextChannel]:
        category = find_category(guild, APPLICATION_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(
                APPLICATION_CATEGORY_NAME,
                reason="Density staff applications",
            )

        panel = find_text_channel(guild, PANEL_CHANNEL_NAME)
        if panel is None:
            panel = await guild.create_text_channel(
                PANEL_CHANNEL_NAME,
                category=category,
                overwrites=public_panel_overwrites(guild),
                reason="Density Partner Manager application panel",
            )

        channels = {"panel": panel}
        for status, name in (
            ("pending", PENDING_CHANNEL_NAME),
            ("accepted", ACCEPTED_CHANNEL_NAME),
            ("denied", DENIED_CHANNEL_NAME),
        ):
            channel = find_text_channel(guild, name)
            if channel is None:
                channel = await guild.create_text_channel(
                    name,
                    category=category,
                    overwrites=private_staff_overwrites(guild),
                    reason=f"Density staff applications: {status}",
                )
            channels[status] = channel
        return channels

    async def ensure_panel(self, guild: discord.Guild, panel: discord.TextChannel) -> None:
        guild_data = guild_record(self.data, guild.id)
        stored_id = guild_data.get("panel_message_id")
        if stored_id:
            try:
                await panel.fetch_message(int(stored_id))
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                guild_data["panel_message_id"] = None

        async for message in panel.history(limit=50):
            if message.author.id == self.bot.user.id and any(
                embed.footer and embed.footer.text == PANEL_MARKER for embed in message.embeds
            ):
                guild_data["panel_message_id"] = message.id
                save_data(self.data)
                return

        embed = discord.Embed(
            title="🤝 Partner Manager Applications",
            description=(
                "Press the button below to apply for **Partner Manager**.\n\n"
                "• The application is completed privately in DMs.\n"
                "• You will receive one question at a time.\n"
                "• Staff will review the finished application.\n"
                f"• If denied, you must wait **{REAPPLY_DAYS} days** before applying again.\n\n"
                "Make sure your DMs are open before starting."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=PANEL_MARKER)
        message = await panel.send(embed=embed, view=ApplicationPanelView(self))
        guild_data["panel_message_id"] = message.id
        save_data(self.data)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with self.ready_lock:
            if self.ready_complete:
                return
            self.ready_complete = True
            for guild in self.bot.guilds:
                try:
                    channels = await self.ensure_channels(guild)
                    await self.ensure_panel(guild, channels["panel"])
                except discord.Forbidden:
                    log.warning("Missing permission to set up staff applications in %s", guild.name)
                except discord.HTTPException:
                    log.exception("Could not set up staff applications in %s", guild.name)

    async def start_application(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this button in the Density SMP server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id
        key = (guild_id, user_id)
        guild_data = guild_record(self.data, guild_id)
        now = datetime.now(UTC)

        denied_until_raw = guild_data.get("denied_until", {}).get(str(user_id))
        if denied_until_raw:
            try:
                denied_until = datetime.fromisoformat(denied_until_raw)
            except ValueError:
                denied_until = now
            if denied_until > now:
                await interaction.response.send_message(
                    f"You can apply again <t:{int(denied_until.timestamp())}:R>.",
                    ephemeral=True,
                )
                return

        existing = next(
            (
                record
                for record in guild_data.get("applications", {}).values()
                if int(record.get("applicant_id", 0)) == user_id
                and record.get("status") in {"pending", "accepted"}
            ),
            None,
        )
        if existing:
            message = (
                "Your application is already waiting for staff review."
                if existing["status"] == "pending"
                else "Your Partner Manager application has already been accepted."
            )
            await interaction.response.send_message(message, ephemeral=True)
            return
        if key in self.in_progress:
            await interaction.response.send_message(
                "You already have an application in progress. Check your DMs.",
                ephemeral=True,
            )
            return

        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                embed=discord.Embed(
                    title="Partner Manager Application",
                    description=(
                        "I will send one question at a time. Reply in this DM to continue. "
                        "Type `cancel` at any time to stop. You have 15 minutes for each answer."
                    ),
                    color=discord.Color.blurple(),
                )
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot DM you. Enable direct messages from server members, then press the button again.",
                ephemeral=True,
            )
            return

        self.in_progress.add(key)
        await interaction.response.send_message(
            "Application started — check your DMs from Density Bot.",
            ephemeral=True,
        )
        asyncio.create_task(
            self.run_questions(interaction.guild, interaction.user, dm),
            name=f"partner-manager-application-{guild_id}-{user_id}",
        )

    async def run_questions(
        self,
        guild: discord.Guild,
        applicant: discord.Member,
        dm: discord.DMChannel,
    ) -> None:
        key = (guild.id, applicant.id)
        answers: list[str] = []
        try:
            for index, question in enumerate(QUESTIONS, start=1):
                embed = discord.Embed(
                    title=f"Question {index} of {len(QUESTIONS)}",
                    description=question,
                    color=discord.Color.blurple(),
                )
                await dm.send(embed=embed)

                def check(message: discord.Message) -> bool:
                    return message.author.id == applicant.id and message.channel.id == dm.id

                while True:
                    try:
                        message = await self.bot.wait_for("message", timeout=900, check=check)
                    except TimeoutError:
                        await dm.send("Your application timed out. Press the application button to start again.")
                        return
                    if message.content.strip().casefold() == "cancel":
                        await dm.send("Your application was cancelled. You can start again from the panel.")
                        return
                    answer_parts = [message.content.strip()]
                    answer_parts.extend(attachment.url for attachment in message.attachments)
                    answer = "\n".join(part for part in answer_parts if part).strip()
                    if not answer:
                        await dm.send("Please send a written answer before continuing.")
                        continue
                    answers.append(answer[:2000])
                    break

            record = {
                "id": uuid.uuid4().hex[:12],
                "applicant_id": applicant.id,
                "applicant_name": str(applicant),
                "answers": answers,
                "status": "pending",
                "submitted_at": datetime.now(UTC).isoformat(),
                "pending_message_id": None,
            }
            channels = await self.ensure_channels(guild)
            pending_message = await channels["pending"].send(
                content=f"New Partner Manager application from {applicant.mention}",
                embed=application_embed(record, "pending"),
                file=application_file(record),
                view=ApplicationReviewView(self),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            record["pending_message_id"] = pending_message.id
            guild_record(self.data, guild.id).setdefault("applications", {})[record["id"]] = record
            save_data(self.data)
            await dm.send(
                embed=discord.Embed(
                    title="Application submitted",
                    description="Your Partner Manager application was submitted successfully. Staff will review it.",
                    color=discord.Color.green(),
                )
            )
        except discord.Forbidden:
            log.info("Applicant %s closed DMs during an application", applicant.id)
        except discord.HTTPException:
            log.exception("Partner Manager application failed for %s", applicant.id)
            try:
                await dm.send("I could not submit your application. Please tell a staff member.")
            except discord.HTTPException:
                pass
        finally:
            self.in_progress.discard(key)

    async def accept_application(self, interaction: discord.Interaction, message_id: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This application is no longer available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.decision_lock:
            record = self.record_for_message(interaction.guild.id, message_id)
            if record is None:
                await interaction.followup.send("This application has already been reviewed.", ephemeral=True)
                return
            try:
                applicant = interaction.guild.get_member(int(record["applicant_id"])) or await interaction.guild.fetch_member(
                    int(record["applicant_id"])
                )
            except discord.NotFound:
                await interaction.followup.send("The applicant is no longer in the server.", ephemeral=True)
                return

            roles = [
                find_role(interaction.guild, PARTNER_MANAGER_ROLE_NAME),
                find_role(interaction.guild, STAFF_TEAM_ROLE_NAME),
            ]
            missing = [name for role, name in zip(roles, (PARTNER_MANAGER_ROLE_NAME, STAFF_TEAM_ROLE_NAME)) if role is None]
            if missing:
                await interaction.followup.send(
                    f"I could not find these role(s): {', '.join(missing)}.",
                    ephemeral=True,
                )
                return
            if interaction.guild.me and any(role >= interaction.guild.me.top_role for role in roles if role):
                await interaction.followup.send(
                    "Move the Density Bot role above Partner Manager and Staff Team, then try again.",
                    ephemeral=True,
                )
                return

            await applicant.add_roles(
                *(role for role in roles if role),
                reason=f"Partner Manager application accepted by {interaction.user}",
            )
            record["status"] = "accepted"
            record["reviewed_at"] = datetime.now(UTC).isoformat()
            record["reviewer_id"] = interaction.user.id
            guild_record(self.data, interaction.guild.id).setdefault("denied_until", {}).pop(
                str(applicant.id), None
            )
            channels = await self.ensure_channels(interaction.guild)
            await channels["accepted"].send(
                content=(
                    f"{applicant.mention}'s submission has been accepted successfully by "
                    f"{interaction.user.mention}"
                ),
                embed=application_embed(record, "accepted", interaction.user),
                file=application_file(record),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            save_data(self.data)
            try:
                await interaction.message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            dm_sent = True
            try:
                await applicant.send(
                    embed=discord.Embed(
                        title="Application accepted!",
                        description=(
                            "Your Partner Manager application was accepted. You have received the "
                            f"**{PARTNER_MANAGER_ROLE_NAME}** and **{STAFF_TEAM_ROLE_NAME}** roles."
                        ),
                        color=discord.Color.green(),
                    )
                )
            except discord.Forbidden:
                dm_sent = False
            suffix = "" if dm_sent else " I could not DM the applicant."
            await interaction.followup.send(f"Application accepted and roles added.{suffix}", ephemeral=True)

    async def deny_application(
        self,
        interaction: discord.Interaction,
        message_id: int,
        reason: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This application is no longer available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.decision_lock:
            record = self.record_for_message(interaction.guild.id, message_id)
            if record is None:
                await interaction.followup.send("This application has already been reviewed.", ephemeral=True)
                return
            applicant_id = int(record["applicant_id"])
            denied_until = datetime.now(UTC) + timedelta(days=REAPPLY_DAYS)
            record["status"] = "denied"
            record["reviewed_at"] = datetime.now(UTC).isoformat()
            record["reviewer_id"] = interaction.user.id
            record["denial_reason"] = reason.strip()[:1000]
            guild_data = guild_record(self.data, interaction.guild.id)
            guild_data.setdefault("denied_until", {})[str(applicant_id)] = denied_until.isoformat()
            channels = await self.ensure_channels(interaction.guild)
            await channels["denied"].send(
                content=(
                    f"<@{applicant_id}>'s submission has been denied by {interaction.user.mention}"
                ),
                embed=application_embed(record, "denied", interaction.user),
                file=application_file(record),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            save_data(self.data)
            try:
                await interaction.message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

            dm_sent = True
            try:
                applicant = interaction.guild.get_member(applicant_id) or await self.bot.fetch_user(applicant_id)
                await applicant.send(
                    embed=discord.Embed(
                        title="Application denied",
                        description=(
                            f"Your Partner Manager application was denied.\n\n**Reason:** {record['denial_reason']}\n\n"
                            f"You can apply again <t:{int(denied_until.timestamp())}:R>."
                        ),
                        color=discord.Color.red(),
                    )
                )
            except (discord.Forbidden, discord.NotFound):
                dm_sent = False
            suffix = "" if dm_sent else " I could not DM the applicant."
            await interaction.followup.send(
                f"Application denied. The two-week reapply lock is active.{suffix}",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Applications(bot))
