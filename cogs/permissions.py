import os
import re

import discord
from discord import app_commands


DEFAULT_STAFF_ROLES = (
    "Owner,Co Owner,Co-Owner,Manager,Administrator,Admin,"
    "Senior Moderator,Senior Mod,Moderator,Mod,Trial Moderator,Trial Mod,"
    "Support,Support Team,Helper,Helpers,Staff,Partner Manager,Partner Managers"
)
DEFAULT_SENIOR_ROLES = "Owner,Co Owner,Co-Owner,Manager"
STAFF_ROLE_KEYWORDS = (
    "staff",
    "moderator",
    "mod",
    "helper",
    "support",
    "admin",
    "manager",
)


class StaffOnly(app_commands.CheckFailure):
    """Raised when a member tries to use a staff-only command."""


class SeniorStaffOnly(app_commands.CheckFailure):
    """Raised when a member tries to use a senior-management command."""


def normalise_role_name(name: str) -> str:
    """Make role matching tolerant of spaces, hyphens and capitalisation."""

    return re.sub(r"[^a-z0-9]", "", name.casefold())


def configured_role_names(variable: str, default: str) -> set[str]:
    configured = os.getenv(variable, default)
    return {
        normalise_role_name(name)
        for name in configured.split(",")
        if normalise_role_name(name)
    }


def staff_role_names() -> set[str]:
    return configured_role_names("STAFF_ROLE_NAMES", DEFAULT_STAFF_ROLES)


def senior_role_names() -> set[str]:
    return configured_role_names("SENIOR_ROLE_NAMES", DEFAULT_SENIOR_ROLES)


def member_has_named_role(member: discord.Member, names: set[str]) -> bool:
    return any(normalise_role_name(role.name) in names for role in member.roles)


def role_is_staff(role: discord.Role) -> bool:
    """Recognise configured and decorated staff roles without broad member access."""

    normalised = normalise_role_name(role.name)
    if normalised in staff_role_names():
        return True

    words = re.findall(r"[a-z0-9]+", role.name.casefold())
    return any(
        word == keyword or word.startswith(keyword)
        for word in words
        for keyword in STAFF_ROLE_KEYWORDS
    )


def member_has_staff_role(member: discord.Member) -> bool:
    return any(role_is_staff(role) for role in member.roles)


def member_is_staff(interaction: discord.Interaction, permission: str | None = None) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True

    permissions = interaction.user.guild_permissions
    if permissions.administrator:
        return True
    if permission and getattr(permissions, permission, False):
        return True

    return member_has_staff_role(interaction.user)


def member_is_senior(interaction: discord.Interaction) -> bool:
    """Allow only the server owner or Owner, Co-Owner and Manager roles."""

    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    return member_has_named_role(interaction.user, senior_role_names())


def staff_only(permission: str | None = None):
    """Allow trusted staff roles or members with the matching Discord permission."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if member_is_staff(interaction, permission):
            return True
        raise StaffOnly()

    return app_commands.check(predicate)


def senior_only():
    """Restrict a command to Manager, Co-Owner, Owner or the server owner."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if member_is_senior(interaction):
            return True
        raise SeniorStaffOnly()

    return app_commands.check(predicate)
