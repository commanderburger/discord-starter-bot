import logging
import os

import discord
from discord import app_commands
from discord.ext import commands


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("starter-bot")


class StarterCommandTree(app_commands.CommandTree):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        log.exception("Application command failed", exc_info=error)
        # Command/cog-specific handlers have already replied at this point.
        # Avoid sending a second, confusing error message after their reply.
        if interaction.response.is_done():
            return
        message = "That command could not be completed. Please try again or ask a staff member for help."
        await interaction.response.send_message(message, ephemeral=True)


class StarterBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Required for the Discord-invite moderation filter.
        # This must also be enabled on the Bot page in Discord's Developer Portal.
        intents.message_content = True
        # Required for the welcome message when a member joins.
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            tree_cls=StarterCommandTree,
            case_insensitive=True,
            strip_after_prefix=True,
        )

    async def setup_hook(self) -> None:
        for extension in (
            "cogs.general",
            "cogs.moderation",
            "cogs.minecraft",
            "cogs.economy",
            "cogs.fun",
            "cogs.api_admin",
            "cogs.giveaways",
            "cogs.tickets",
            "cogs.welcome",
            "cogs.levels",
            "cogs.updates",
            "cogs.social_links",
            "cogs.staff_tools",
            "cogs.staff_guide",
            "cogs.applications",
            "cogs.ai_chat",
        ):
            await self.load_extension(extension)

        # A test guild makes command changes appear almost immediately.
        # Remove TEST_GUILD_ID later to use globally synced commands instead.
        test_guild_id = os.getenv("TEST_GUILD_ID")
        if test_guild_id:
            guild = discord.Object(id=int(test_guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to test guild %s", len(synced), test_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global command(s)", len(synced))

    async def on_ready(self) -> None:
        if self.user:
            log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
            log.info(
                "Connected to: %s",
                ", ".join(f"{guild.name} ({guild.id})" for guild in self.guilds) or "no servers",
            )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    StarterBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()

