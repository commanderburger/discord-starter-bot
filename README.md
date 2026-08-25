# Discord Starter Bot

A modular Python Discord bot with moderation, Minecraft status, spawner economy estimates, utility commands, and a help menu. Commands live in `cogs/`, so adding features later does not require turning the main file into one large script.

## Included features

- Removes Discord invite links outside `partners`, `announcements`, and `our-ad` (configurable).
- `/stats` checks a Java or Bedrock Minecraft server directly. No API key is needed.
- `/links` displays the server address and optional website/store/vote/Discord links.
- `/ordering` lists live Density order prices and quantities after an API sync.
- `/calc` uses DonutSpawners rates, stacking efficiency, and the live Density order book.
- `/spawner count` lists live server-wide stacked totals and placed blocks by type.
- `/help`, `/serverinfo`, `/userinfo`, `/avatar`, `/coinflip`, and `/roll`.
- Persistent Density SMP ticket panel with Support, Partnerships and Bug Report tickets.
- Automatic welcome messages in the `welcome` or `welcom` channel.
- Form-based `/gcreate` and `/autogcreate` giveaway setup with automatic `@giveaway ping` mentions.
- Private `/api` controls for the server owner and `Owner`/`Manager` roles.

## 1. Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), choose **New Application**, and give it a name.
2. Open **Bot**, choose **Reset Token**, and copy the token somewhere private. Do not paste it into source code or commit it to Git.
3. Under **Installation**, enable **Guild Install**. Add the `applications.commands` and `bot` scopes. Give the bot **View Channels**, **Send Messages**, **Embed Links**, **Read Message History**, and **Manage Messages** permissions.
4. On the **Bot** page, enable **Message Content Intent** and **Server Members Intent** under **Privileged Gateway Intents**. The invite filter needs message content and welcome messages need the members intent. Presence Intent can remain disabled.
5. Copy the install link, open it, and add the bot to a server you manage.
6. Optional but useful: in Discord, enable Developer Mode under **User Settings > Advanced**, right-click your server, and choose **Copy Server ID**. Use this as `TEST_GUILD_ID` while developing.

## 2. Run it on your computer

Install Python 3.11 or newer, then in this folder:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and enter the bot token. Then run:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

Or, if Docker Desktop is installed:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Run `/ping` in your Discord server. Stop the local bot before starting the TrueNAS copy; the same bot token should not be used by two running instances.

## 3. Publish the image for TrueNAS SCALE

TrueNAS needs an image it can pull. The included GitHub workflow publishes one to GitHub Container Registry (GHCR):

1. Create a GitHub repository and copy this project's contents into it.
2. Commit and push to the `main` branch.
3. In GitHub, open **Actions** and wait for **Publish container** to finish.
4. Open your GitHub profile's **Packages**, select `discord-starter-bot`, open **Package settings**, and change visibility to **Public**. This prevents TrueNAS from needing registry credentials.
5. Your image name is `ghcr.io/YOUR_GITHUB_USERNAME/discord-starter-bot:latest` (lowercase).

## 4. Host it on TrueNAS SCALE 24.10 or newer

Recent SCALE releases use Docker for Apps. The bot needs a small private `/data` storage mount, but no inbound port or router forwarding.

1. In TrueNAS, make sure the Apps pool has been configured.
2. Go to **Apps > Discover Apps > Custom App**.
3. Set the application name to `discord-bot`.
4. Select the image/custom-image setup and enter:
   - Repository/image: `ghcr.io/YOUR_GITHUB_USERNAME/discord-starter-bot`
   - Tag: `latest`
   - Pull policy: **Always** (useful while developing)
5. Add environment variables:
   - `DISCORD_TOKEN` = the bot token
   - `MINECRAFT_SERVER` = the address players use, such as `play.example.net:25565`
   - `MINECRAFT_EDITION` = `java` or `bedrock`
   - `INVITE_EXEMPT_CHANNELS` = `partners,announcements,our-ad`
   - `STAFF_ROLE_NAMES` = `Owner,Co Owner,Manager,Admin,Moderator,Staff,Support`
   - `SENIOR_ROLE_NAMES` = `Owner,Co Owner,Manager`
   - `GIVEAWAY_PING_ROLE` = `giveaway ping`
   - `TICKET_CHANNEL_NAMES` = `ticket,tickets`
   - `WELCOME_CHANNEL_NAMES` = `welcome,welcom`
   - `API_ALLOWED_ROLES` = `Owner,Manager`
   - `TEST_GUILD_ID` = your server ID (optional)
   - `LOG_LEVEL` = `INFO` (optional)
6. Set restart policy to **Unless Stopped**.
7. Add an **ixVolume** or host-path storage mount at `/data`. This keeps the private API configuration and synced economy data across container updates. Do not expose `/data` through SMB or other shares.
8. Do not add ports, host networking, privileged mode, or extra capabilities.
9. Save/install. Open the app's logs and look for `Logged in as` and `Synced ... command(s)`.

If your Custom App screen offers a Docker Compose YAML editor instead, use this, replacing both placeholders:

