import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands
from mcstatus import BedrockServer, JavaServer


class Minecraft(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="stats", description="Show the Minecraft server status")
    async def stats(self, interaction: discord.Interaction) -> None:
        address = os.getenv("MINECRAFT_SERVER", "").strip()
        edition = os.getenv("MINECRAFT_EDITION", "java").strip().casefold()
        if not address:
            await interaction.response.send_message(
                "The server owner still needs to set `MINECRAFT_SERVER` in TrueNAS.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            server_type = BedrockServer if edition == "bedrock" else JavaServer
            server = await asyncio.to_thread(server_type.lookup, address)
            status = await asyncio.wait_for(server.async_status(), timeout=8)
        except Exception:
            embed = discord.Embed(
                title="Minecraft Server",
                description=f"🔴 `{address}` appears to be offline.",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        players = status.players
        version = getattr(status.version, "name", "Unknown")
        latency = round(getattr(status, "latency", 0))
        embed = discord.Embed(
            title="Minecraft Server",
            description=f"🟢 `{address}` is online",
            color=discord.Color.green(),
        )
        embed.add_field(name="Players", value=f"{players.online}/{players.max}")
        embed.add_field(name="Version", value=version)
        embed.add_field(name="Latency", value=f"{latency} ms")
        sample = getattr(players, "sample", None)
        if sample:
            names = ", ".join(player.name for player in sample[:15])
            embed.add_field(name="Online Players", value=names, inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="links", description="Show the Minecraft server and community links")
    async def links(self, interaction: discord.Interaction) -> None:
        address = os.getenv("MINECRAFT_SERVER", "Not configured")
        links = {
            "Website": os.getenv("SERVER_WEBSITE_URL", ""),
            "Store": os.getenv("SERVER_STORE_URL", ""),
            "Vote": os.getenv("SERVER_VOTE_URL", ""),
            "Discord": os.getenv("SERVER_DISCORD_URL", ""),
        }
        embed = discord.Embed(title="Server Links", color=discord.Color.blurple())
        embed.add_field(name="Minecraft Address", value=f"`{address}`", inline=False)
        configured = False
        for label, url in links.items():
            if url:
                configured = True
                embed.add_field(name=label, value=f"[Open {label}]({url})", inline=True)
        if not configured:
            embed.set_footer(text="The owner can add website, store, vote and Discord URLs in TrueNAS.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Minecraft(bot))
