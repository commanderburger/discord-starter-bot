import json
import math
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


BUNDLED_ECONOMY_FILE = Path(__file__).resolve().parent.parent / "config" / "economy.json"
SYNCED_ECONOMY_FILE = Path(os.getenv("BOT_DATA_DIR", "/data")) / "economy.json"


def load_economy() -> dict:
    for economy_file in (SYNCED_ECONOMY_FILE, BUNDLED_ECONOMY_FILE):
        try:
            with economy_file.open(encoding="utf-8") as file:
                economy = json.load(file)
            if isinstance(economy, dict) and isinstance(economy.get("spawners"), dict):
                return economy
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return {"currency_symbol": "$", "spawners": {}}


def number(value: object, fallback: float = 0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else fallback
    return fallback


def money_text(symbol: str, value: float) -> str:
    if float(value).is_integer():
        return f"{symbol}{value:,.0f}"
    return f"{symbol}{value:,.2f}"


def fit_lines(lines: list[str], limit: int = 1024) -> str:
    result: list[str] = []
    used = 0
    for line in lines:
        extra = len(line) + (1 if result else 0)
        if used + extra > limit - 20:
            result.append("…and more")
            break
        result.append(line)
        used += extra
    return "\n".join(result) or "None"


def fill_orders(quantity: float, order_book: object) -> tuple[float, float]:
    remaining_drops = max(0.0, quantity)
    sold = 0.0
    earned = 0.0
    if not isinstance(order_book, list):
        return sold, earned
    levels = sorted(
        (level for level in order_book if isinstance(level, dict)),
        key=lambda level: number(level.get("price")),
        reverse=True,
    )
    for level in levels:
        price = max(0.0, number(level.get("price")))
        wanted = max(0.0, number(level.get("remaining")))
        amount = min(remaining_drops, wanted)
        sold += amount
        earned += amount * price
        remaining_drops -= amount
        if remaining_drops <= 0:
            break
    return sold, earned


class Economy(commands.Cog):
    spawner = app_commands.Group(name="spawner", description="Spawner information")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def spawner_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        spawners = load_economy()["spawners"]
        matches = [
            app_commands.Choice(name=str(data.get("display_name", name))[:100], value=name)
            for name, data in spawners.items()
            if isinstance(data, dict)
            and current.casefold() in str(data.get("display_name", name)).casefold()
        ]
        return matches[:25]

    @app_commands.command(name="ordering", description="Show live Minecraft item order prices")
    async def ordering(self, interaction: discord.Interaction) -> None:
        economy = load_economy()
        symbol = str(economy.get("currency_symbol", "$"))
        materials: dict[str, dict] = {}
        for spawner_data in economy["spawners"].values():
            if not isinstance(spawner_data, dict):
                continue
            drops = spawner_data.get("drops")
            if not isinstance(drops, list):
                continue
            for drop in drops:
                if not isinstance(drop, dict):
                    continue
                material = str(drop.get("material", "")).strip()
                if material and material not in materials:
                    materials[material] = drop

        lines: list[str] = []
        for material, drop in sorted(
            materials.items(), key=lambda item: str(item[1].get("display_name", item[0]))
        ):
            display_name = str(drop.get("display_name", material.replace("_", " ").title()))
            best_price = max(0.0, number(drop.get("best_order_price")))
            ordered = max(0.0, number(drop.get("ordered_amount")))
            if best_price > 0 and ordered > 0:
                lines.append(
                    f"**{display_name}** — {money_text(symbol, best_price)} each · {ordered:,.0f} wanted"
                )

        if not lines:
            for data in economy["spawners"].values():
                if not isinstance(data, dict):
                    continue
                price = max(0.0, number(data.get("order_price")))
                if price > 0:
                    lines.append(
                        f"**{data.get('drop_name', 'Drops')}** — {money_text(symbol, price)} each"
                    )

        embed = discord.Embed(
            title="Active Item Orders",
            description=fit_lines(lines, 4000) if lines else "There are no active orders for spawner drops.",
            color=discord.Color.gold(),
        )
        if economy.get("live_sync", False):
            embed.set_footer(text="Live data from the Minecraft server's Density orders.")
        elif economy.get("example_values", False):
            embed.set_footer(text="Example values — use /api sync after installing DensityBridge.")
        await interaction.response.send_message(embed=embed)

    @spawner.command(name="count", description="Show all server spawner counts by type")
    async def spawner_count(self, interaction: discord.Interaction) -> None:
        economy = load_economy()
        lines: list[str] = []
        total_spawners = 0
        total_blocks = 0
        counts_available = False
        for data in economy["spawners"].values():
            if not isinstance(data, dict):
                continue
            count_value = data.get("server_count")
            if not isinstance(count_value, (int, float)) or isinstance(count_value, bool):
                continue
            counts_available = True
            count = max(0, int(count_value))
            blocks = max(0, int(number(data.get("physical_blocks"), count)))
            total_spawners += count
            total_blocks += blocks
            if count == 0:
                continue
            display_name = str(data.get("display_name", "Spawner"))
            block_text = f" in {blocks:,} placed block{'s' if blocks != 1 else ''}" if blocks != count else ""
            lines.append(f"• **{display_name}** — {count:,}{block_text}")

        embed = discord.Embed(
            title="Server Spawner Counts",
            description=fit_lines(lines, 4000) if lines else "No spawners were found.",
            color=discord.Color.blurple(),
        )
        if counts_available:
            embed.add_field(name="Total spawners", value=f"{total_spawners:,}")
            embed.add_field(name="Placed blocks", value=f"{total_blocks:,}")
        else:
            embed.set_footer(text="Install DensityBridge and use /api sync to load server counts.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="calc", description="Estimate spawner drops and current order money per hour")
    @app_commands.describe(spawner="Spawner type", amount="Number of spawners in the stack")
    @app_commands.autocomplete(spawner=spawner_autocomplete)
    async def calc(
        self,
        interaction: discord.Interaction,
        spawner: str,
        amount: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        economy = load_economy()
        data = economy["spawners"].get(spawner)
        if not isinstance(data, dict):
            await interaction.response.send_message("That spawner is not configured.", ephemeral=True)
            return

        exponent = min(1.0, max(0.01, number(data.get("stacking_efficiency_exponent"), 1.0)))
        effective_spawners = max(1, math.floor((amount**exponent) + 0.5))
        drops_data = data.get("drops")
        detailed_drops = drops_data if isinstance(drops_data, list) else []

        drop_lines: list[str] = []
        total_drops = 0.0
        total_sold = 0.0
        total_money = 0.0
        for drop in detailed_drops:
            if not isinstance(drop, dict):
                continue
            hourly = max(0.0, number(drop.get("drops_per_hour_per_spawner"))) * effective_spawners
            sold, earned = fill_orders(hourly, drop.get("order_book"))
            total_drops += hourly
            total_sold += sold
            total_money += earned
            name = str(drop.get("display_name", drop.get("material", "Drops")))
            drop_lines.append(f"**{name}:** {hourly:,.0f}/hr · orders buy {sold:,.0f}")

        using_live_orders = bool(detailed_drops)
        if not detailed_drops:
            total_drops = max(0.0, number(data.get("drops_per_hour_per_spawner"))) * effective_spawners
            price = max(0.0, number(data.get("order_price")))
            total_sold = total_drops if price > 0 else 0
            total_money = total_sold * price
            drop_lines.append(f"**{data.get('drop_name', 'Drops')}:** {total_drops:,.0f}/hr")

        symbol = str(economy.get("currency_symbol", "$"))
        embed = discord.Embed(
            title=f"{data.get('display_name', 'Spawner')} Estimate",
            color=discord.Color.green(),
        )
        embed.add_field(name="Spawner stack", value=f"{amount:,}")
        if effective_spawners != amount:
            embed.add_field(
                name="Effective output",
                value=f"{effective_spawners:,} spawners ({exponent:g} stacking exponent)",
            )
        embed.add_field(name="Expected drops/hour", value=f"{total_drops:,.0f}", inline=False)
        embed.add_field(name="Drop breakdown", value=fit_lines(drop_lines), inline=False)
        embed.add_field(name="Current order money/hour", value=money_text(symbol, total_money), inline=False)
        if using_live_orders:
            unsold = max(0.0, total_drops - total_sold)
            embed.add_field(
                name="Order capacity",
                value=f"{total_sold:,.0f} drops sellable now · {unsold:,.0f} without an active order",
                inline=False,
            )
        embed.set_footer(
            text="Estimate uses DonutSpawners rates and current Density orders; chance-based output varies."
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economy(bot))
