import json
import math
import os
import re
import time
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
                economy.setdefault("sell_prices", {})
                economy.setdefault("auctions", {"open": [], "history": []})
                return economy
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return {"currency_symbol": "$", "spawners": {}, "sell_prices": {}, "auctions": {"open": [], "history": []}}


def number(value: object, fallback: float = 0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else fallback
    return fallback


def money_text(symbol: str, value: float) -> str:
    return f"{symbol}{value:,.0f}" if float(value).is_integer() else f"{symbol}{value:,.2f}"


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


def normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def is_decoy_spawner(name: str, data: dict) -> bool:
    return "decoy" in normalise(name + " " + str(data.get("display_name", "")))


def sell_price(economy: dict, material: str) -> tuple[float, str]:
    value = economy.get("sell_prices", {}).get(material.upper(), {})
    if isinstance(value, dict):
        return max(0.0, number(value.get("price"))), str(value.get("display_name", material.replace("_", " ").title()))
    if isinstance(value, (int, float)):
        return max(0.0, number(value)), material.replace("_", " ").title()
    return 0.0, material.replace("_", " ").title()


def auction_time(value: object) -> int:
    raw = int(number(value))
    return raw // 1000 if raw > 20_000_000_000 else raw


class Economy(commands.Cog):
    spawner = app_commands.Group(name="spawner", description="Spawner information")
    farm = app_commands.Group(name="farm", description="Live farm profit calculators")
    auction = app_commands.Group(name="auction", description="Minecraft auction tracking")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def spawner_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        matches = []
        for name, data in load_economy()["spawners"].items():
            if not isinstance(data, dict) or is_decoy_spawner(name, data):
                continue
            display = str(data.get("display_name", name))
            if current.casefold() in display.casefold():
                matches.append(app_commands.Choice(name=display[:100], value=name))
        return matches[:25]

    async def item_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        economy = load_economy()
        names = set(economy.get("sell_prices", {}).keys())
        auctions = economy.get("auctions", {})
        if isinstance(auctions, dict):
            for collection in (auctions.get("open", []), auctions.get("history", [])):
                if isinstance(collection, list):
                    for entry in collection:
                        if isinstance(entry, dict) and entry.get("material"):
                            names.add(str(entry["material"]))
        matches = [name for name in sorted(names) if current.casefold() in name.replace("_", " ").casefold()]
        return [app_commands.Choice(name=name.replace("_", " ").title()[:100], value=name) for name in matches[:25]]

    @app_commands.command(name="ordering", description="Show live Minecraft item order prices")
    async def ordering(self, interaction: discord.Interaction) -> None:
        economy = load_economy()
        symbol = str(economy.get("currency_symbol", "$"))
        materials: dict[str, dict] = {}
        for spawner_data in economy["spawners"].values():
            if not isinstance(spawner_data, dict):
                continue
            for drop in spawner_data.get("drops", []):
                if isinstance(drop, dict) and drop.get("material"):
                    materials.setdefault(str(drop["material"]), drop)
        lines = []
        for material, drop in sorted(materials.items()):
            best_price = max(0.0, number(drop.get("best_order_price")))
            ordered = max(0.0, number(drop.get("ordered_amount")))
            if best_price > 0 and ordered > 0:
                name = str(drop.get("display_name", material.replace("_", " ").title()))
                lines.append(f"**{name}** — {money_text(symbol, best_price)} each · {ordered:,.0f} wanted")
        embed = discord.Embed(title="Active Item Orders", description=fit_lines(lines, 4000) if lines else "There are no active orders for spawner drops.", color=discord.Color.gold())
        embed.set_footer(text="Live data from the Minecraft server's Density orders." if economy.get("live_sync") else "Use /api sync to refresh live values.")
        await interaction.response.send_message(embed=embed)

    @spawner.command(name="count", description="Show all real server spawner counts by type")
    async def spawner_count(self, interaction: discord.Interaction) -> None:
        economy = load_economy()
        lines: list[str] = []
        total_spawners = total_blocks = 0
        counts_available = False
        for name, data in economy["spawners"].items():
            if not isinstance(data, dict) or is_decoy_spawner(name, data):
                continue
            count_value = data.get("server_count")
            if not isinstance(count_value, (int, float)) or isinstance(count_value, bool):
                continue
            counts_available = True
            count = max(0, int(count_value))
            blocks = max(0, int(number(data.get("physical_blocks"), count)))
            total_spawners += count
            total_blocks += blocks
            if count:
                display = str(data.get("display_name", "Spawner"))
                block_text = f" in {blocks:,} placed block{'s' if blocks != 1 else ''}" if blocks != count else ""
                lines.append(f"• **{display}** — {count:,}{block_text}")
        lines.sort(key=str.casefold)
        embed = discord.Embed(title="Server Spawner Counts", description=fit_lines(lines, 4000) if lines else "No real spawners were found.", color=discord.Color.blurple())
        if counts_available:
            embed.add_field(name="Total spawners", value=f"{total_spawners:,}")
            embed.add_field(name="Placed blocks", value=f"{total_blocks:,}")
            embed.set_footer(text="Includes Overworld and Nether spawners. Decoy spawners are hidden.")
        else:
            embed.set_footer(text="Use /api sync to load server counts.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="calc", description="Estimate spawner drops and current order money per hour")
    @app_commands.describe(spawner="Spawner type", amount="Number of spawners in the stack")
    @app_commands.autocomplete(spawner=spawner_autocomplete)
    async def calc(self, interaction: discord.Interaction, spawner: str, amount: app_commands.Range[int, 1, 1_000_000]) -> None:
        economy = load_economy()
        data = economy["spawners"].get(spawner)
        if not isinstance(data, dict) or is_decoy_spawner(spawner, data):
            await interaction.response.send_message("That spawner is not available.", ephemeral=True)
            return
        exponent = min(1.0, max(0.01, number(data.get("stacking_efficiency_exponent"), 1.0)))
        effective = max(1, math.floor((amount**exponent) + 0.5))
        detailed = data.get("drops") if isinstance(data.get("drops"), list) else []
        drop_lines: list[str] = []
        total_drops = total_sold = total_money = 0.0
        for drop in detailed:
            if not isinstance(drop, dict):
                continue
            hourly = max(0.0, number(drop.get("drops_per_hour_per_spawner"))) * effective
            sold, earned = fill_orders(hourly, drop.get("order_book"))
            total_drops += hourly
            total_sold += sold
            total_money += earned
            drop_lines.append(f"**{drop.get('display_name', 'Drops')}:** {hourly:,.0f}/hr · orders buy {sold:,.0f}")
        if not detailed:
            total_drops = max(0.0, number(data.get("drops_per_hour_per_spawner"))) * effective
            price = max(0.0, number(data.get("order_price")))
            total_sold = total_drops if price else 0
            total_money = total_sold * price
            drop_lines.append(f"**{data.get('drop_name', 'Drops')}:** {total_drops:,.0f}/hr")
        symbol = str(economy.get("currency_symbol", "$"))
        embed = discord.Embed(title=f"{data.get('display_name', 'Spawner')} Estimate", color=discord.Color.green())
        embed.add_field(name="Spawner stack", value=f"{amount:,}")
        embed.add_field(name="Expected drops/hour", value=f"{total_drops:,.0f}", inline=False)
        embed.add_field(name="Drop breakdown", value=fit_lines(drop_lines), inline=False)
        embed.add_field(name="Current order money/hour", value=money_text(symbol, total_money), inline=False)
        embed.add_field(name="Order capacity", value=f"{total_sold:,.0f} sellable now · {max(0, total_drops-total_sold):,.0f} without an active order", inline=False)
        embed.set_footer(text="Uses live DonutSpawners rates and Density orders; chance-based output varies.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="sellprice", description="Look up a live Minecraft /sell price")
    @app_commands.autocomplete(item=item_autocomplete)
    async def sellprice_command(self, interaction: discord.Interaction, item: str) -> None:
        economy = load_economy()
        price, display = sell_price(economy, item)
        if price <= 0:
            await interaction.response.send_message("That item has no live `/sell` price. Run `/api sync` after updating DensityBridge.", ephemeral=True)
            return
        await interaction.response.send_message(f"**{display}** sells for **{money_text(str(economy.get('currency_symbol', '$')), price)} each**.")

    @farm.command(name="pickle", description="Calculate sea pickle farm profit from bones used per hour")
    @app_commands.describe(bones_per_hour="Bones used each hour", pickles_per_bone="Average pickles made per bone")
    async def pickle_profit(
        self,
        interaction: discord.Interaction,
        bones_per_hour: app_commands.Range[int, 1, 100_000_000],
        pickles_per_bone: app_commands.Range[float, 0.01, 100.0] = 4.0,
    ) -> None:
        economy = load_economy()
        pickle_price, _ = sell_price(economy, "SEA_PICKLE")
        bone_price, _ = sell_price(economy, "BONE")
        if pickle_price <= 0:
            await interaction.response.send_message("The live Sea Pickle `/sell` price is unavailable. Use `/api sync` after the bridge update.", ephemeral=True)
            return
        gross_per_bone = pickle_price * pickles_per_bone
        net_per_bone = gross_per_bone - bone_price
        symbol = str(economy.get("currency_symbol", "$"))
        embed = discord.Embed(title="Sea Pickle Farm Profit", color=discord.Color.green())
        embed.add_field(name="Live sell prices", value=f"Sea Pickle: {money_text(symbol, pickle_price)}\nBone: {money_text(symbol, bone_price)}")
        embed.add_field(name="Output", value=f"{bones_per_hour * pickles_per_bone:,.0f} pickles/hour")
        embed.add_field(name="Profit per bone", value=money_text(symbol, net_per_bone))
        embed.add_field(name="Gross per hour", value=money_text(symbol, bones_per_hour * gross_per_bone))
        embed.add_field(name="Bone value per hour", value=money_text(symbol, bones_per_hour * bone_price))
        embed.add_field(name="Net profit per hour", value=money_text(symbol, bones_per_hour * net_per_bone))
        embed.set_footer(text="Default assumes 4 sea pickles per bone; change pickles_per_bone to match your farm.")
        await interaction.response.send_message(embed=embed)

    @farm.command(name="bamboo", description="Calculate bamboo farm profit per hour")
    async def bamboo_profit(self, interaction: discord.Interaction, bamboo_per_hour: app_commands.Range[int, 1, 1_000_000_000]) -> None:
        economy = load_economy()
        price, _ = sell_price(economy, "BAMBOO")
        if price <= 0:
            await interaction.response.send_message("The live Bamboo `/sell` price is unavailable. Use `/api sync` after the bridge update.", ephemeral=True)
            return
        symbol = str(economy.get("currency_symbol", "$"))
        embed = discord.Embed(title="Bamboo Farm Profit", color=discord.Color.green())
        embed.add_field(name="Bamboo/hour", value=f"{bamboo_per_hour:,}")
        embed.add_field(name="Live /sell price", value=money_text(symbol, price))
        embed.add_field(name="Profit/hour", value=money_text(symbol, bamboo_per_hour * price), inline=False)
        await interaction.response.send_message(embed=embed)

    @auction.command(name="browse", description="Browse current Minecraft auction listings")
    @app_commands.autocomplete(item=item_autocomplete)
    async def auction_browse(self, interaction: discord.Interaction, item: str | None = None) -> None:
        economy = load_economy()
        auctions = economy.get("auctions", {})
        listings = auctions.get("open", []) if isinstance(auctions, dict) else []
        if item:
            listings = [entry for entry in listings if isinstance(entry, dict) and str(entry.get("material", "")).casefold() == item.casefold()]
        listings = sorted((entry for entry in listings if isinstance(entry, dict)), key=lambda entry: number(entry.get("price")))[:20]
        symbol = str(economy.get("currency_symbol", "$"))
        lines = [f"**{entry.get('display_name', str(entry.get('material', 'Item')).title())} ×{int(number(entry.get('amount'), 1))}** — {money_text(symbol, number(entry.get('price')))} · {entry.get('seller_name', 'Unknown')}" for entry in listings]
        embed = discord.Embed(title="Live Auction Listings", description=fit_lines(lines, 4000) if lines else "No matching open listings.", color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    @auction.command(name="track", description="Show sold/bought auction history as a trading graph")
    @app_commands.autocomplete(item=item_autocomplete)
    async def auction_track(self, interaction: discord.Interaction, item: str, hours: app_commands.Range[int, 1, 168] = 24) -> None:
        economy = load_economy()
        auctions = economy.get("auctions", {})
        history = auctions.get("history", []) if isinstance(auctions, dict) else []
        cutoff = int(time.time()) - hours * 3600
        trades = [entry for entry in history if isinstance(entry, dict) and str(entry.get("material", "")).casefold() == item.casefold() and auction_time(entry.get("created_at")) >= cutoff]
        bucket_count = min(12, hours)
        bucket_seconds = max(3600, math.ceil(hours * 3600 / bucket_count))
        volumes = [0.0] * bucket_count
        money = [0.0] * bucket_count
        now = int(time.time())
        for trade in trades:
            age = max(0, now - auction_time(trade.get("created_at")))
            index = bucket_count - 1 - min(bucket_count - 1, age // bucket_seconds)
            volumes[index] += max(1.0, number(trade.get("amount"), 1))
            money[index] += max(0.0, number(trade.get("price")))
        maximum = max(volumes, default=0)
        graph_lines = []
        for index, volume in enumerate(volumes):
            end_time = now - (bucket_count - 1 - index) * bucket_seconds
            bar = "█" * (0 if maximum <= 0 else max(1, round(volume / maximum * 12)))
            graph_lines.append(f"{time.strftime('%d %H:%M', time.localtime(end_time))} | {bar:<12} {volume:,.0f}")
        symbol = str(economy.get("currency_symbol", "$"))
        open_listings = [entry for entry in auctions.get("open", []) if isinstance(entry, dict) and str(entry.get("material", "")).casefold() == item.casefold()] if isinstance(auctions, dict) else []
        embed = discord.Embed(title=f"{item.replace('_', ' ').title()} Auction Tracker", color=discord.Color.teal())
        embed.description = "```\n" + "\n".join(graph_lines) + "\n```"
        embed.add_field(name="Bought / sold volume", value=f"{sum(volumes):,.0f} items in {len(trades):,} completed trade(s)")
        embed.add_field(name="Trading value", value=money_text(symbol, sum(money)))
        embed.add_field(name="Open listings", value=f"{len(open_listings):,}")
        if open_listings:
            embed.add_field(name="Lowest open price", value=money_text(symbol, min(number(entry.get("price")) for entry in open_listings)), inline=False)
        embed.set_footer(text="Every completed auction is one buy and one sale; the graph shows traded item volume.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economy(bot))

