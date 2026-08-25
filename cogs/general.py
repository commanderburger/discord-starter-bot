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

    @app_commands.command(name="help", description="Show all available bot commands")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Bot Commands",
            description="Here is what I can do.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Minecraft",
            value=(
                "`/stats` — Minecraft server status\n"
                "`/links` — Server address and useful links\n"
                "`/ordering` — Live item orders and prices\n"
                "`/calc` — Live spawner drops and order money\n"
                "`/spawner count` — Real Overworld and Nether spawner totals\n"
                "`/sellprice` — Live /sell value\n"
                "`/farm pickle` · `/farm bamboo` — Farm profit\n"
                "`/auction browse` · `/auction track` — Auction listings and graph"
            ),
            inline=False,
        )
        embed.add_field(
            name="Server",
            value="`/serverinfo` · `/userinfo` · `/avatar`",
            inline=False,
        )
        embed.add_field(
            name="Fun",
            value="`/coinflip` · `/roll`",
            inline=False,
        )
        embed.add_field(
            name="Giveaways (Staff)",
            value=(
                "`/gcreate` · `/autogcreate` — open setup forms\n"
                "`/gend` · `/greroll` · `/autoglist` · `/autogstop`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Tickets",
            value=(
                "Use the menu in the ticket channel for Support, Partnerships or a Bug Report.\n"
                "`/ticketsetup` — Manager, Co-Owner and Owner only"
            ),
            inline=False,
        )
        embed.add_field(
            name="Moderation (Staff)",
            value=(
                "`/mute` · `/tempmute` · `/unmute` · `/purge` · `/warn`\n"
                "`/warnings` · `/clearwarnings`\n"
                "`/ban` · `/kick` — Manager, Co-Owner and Owner only\n"
            ),
            inline=False,
        )
        embed.add_field(
            name="General",
            value="`/ping` · `/hello` · `/about` · `/help`",
            inline=False,
        )
        embed.add_field(
            name="Owner/Manager",
            value="`/api status` · `/api test` · `/api set-url` · `/api set-key` · `/api sync`",
            inline=False,
        )
        embed.set_footer(text="Discord invite links are removed outside approved advert channels.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
