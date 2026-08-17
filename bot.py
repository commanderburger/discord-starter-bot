import logging
import os

import discord
from discord.ext import commands


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("starter-bot")


class StarterBot(commands.Bot):
    def __init__(self) -> None:
        # Slash commands do not require the privileged Message Content intent.
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())

    async def setup_hook(self) -> None:
        await self.load_extension("cogs.general")

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


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    StarterBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
