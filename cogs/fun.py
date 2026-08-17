import random

import discord
from discord import app_commands
from discord.ext import commands


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="coinflip", description="Flip a coin")
    async def coinflip(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"🪙 **{random.choice(['Heads', 'Tails'])}!**")

    @app_commands.command(name="roll", description="Roll dice")
    @app_commands.describe(sides="Number of sides", dice="Number of dice")
    async def roll(
        self,
        interaction: discord.Interaction,
        sides: app_commands.Range[int, 2, 1000] = 6,
        dice: app_commands.Range[int, 1, 20] = 1,
    ) -> None:
        rolls = [random.randint(1, sides) for _ in range(dice)]
        await interaction.response.send_message(
            f"🎲 Rolls: **{', '.join(map(str, rolls))}** — Total: **{sum(rolls)}**"
        )

    @app_commands.command(name="avatar", description="Show a member's avatar")
    async def avatar(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        embed = discord.Embed(title=f"{target.display_name}'s Avatar", color=target.color)
        embed.set_image(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Show information about a server member")
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        embed = discord.Embed(title=str(target), color=target.color)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Account created", value=discord.utils.format_dt(target.created_at, "R"))
        if isinstance(target, discord.Member) and target.joined_at:
            embed.add_field(name="Joined server", value=discord.utils.format_dt(target.joined_at, "R"))
        embed.add_field(name="User ID", value=str(target.id), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="Show information about this Discord server")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        embed = discord.Embed(title=guild.name, color=discord.Color.blurple())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Members", value=f"{guild.member_count:,}")
        embed.add_field(name="Channels", value=str(len(guild.channels)))
        embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "R"))
        embed.add_field(name="Server ID", value=str(guild.id), inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
