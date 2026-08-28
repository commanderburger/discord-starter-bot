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
HELPER_ROLE_NAME = os.getenv("HELPER_ROLE", "Helper")
STAFF_TEAM_ROLE_NAME = os.getenv("STAFF_TEAM_ROLE", "Staff Team")
HIGH_STAFF_CHANNEL_NAME = os.getenv("HIGH_STAFF_CHANNEL", "high-staff")
PANEL_MARKER = "Density Staff Applications v2"
CONTROL_PANEL_MARKER = "Density Staff Application Controls v1"
try:
    REAPPLY_DAYS = max(1, int(os.getenv("STAFF_APPLICATION_REAPPLY_DAYS", "14")))
except ValueError:
    REAPPLY_DAYS = 14


PARTNER_MANAGER_QUESTIONS = (
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

HELPER_QUESTIONS = (
    "Tell us about yourself and why you're applying for staff.",
    "What experience do you have with moderating Minecraft or Discord servers?",
    "How would you handle a player who repeatedly breaks the rules after being warned?",
    "What would you do if another staff member abused their permissions?",
    "How would you deal with a friend who broke the server rules?",
    "What qualities make a good staff member, and how do you show them?",
    "How would you handle an angry player who disagrees with your punishment?",
    "Why should we choose you instead of other applicants?",
)

APPLICATION_TYPES = {
    "partner_manager": {
        "name": "Partner Manager",
        "role": PARTNER_MANAGER_ROLE_NAME,
        "questions": PARTNER_MANAGER_QUESTIONS,
        "emoji": "🤝",
    },
    "helper": {
        "name": "Helper",
        "role": HELPER_ROLE_NAME,
        "questions": HELPER_QUESTIONS,
        "emoji": "🛟",
    },
}

AI_STYLE_PHRASES = (
    "furthermore",
    "moreover",
    "in conclusion",
    "it is important to",
    "i would remain calm and professional",
    "i would approach the situation",
    "effective communication",
    "foster a positive environment",
    "ensure a fair and respectful",
    "maintain a safe and welcoming",
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
    record = data.setdefault("guilds", {}).setdefault(
        str(guild_id),
        {
            "applications": {},
            "denied_until": {},
            "panel_message_id": None,
            "control_message_id": None,
            "paused": {"partner_manager": False, "helper": False},
        },
    )
    record.setdefault("applications", {})
    record.setdefault("denied_until", {})
    record.setdefault("panel_message_id", None)
    record.setdefault("control_message_id", None)
    paused = record.setdefault("paused", {})
    paused.setdefault("partner_manager", False)
    paused.setdefault("helper", False)
    return record


def application_type(record: dict) -> str:
    value = str(record.get("application_type", "partner_manager"))
    return value if value in APPLICATION_TYPES else "partner_manager"


def application_config(kind: str) -> dict:
    return APPLICATION_TYPES.get(kind, APPLICATION_TYPES["partner_manager"])


def application_questions(record: dict) -> tuple[str, ...]:
    return application_config(application_type(record))["questions"]


def cooldown_key(kind: str, user_id: int) -> str:
    return f"{kind}:{user_id}"


def ai_style_check(answers: list[str]) -> dict:
    """Return a cautious style flag, never a claim that AI use is proven."""
    joined = "\n".join(answers).casefold()
    matched = sorted({phrase for phrase in AI_STYLE_PHRASES if phrase in joined})
    nonempty = [answer.strip() for answer in answers if answer.strip()]
    average_length = sum(map(len, nonempty)) / max(1, len(nonempty))
    long_answers = sum(len(answer) >= 450 for answer in nonempty)
    score = len(matched) * 12
    if average_length >= 550:
        score += 20
    if long_answers >= max(4, len(nonempty) // 2):
        score += 15
    reasons: list[str] = []
    if matched:
        reasons.append("Repeated polished/template phrases: " + ", ".join(matched[:5]))
    if average_length >= 550:
        reasons.append("Unusually long average answer length")
    if long_answers >= max(4, len(nonempty) // 2):
        reasons.append("Many answers use similarly long, formal responses")
    return {
        "flagged": score >= 45,
        "score": min(score, 100),
        "reasons": reasons,
        "notice": "This is a style flag only and is not proof of AI use. Staff must review it manually.",
    }


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
    config = application_config(application_type(record))
    lines = [
        f"Density SMP {config['name']} Application",
        f"Applicant: {record.get('applicant_name', 'Unknown')}",
        f"Discord user ID: {record.get('applicant_id', 'Unknown')}",
        f"Submitted: {record.get('submitted_at', 'Unknown')}",
        "",
    ]
    for index, question in enumerate(application_questions(record)):
        answer = answers[index] if index < len(answers) else "No answer"
        lines.extend((f"Question {index + 1}: {question}", f"Answer: {answer}", ""))
    ai_check = record.get("ai_check", {})
    lines.extend(
        (
            "AI style check (manual review required)",
            f"Flagged: {'Yes' if ai_check.get('flagged') else 'No'}",
            f"Style score: {ai_check.get('score', 0)}/100",
            "Reasons: " + ("; ".join(ai_check.get("reasons", [])) or "No strong style indicators found"),
            str(ai_check.get("notice", "A style flag is not proof of AI use.")),
        )
    )
    return "\n".join(lines)


def application_file(record: dict) -> discord.File:
    content = application_text(record).encode("utf-8")
    applicant_id = record.get("applicant_id", "unknown")
    kind = application_type(record).replace("_", "-")
    return discord.File(io.BytesIO(content), filename=f"{kind}-{applicant_id}.txt")


def application_embed(record: dict, status: str, reviewer: discord.abc.User | None = None) -> discord.Embed:
    colours = {
        "pending": discord.Color.blurple(),
        "accepted": discord.Color.green(),
        "denied": discord.Color.red(),
    }
    config = application_config(application_type(record))
    titles = {state: f"{config['name']} Application • {state.title()}" for state in colours}
    applicant_id = int(record["applicant_id"])
    embed = discord.Embed(
        title=titles[status],
        description=f"Applicant: <@{applicant_id}> (`{applicant_id}`)",
        color=colours[status],
        timestamp=datetime.fromisoformat(record["submitted_at"]),
    )
    answers = record.get("answers", [])
    for index, question in enumerate(application_questions(record)):
        answer = answers[index] if index < len(answers) else "No answer"
        shortened = answer if len(answer) <= 300 else f"{answer[:297]}..."
        embed.add_field(name=f"{index + 1}. {question}", value=shortened or "No answer", inline=False)
    ai_check = record.get("ai_check", {})
    ai_flagged = bool(ai_check.get("flagged"))
    ai_reasons = "; ".join(ai_check.get("reasons", [])) or "No strong style indicators found"
    embed.add_field(
        name="AI-use style check",
        value=(
            f"{'⚠️ FLAGGED FOR MANUAL REVIEW' if ai_flagged else '✅ No strong indicators found'}\n"
            f"Style score: {int(ai_check.get('score', 0))}/100\n"
            f"{ai_reasons[:350]}\n"
            "This check is not proof and must never be the sole reason for a decision."
        ),
        inline=False,
    )
    if reviewer is not None:
        embed.add_field(name="Reviewed by", value=f"{reviewer.mention} (`{reviewer.id}`)", inline=False)
    if status == "denied" and record.get("denial_reason"):
        embed.add_field(name="Reason", value=str(record["denial_reason"])[:600], inline=False)
    embed.set_footer(text=f"Application ID: {record['id']} • Full answers attached")
    return embed


class ApplicationPanelView(discord.ui.View):
    def __init__(self, cog: "Applications", guild_id: int | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if guild_id is not None:
            self.apply.disabled = cog.is_application_paused(guild_id, "partner_manager")
            self.apply_helper.disabled = cog.is_application_paused(guild_id, "helper")

    @discord.ui.button(
        label="Apply for Partner Manager",
        emoji="🤝",
        style=discord.ButtonStyle.primary,
        custom_id="density-partner-manager-apply-v1",
    )
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.start_application(interaction, "partner_manager")

    @discord.ui.button(
        label="Apply for Helper",
        emoji="🛟",
        style=discord.ButtonStyle.success,
        custom_id="density-helper-apply-v1",
    )
    async def apply_helper(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.start_application(interaction, "helper")


class ApplicationControlView(discord.ui.View):
    def __init__(self, cog: "Applications", guild_id: int | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if guild_id is not None:
            partner_paused = cog.is_application_paused(guild_id, "partner_manager")
            helper_paused = cog.is_application_paused(guild_id, "helper")
            self.toggle_partner.label = f"Partner Manager: {'Paused' if partner_paused else 'Open'}"
            self.toggle_partner.style = (
                discord.ButtonStyle.danger if partner_paused else discord.ButtonStyle.success
            )
            self.toggle_helper.label = f"Helper: {'Paused' if helper_paused else 'Open'}"
            self.toggle_helper.style = (
                discord.ButtonStyle.danger if helper_paused else discord.ButtonStyle.success
            )

    @discord.ui.button(
        label="Partner Manager: Open",
        emoji="🤝",
        style=discord.ButtonStyle.success,
        custom_id="density-toggle-partner-manager-applications-v1",
    )
    async def toggle_partner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.toggle_application(interaction, "partner_manager")

    @discord.ui.button(
        label="Helper: Open",
        emoji="🛠️",
        style=discord.ButtonStyle.success,
        custom_id="density-toggle-helper-applications-v1",
    )
    async def toggle_helper(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.toggle_application(interaction, "helper")


class DenialReasonModal(discord.ui.Modal, title="Deny Staff Application"):
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
        self.in_progress: set[tuple[int, int, str]] = set()
        self.ready_lock = asyncio.Lock()
        self.decision_lock = asyncio.Lock()
        self.ready_complete = False
        self.bot.add_view(ApplicationPanelView(self))
        self.bot.add_view(ApplicationControlView(self))
        self.bot.add_view(ApplicationReviewView(self))

    def is_application_paused(self, guild_id: int, kind: str) -> bool:
        return bool(guild_record(self.data, guild_id).setdefault("paused", {}).get(kind, False))

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

    def panel_embed(self, guild_id: int) -> discord.Embed:
        partner_status = "🔴 Paused" if self.is_application_paused(guild_id, "partner_manager") else "🟢 Open"
        helper_status = "🔴 Paused" if self.is_application_paused(guild_id, "helper") else "🟢 Open"
        embed = discord.Embed(
            title="📝 Density SMP Staff Applications",
            description=(
                "Choose the role you want to apply for below.\n\n"
                f"**Partner Manager:** {partner_status}\n"
                f"**Helper:** {helper_status}\n\n"
                "• Applications are completed privately in DMs, one question at a time.\n"
                "• **AI-generated or AI-rewritten answers are prohibited.**\n"
                "• Submissions are checked for possible AI-style writing and may be flagged for manual review.\n"
                "• A style flag is not proof; staff will always review the answers themselves.\n"
                f"• If denied, you must wait **{REAPPLY_DAYS} days** before applying for that same role again.\n"
                "• A denial for one role does not stop you applying for the other role.\n\n"
                "Make sure your DMs are open before starting."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=PANEL_MARKER)
        return embed

    async def ensure_panel(self, guild: discord.Guild, panel: discord.TextChannel) -> None:
        guild_data = guild_record(self.data, guild.id)
        stored_id = guild_data.get("panel_message_id")
        if stored_id:
            try:
                message = await panel.fetch_message(int(stored_id))
                await message.edit(
                    embed=self.panel_embed(guild.id),
                    view=ApplicationPanelView(self, guild.id),
                )
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                guild_data["panel_message_id"] = None

        async for message in panel.history(limit=50):
            if message.author.id == self.bot.user.id and any(
                embed.footer and embed.footer.text in {PANEL_MARKER, "Density Partner Manager Applications v1"}
                for embed in message.embeds
            ):
                guild_data["panel_message_id"] = message.id
                await message.edit(
                    embed=self.panel_embed(guild.id),
                    view=ApplicationPanelView(self, guild.id),
                )
                save_data(self.data)
                return

        message = await panel.send(
            embed=self.panel_embed(guild.id),
            view=ApplicationPanelView(self, guild.id),
        )
        guild_data["panel_message_id"] = message.id
        save_data(self.data)

    def high_staff_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        candidates = {
            normalise_role_name(HIGH_STAFF_CHANNEL_NAME),
            normalise_role_name("high-staff"),
            normalise_role_name("high-staff-chat"),
        }
        exact = discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel)
            and normalise_role_name(channel.name) in candidates,
            guild.channels,
        )
        if isinstance(exact, discord.TextChannel):
            return exact
        return discord.utils.find(
            lambda channel: isinstance(channel, discord.TextChannel)
            and "highstaff" in normalise_role_name(channel.name),
            guild.channels,
        )

    def control_embed(self, guild_id: int) -> discord.Embed:
        partner_paused = self.is_application_paused(guild_id, "partner_manager")
        helper_paused = self.is_application_paused(guild_id, "helper")
        embed = discord.Embed(
            title="⚙️ Staff Application Controls",
            description=(
                "Owner, Co-Owner, and Manager can pause or reopen each application separately. "
                "When paused, its public application button is disabled and new applications cannot start.\n\n"
                f"**Partner Manager:** {'🔴 Paused' if partner_paused else '🟢 Open'}\n"
                f"**Helper:** {'🔴 Paused' if helper_paused else '🟢 Open'}"
            ),
            color=discord.Color.orange() if partner_paused or helper_paused else discord.Color.green(),
        )
        embed.set_footer(text=CONTROL_PANEL_MARKER)
        return embed

    async def ensure_control_panel(self, guild: discord.Guild) -> None:
        channel = self.high_staff_channel(guild)
        if channel is None:
            log.warning("No high-staff channel found in %s; application controls were not posted", guild.name)
            return
        guild_data = guild_record(self.data, guild.id)
        stored_id = guild_data.get("control_message_id")
        if stored_id:
            try:
                message = await channel.fetch_message(int(stored_id))
                await message.edit(
                    embed=self.control_embed(guild.id),
                    view=ApplicationControlView(self, guild.id),
                )
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                guild_data["control_message_id"] = None

        async for message in channel.history(limit=50):
            if message.author.id == self.bot.user.id and any(
                embed.footer and embed.footer.text == CONTROL_PANEL_MARKER
                for embed in message.embeds
            ):
                guild_data["control_message_id"] = message.id
                await message.edit(
                    embed=self.control_embed(guild.id),
                    view=ApplicationControlView(self, guild.id),
                )
                save_data(self.data)
                return

        message = await channel.send(
            embed=self.control_embed(guild.id),
            view=ApplicationControlView(self, guild.id),
        )
        guild_data["control_message_id"] = message.id
        save_data(self.data)

    async def toggle_application(self, interaction: discord.Interaction, kind: str) -> None:
        if interaction.guild is None or not member_is_senior(interaction):
            await interaction.response.send_message(
                "Only Owner, Co-Owner, or Manager can change application availability.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        config = application_config(kind)
        guild_data = guild_record(self.data, interaction.guild.id)
        paused = guild_data.setdefault("paused", {})
        paused[kind] = not bool(paused.get(kind, False))
        save_data(self.data)
        channels = await self.ensure_channels(interaction.guild)
        await self.ensure_panel(interaction.guild, channels["panel"])
        if interaction.message is not None:
            await interaction.message.edit(
                embed=self.control_embed(interaction.guild.id),
                view=ApplicationControlView(self, interaction.guild.id),
            )
        state = "paused" if paused[kind] else "reopened"
        await interaction.followup.send(f"{config['name']} applications are now **{state}**.", ephemeral=True)

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
                    await self.ensure_control_panel(guild)
                except discord.Forbidden:
                    log.warning("Missing permission to set up staff applications in %s", guild.name)
                except discord.HTTPException:
                    log.exception("Could not set up staff applications in %s", guild.name)

    async def start_application(self, interaction: discord.Interaction, kind: str) -> None:
        # Acknowledge the button immediately. Creating a DM can take long enough for
        # Discord's three-second interaction window to expire, which previously sent
        # the introduction but prevented the question task from ever starting.
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Use this button in the Density SMP server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id
        config = application_config(kind)
        key = (guild_id, user_id, kind)
        guild_data = guild_record(self.data, guild_id)
        now = datetime.now(UTC)

        if self.is_application_paused(guild_id, kind):
            await interaction.followup.send(
                f"{config['name']} applications are temporarily paused by high staff. Please check again later.",
                ephemeral=True,
            )
            return

        denied_records = guild_data.get("denied_until", {})
        denied_until_raw = denied_records.get(cooldown_key(kind, user_id))
        if kind == "partner_manager" and denied_until_raw is None:
            denied_until_raw = denied_records.get(str(user_id))
        if denied_until_raw:
            try:
                denied_until = datetime.fromisoformat(denied_until_raw)
            except ValueError:
                denied_until = now
            if denied_until > now:
                await interaction.followup.send(
                    f"You can apply again <t:{int(denied_until.timestamp())}:R>.",
                    ephemeral=True,
                )
                return

        existing = next(
            (
                record
                for record in guild_data.get("applications", {}).values()
                if int(record.get("applicant_id", 0)) == user_id
                and application_type(record) == kind
                and record.get("status") in {"pending", "accepted"}
            ),
            None,
        )
        if existing:
            message = (
                f"Your {config['name']} application is already waiting for staff review."
                if existing["status"] == "pending"
                else f"Your {config['name']} application has already been accepted."
            )
            await interaction.followup.send(message, ephemeral=True)
            return
        if any(active_guild == guild_id and active_user == user_id for active_guild, active_user, _ in self.in_progress):
            await interaction.followup.send(
                "You already have a staff application in progress. Finish or cancel it in your DMs first.",
                ephemeral=True,
            )
            return

        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                embed=discord.Embed(
                    title=f"{config['name']} Application",
                    description=(
                        "**AI-generated or AI-rewritten answers are prohibited.** Your answers will be "
                        "checked for possible AI-style writing and may be flagged for manual staff review. "
                        "A flag is not proof of AI use.\n\n"
                        "I will send one question at a time. Reply in this DM to continue. "
                        "Type `cancel` at any time to stop. You have 15 minutes for each answer."
                    ),
                    color=discord.Color.blurple(),
                )
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I cannot DM you. Enable direct messages from server members, then press the button again.",
                ephemeral=True,
            )
            return

        self.in_progress.add(key)
        await interaction.followup.send(
            f"{config['name']} application started — check your DMs from Density Bot.",
            ephemeral=True,
        )
        asyncio.create_task(
            self.run_questions(interaction.guild, interaction.user, dm, kind),
            name=f"{kind}-application-{guild_id}-{user_id}",
        )

    async def run_questions(
        self,
        guild: discord.Guild,
        applicant: discord.Member,
        dm: discord.DMChannel,
        kind: str,
    ) -> None:
        config = application_config(kind)
        questions = config["questions"]
        key = (guild.id, applicant.id, kind)
        answers: list[str] = []
        try:
            for index, question in enumerate(questions, start=1):
                embed = discord.Embed(
                    title=f"{config['name']} • Question {index} of {len(questions)}",
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
                "application_type": kind,
                "applicant_id": applicant.id,
                "applicant_name": str(applicant),
                "answers": answers,
                "ai_check": ai_style_check(answers),
                "status": "pending",
                "submitted_at": datetime.now(UTC).isoformat(),
                "pending_message_id": None,
            }
            channels = await self.ensure_channels(guild)
            ai_prefix = "⚠️ **POSSIBLE AI USE — MANUAL REVIEW REQUIRED**\n" if record["ai_check"]["flagged"] else ""
            pending_message = await channels["pending"].send(
                content=f"{ai_prefix}New {config['name']} application from {applicant.mention}",
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
                    description=f"Your {config['name']} application was submitted successfully. Staff will review it.",
                    color=discord.Color.green(),
                )
            )
        except discord.Forbidden:
            log.info("Applicant %s closed DMs during an application", applicant.id)
        except discord.HTTPException:
            log.exception("%s application failed for %s", config["name"], applicant.id)
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

            kind = application_type(record)
            config = application_config(kind)
            roles = [
                find_role(interaction.guild, config["role"]),
                find_role(interaction.guild, STAFF_TEAM_ROLE_NAME),
            ]
            missing = [name for role, name in zip(roles, (config["role"], STAFF_TEAM_ROLE_NAME)) if role is None]
            if missing:
                await interaction.followup.send(
                    f"I could not find these role(s): {', '.join(missing)}.",
                    ephemeral=True,
                )
                return
            if interaction.guild.me and any(role >= interaction.guild.me.top_role for role in roles if role):
                await interaction.followup.send(
                    f"Move the Density Bot role above {config['role']} and Staff Team, then try again.",
                    ephemeral=True,
                )
                return

            await applicant.add_roles(
                *(role for role in roles if role),
                reason=f"{config['name']} application accepted by {interaction.user}",
            )
            record["status"] = "accepted"
            record["reviewed_at"] = datetime.now(UTC).isoformat()
            record["reviewer_id"] = interaction.user.id
            denied_records = guild_record(self.data, interaction.guild.id).setdefault("denied_until", {})
            denied_records.pop(cooldown_key(kind, applicant.id), None)
            if kind == "partner_manager":
                denied_records.pop(str(applicant.id), None)
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
                            f"Your {config['name']} application was accepted. You have received the "
                            f"**{config['role']}** and **{STAFF_TEAM_ROLE_NAME}** roles."
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
            kind = application_type(record)
            config = application_config(kind)
            denied_until = datetime.now(UTC) + timedelta(days=REAPPLY_DAYS)
            record["status"] = "denied"
            record["reviewed_at"] = datetime.now(UTC).isoformat()
            record["reviewer_id"] = interaction.user.id
            record["denial_reason"] = reason.strip()[:1000]
            guild_data = guild_record(self.data, interaction.guild.id)
            guild_data.setdefault("denied_until", {})[cooldown_key(kind, applicant_id)] = denied_until.isoformat()
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
                            f"Your {config['name']} application was denied.\n\n**Reason:** {record['denial_reason']}\n\n"
                            f"You can apply for {config['name']} again <t:{int(denied_until.timestamp())}:R>. "
                            "This does not stop you applying for the other staff role."
                        ),
                        color=discord.Color.red(),
                    )
                )
            except (discord.Forbidden, discord.NotFound):
                dm_sent = False
            suffix = "" if dm_sent else " I could not DM the applicant."
            await interaction.followup.send(
                f"Application denied. The two-week reapply lock is active for {config['name']} only.{suffix}",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Applications(bot))
