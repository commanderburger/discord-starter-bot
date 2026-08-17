import platform

import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check whether the bot is online")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! `{latency_ms} ms`")

    @app_commands.command(name="hello", description="Get a friendly greeting")
    async def hello(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"Hello, {interaction.user.mention}!")

    @app_commands.command(name="about", description="Show basic bot information")
    async def about(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Starter Bot",
            description="A small Discord bot ready for your own commands.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Python", value=platform.python_version())
        embed.add_field(name="discord.py", value=discord.__version__)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
