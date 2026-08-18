import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


log = logging.getLogger("starter-bot.api")
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "/data"))
API_CONFIG_FILE = DATA_DIR / "api-config.json"
SYNCED_ECONOMY_FILE = DATA_DIR / "economy.json"
BUNDLED_ECONOMY_FILE = Path(__file__).resolve().parent.parent / "config" / "economy.json"
MAX_RESPONSE_BYTES = 2_500_000


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
        file.write("\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def load_json(path: Path, default: dict) -> dict:
    try:
        with path.open(encoding="utf-8") as file:
            value = json.load(file)
            return value if isinstance(value, dict) else default.copy()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default.copy()


def valid_api_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def allowed_role_names() -> set[str]:
    configured = os.getenv("API_ALLOWED_ROLES", "Owner,Manager")
    return {role.strip().casefold() for role in configured.split(",") if role.strip()}


def is_api_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.guild.owner_id == interaction.user.id:
        return True
    allowed = allowed_role_names()
    return any(role.name.casefold() in allowed for role in interaction.user.roles)


async def reject_unless_api_admin(interaction: discord.Interaction) -> bool:
    if is_api_admin(interaction):
        return False
    await interaction.response.send_message(
        "Only the server owner or an Owner/Manager role can use this command.",
        ephemeral=True,
    )
    return True


class ApiKeyModal(discord.ui.Modal, title="Set private API key"):
    api_key = discord.ui.TextInput(
        label="API key",
        placeholder="Paste the key here",
        required=True,
        min_length=4,
        max_length=1000,
    )

    def __init__(self, cog: "ApiAdmin") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await reject_unless_api_admin(interaction):
            return
        await self.cog.update_config(api_key=str(self.api_key))
        await interaction.response.send_message(
            "API key saved privately. Use `/api test` to check it.",
            ephemeral=True,
        )


class ApiAdmin(commands.Cog):
    api = app_commands.Group(name="api", description="Owner and manager API controls")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config_lock = asyncio.Lock()

    async def update_config(self, **updates: str) -> None:
        async with self.config_lock:
            config = load_json(API_CONFIG_FILE, {})
            config.update(updates)
            await asyncio.to_thread(atomic_write_json, API_CONFIG_FILE, config)

    async def request_api(self) -> tuple[int, object]:
        config = load_json(API_CONFIG_FILE, {})
        url = str(config.get("url", "")).strip()
        key = str(config.get("api_key", "")).strip()
        if not valid_api_url(url):
            raise ValueError("The API URL has not been configured.")

        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
            headers["X-API-Key"] = key

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError("The API response is larger than 2.5 MB.")
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}
                return response.status, payload

    @api.command(name="status", description="Show API configuration without revealing secrets")
    async def status(self, interaction: discord.Interaction) -> None:
        if await reject_unless_api_admin(interaction):
            return
        config = load_json(API_CONFIG_FILE, {})
        embed = discord.Embed(title="API Status", color=discord.Color.blurple())
        embed.add_field(name="URL configured", value="Yes" if config.get("url") else "No")
        embed.add_field(name="Key configured", value="Yes" if config.get("api_key") else "No")
        embed.add_field(
            name="Synced economy data",
            value="Yes" if SYNCED_ECONOMY_FILE.exists() else "No",
            inline=False,
        )
        embed.set_footer(text="Secret values are never displayed.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @api.command(name="set-url", description="Set the Minecraft plugin API endpoint")
    @app_commands.describe(url="Full http:// or https:// JSON API endpoint")
    async def set_url(self, interaction: discord.Interaction, url: str) -> None:
        if await reject_unless_api_admin(interaction):
            return
        url = url.strip()
        if not valid_api_url(url):
            await interaction.response.send_message(
                "Enter a complete `http://` or `https://` URL.",
                ephemeral=True,
            )
            return
        await self.update_config(url=url)
        await interaction.response.send_message("API URL saved.", ephemeral=True)

    @api.command(name="set-key", description="Set the API key using a private form")
    async def set_key(self, interaction: discord.Interaction) -> None:
        if await reject_unless_api_admin(interaction):
            return
        await interaction.response.send_modal(ApiKeyModal(self))

    @api.command(name="test", description="Test the configured API connection")
    async def test(self, interaction: discord.Interaction) -> None:
        if await reject_unless_api_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            status, _ = await self.request_api()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            await interaction.followup.send(f"API test failed: {error}", ephemeral=True)
            return
        if 200 <= status < 300:
            await interaction.followup.send(f"API connection succeeded (`HTTP {status}`).", ephemeral=True)
        else:
            await interaction.followup.send(f"API returned `HTTP {status}`.", ephemeral=True)

    @api.command(name="sync", description="Sync spawner counts, rates and prices from the API")
    async def sync(self, interaction: discord.Interaction) -> None:
        if await reject_unless_api_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            status, payload = await self.request_api()
            if not 200 <= status < 300:
                raise ValueError(f"The API returned HTTP {status}.")
            if not isinstance(payload, dict) or not isinstance(payload.get("spawners"), dict):
                raise ValueError("The response must contain a JSON `spawners` object.")
            scan = payload.get("scan")
            if isinstance(scan, dict) and scan.get("running") is True:
                processed = scan.get("processed", 0)
                total = scan.get("total", "?")
                raise ValueError(
                    f"The Minecraft spawner scan is still running ({processed}/{total}). Try again shortly."
                )

            previous = load_json(
                SYNCED_ECONOMY_FILE if SYNCED_ECONOMY_FILE.exists() else BUNDLED_ECONOMY_FILE,
                {"currency_symbol": "$"},
            )
            economy = {
                "currency_symbol": str(previous.get("currency_symbol", "$"))[:8],
                "example_values": False,
                "live_sync": True,
                "synced_at": datetime.now(UTC).isoformat(),
                "spawners": {},
                "sell_prices": {},
                "auctions": {"open": [], "history": []},
            }
            changed = 0
            for name, incoming in payload["spawners"].items():
                if not isinstance(name, str) or not isinstance(incoming, dict):
                    continue
                current = {
                    "display_name": name.replace("_", " ").title() + " Spawner",
                    "drop_name": name.replace("_", " ").title() + " Drops",
                    "server_count": 0,
                    "physical_blocks": 0,
                    "stacking_efficiency_exponent": 1,
                    "cycle_seconds": 60,
                    "drops_per_hour_per_spawner": 0,
                    "order_price": 0,
                    "money_per_hour_per_spawner": 0,
                    "drops": [],
                }
                for field in (
                    "server_count",
                    "physical_blocks",
                    "stacking_efficiency_exponent",
                    "cycle_seconds",
                    "drops_per_hour_per_spawner",
                    "order_price",
                    "money_per_hour_per_spawner",
                ):
                    value = incoming.get(field)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                        current[field] = value
                for field in ("display_name", "drop_name"):
                    value = incoming.get(field)
                    if isinstance(value, str) and value.strip():
                        current[field] = value.strip()[:100]
                drops = incoming.get("drops")
                if isinstance(drops, list):
                    for drop in drops[:100]:
                        if not isinstance(drop, dict):
                            continue
                        clean_drop = {
                            "material": str(drop.get("material", ""))[:100],
                            "display_name": str(drop.get("display_name", "Drops"))[:100],
                            "drops_per_hour_per_spawner": 0,
                            "best_order_price": 0,
                            "ordered_amount": 0,
                            "order_book": [],
                        }
                        for field in (
                            "drops_per_hour_per_spawner",
                            "best_order_price",
                            "ordered_amount",
                        ):
                            value = drop.get(field)
                            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                                clean_drop[field] = value
                        order_book = drop.get("order_book")
                        if isinstance(order_book, list):
                            for level in order_book[:500]:
                                if not isinstance(level, dict):
                                    continue
                                price = level.get("price")
                                remaining = level.get("remaining")
                                if (
                                    isinstance(price, (int, float))
                                    and not isinstance(price, bool)
                                    and price >= 0
                                    and isinstance(remaining, (int, float))
                                    and not isinstance(remaining, bool)
                                    and remaining > 0
                                ):
                                    clean_drop["order_book"].append(
                                        {"price": price, "remaining": remaining}
                                    )
                        current["drops"].append(clean_drop)
                economy["spawners"][name[:100]] = current
                changed += 1

            sell_prices = payload.get("sell_prices")
            if isinstance(sell_prices, dict):
                for material, incoming in list(sell_prices.items())[:5000]:
                    if not isinstance(material, str) or not isinstance(incoming, dict):
                        continue
                    price = incoming.get("price")
                    if not isinstance(price, (int, float)) or isinstance(price, bool) or price < 0:
                        continue
                    economy["sell_prices"][material[:100].upper()] = {
                        "display_name": str(incoming.get("display_name", material.replace("_", " ").title()))[:100],
                        "price": price,
                    }

            auctions = payload.get("auctions")
            if isinstance(auctions, dict):
                for collection_name, maximum in (("open", 1000), ("history", 5000)):
                    collection = auctions.get(collection_name)
                    if not isinstance(collection, list):
                        continue
                    for incoming in collection[:maximum]:
                        if not isinstance(incoming, dict):
                            continue
                        material = incoming.get("material")
                        price = incoming.get("price")
                        if (
                            not isinstance(material, str)
                            or not isinstance(price, (int, float))
                            or isinstance(price, bool)
                            or price < 0
                        ):
                            continue
                        clean = {
                            "material": material[:100].upper(),
                            "display_name": str(incoming.get("display_name", material.replace("_", " ").title()))[:100],
                            "amount": max(1, int(incoming.get("amount", 1))) if isinstance(incoming.get("amount", 1), (int, float)) else 1,
                            "price": price,
                        }
                        if collection_name == "open":
                            clean["id"] = str(incoming.get("id", ""))[:100]
                            clean["seller_name"] = str(incoming.get("seller_name", "Unknown"))[:100]
                            clean["created_at"] = incoming.get("created_at", 0)
                            clean["expires_at"] = incoming.get("expires_at", 0)
                        else:
                            clean["created_at"] = incoming.get("created_at", 0)
                        economy["auctions"][collection_name].append(clean)

            await asyncio.to_thread(atomic_write_json, SYNCED_ECONOMY_FILE, economy)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as error:
            log.warning("API sync failed: %s", error)
            await interaction.followup.send(f"Sync failed: {error}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Synced **{changed}** spawner type(s), live sell prices, and auction history. Farm, auction, and spawner commands are refreshed.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ApiAdmin(bot))
