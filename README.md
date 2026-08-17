# Discord Starter Bot

A small Python bot with `/ping`, `/hello`, and `/about` slash commands. Commands live in `cogs/`, so adding features later does not require turning the main file into one large script.

## 1. Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), choose **New Application**, and give it a name.
2. Open **Bot**, choose **Reset Token**, and copy the token somewhere private. Do not paste it into source code or commit it to Git.
3. Under **Installation**, enable **Guild Install**. Add the `applications.commands` and `bot` scopes. For this starter, the bot only needs **Send Messages** and **Embed Links** permissions.
4. Copy the install link, open it, and add the bot to a server you manage.
5. Optional but useful: in Discord, enable Developer Mode under **User Settings > Advanced**, right-click your server, and choose **Copy Server ID**. Use this as `TEST_GUILD_ID` while developing.

The bot uses slash commands, so **Message Content Intent is not required**.

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

Recent SCALE releases use Docker for Apps. No storage mount or port forwarding is needed for this bot.

1. In TrueNAS, make sure the Apps pool has been configured.
2. Go to **Apps > Discover Apps > Custom App**.
3. Set the application name to `discord-bot`.
4. Select the image/custom-image setup and enter:
   - Repository/image: `ghcr.io/YOUR_GITHUB_USERNAME/discord-starter-bot`
   - Tag: `latest`
   - Pull policy: **Always** (useful while developing)
5. Add environment variables:
   - `DISCORD_TOKEN` = the bot token
   - `TEST_GUILD_ID` = your server ID (optional)
   - `LOG_LEVEL` = `INFO` (optional)
6. Set restart policy to **Unless Stopped**.
7. Do not add ports, host networking, privileged mode, storage, or extra capabilities.
8. Save/install. Open the app's logs and look for `Logged in as` and `Synced ... command(s)`.

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

## Updating the bot

Edit or add files in `cogs/`, push to `main`, and let GitHub build a new `latest` image. Then use **Pull Image / Update / Redeploy** in the TrueNAS app menu (the exact label varies by SCALE release).

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
- **Image pull denied**: make the GHCR package public, or configure registry credentials in TrueNAS.

## Project layout

```text
bot.py                         startup and command syncing
cogs/general.py                starter commands
Dockerfile                     production container
compose.yaml                   local Docker testing
.github/workflows/...          publishes the image to GHCR
```