```yaml
services:
  discord-bot:
    image: ghcr.io/YOUR_GITHUB_USERNAME/discord-starter-bot:latest
    restart: unless-stopped
    environment:
      DISCORD_TOKEN: "YOUR_DISCORD_BOT_TOKEN"
      TEST_GUILD_ID: "YOUR_SERVER_ID"
      LOG_LEVEL: "INFO"
```

Treat the token like a password. Anyone who gets it can control the bot. If it leaks, reset it immediately in the Developer Portal and update TrueNAS.

### Minecraft API key

`/stats` uses Minecraft's public server-status protocol through `mcstatus`; it does not require an API account or key. The NAS must be able to make outbound DNS and network connections to the Minecraft server.

### Spawner rates and order prices

Install `DensityBridge-1.1.0.jar` on the Paper server. It reads DonutSpawners rates, every loaded Minecraft world's real spawner stacks (including Nether worlds), active Density orders, Density `/sell` prices, and auction history directly. There is no third-party API key to obtain and no need to maintain prices by hand. Decoy spawners stay in the raw private API for diagnostics but are deliberately hidden from `/spawner count` and `/calc`.

### Private API commands

The Discord server owner is always authorized. Members with a role named `Owner` or `Manager` are also authorized by default. Change `API_ALLOWED_ROLES` in TrueNAS to use different comma-separated role names.

- `/api set-url` stores the full JSON API endpoint.
- `/api set-key` opens a private modal and stores the key without displaying it.
- `/api status` shows which parts are configured without revealing their values.
- `/api test` checks the endpoint and reports only its HTTP result.
- `/api sync` imports spawner counts, full drop tables, stacking efficiency, the active order book, `/sell` prices, and auction trades.

### Added commands

- `/farm pickle` calculates gross and net sea-pickle profit per bone and per hour from the live `/sell` prices.
- `/farm bamboo` calculates live bamboo profit per hour.
- `/auction browse` shows current listings; `/auction track` draws a bought/sold trading-volume graph.
- `/mute`, `/tempmute`, `/unmute`, `/purge`, `/warn`, `/warnings`, and `/clearwarnings` are available to configured staff roles. `/ban` and `/kick` are restricted to Manager, Co-Owner, Owner and the Discord server owner.
- `/gcreate` and `/autogcreate` open private forms. New giveaways ping `@giveaway ping`; no role is required to enter.
- `/gend`, `/greroll`, `/autoglist`, and `/autogstop` manage giveaways after creation.
- The bot posts its own ticket selector in the ticket channel and creates private Support, Partnerships and Bug Report channels.
- New members are welcomed in the welcome channel with their current member number.

Giveaways and warnings are saved under `/data`, so the existing TrueNAS `/data` storage mount keeps them after updates and restarts.

DensityBridge provides the endpoint in this shape (shortened here):

```json
{
  "spawners": {
    "zombie": {
      "display_name": "Zombie Spawner",
      "drop_name": "Rotten Flesh",
      "server_count": 125,
      "physical_blocks": 4,
      "stacking_efficiency_exponent": 1,
      "drops": [
        {
          "material": "ROTTEN_FLESH",
          "drops_per_hour_per_spawner": 576,
          "order_book": [{"price": 2, "remaining": 10000}]
        }
      ]
    }
  }
}
```

When a key is configured, the bot sends it both as `Authorization: Bearer <key>` and `X-API-Key: <key>` for compatibility. Use HTTPS whenever the API is not confined to a trusted private network. The key is stored at `/data/api-config.json` with owner-only file permissions and is never included in command output or logs.

## Updating the bot

Edit or add files in `cogs/` or `config/`, push to `main`, and let GitHub build a new `latest` image. Then use **Pull Image / Update / Redeploy** in the TrueNAS app menu (the exact label varies by SCALE release).

To add a command, put another method in `General` using the same pattern:

```python
@app_commands.command(name="rules", description="Show the server rules")
async def rules(self, interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Be kind and have fun!")
```

With `TEST_GUILD_ID` set, command changes usually appear quickly. Global Discord command updates can take longer.

## Troubleshooting

- **`DISCORD_TOKEN is not set`**: the environment variable name is wrong or missing in TrueNAS.
- **Improper token / 401**: reset the token in Discord and update it in TrueNAS.
- **Bot is offline**: inspect the TrueNAS app logs and confirm the NAS can reach the internet and resolve DNS.
- **Commands are missing**: confirm the app was installed with the `applications.commands` scope, check `TEST_GUILD_ID`, and inspect the sync line in the logs.
- **Invite links are not removed**: enable Message Content Intent in Discord and grant the bot Manage Messages in the channel.
- **`/stats` says not configured**: add `MINECRAFT_SERVER` to the TrueNAS app environment variables and redeploy.
- **`/stats` says offline**: verify the address, edition, DNS, port, and that the Minecraft server permits status pings.
- **Image pull denied**: make the GHCR package public, or configure registry credentials in TrueNAS.

## Project layout

```text
bot.py                         startup and command syncing
cogs/general.py                starter commands
Dockerfile                     production container
compose.yaml                   local Docker testing
.github/workflows/...          publishes the image to GHCR
```
