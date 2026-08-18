import os

import discord
from discord import app_commands


DEFAULT_STAFF_ROLES = "Owner,Manager,Administrator,Admin,Moderator,Mod,Staff"


class StaffOnly(app_commands.CheckFailure):
    """Raised when a member tries to use a staff-only command."""


def staff_role_names() -> set[str]:
    configured = os.getenv("STAFF_ROLE_NAMES", DEFAULT_STAFF_ROLES)
    return {name.strip().casefold() for name in configured.split(",") if name.strip()}


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

    allowed_roles = staff_role_names()
    return any(role.name.casefold() in allowed_roles for role in interaction.user.roles)


def staff_only(permission: str | None = None):
    """Allow trusted staff roles or members with the matching Discord permission."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if member_is_staff(interaction, permission):
            return True
        raise StaffOnly()

    return app_commands.check(predicate)
