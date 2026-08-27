"""Persistent, poisoning-resistant knowledge for mention-based AI chat."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import discord
from discord.ext import commands


log = logging.getLogger("starter-bot.server-knowledge")

DEFAULT_CHANNELS = (
    "announcements,rules,faq,information,server-info,links,updates,bot-updates"
)
DEFAULT_TRUSTED_ROLES = (
    "Owner,Co Owner,Co-Owner,Manager,Admin,Moderator,Staff Team,Staff"
)
DEFAULT_EXCLUDED_WORDS = (
    "staff,ticket,transcript,logs,punishment,application,admin,moderation,private,"
    "appeal,report,claim,api"
)
STOP_WORDS = {
    "about", "after", "also", "and", "are", "been", "before", "but", "can",
    "could", "density", "discord", "does", "for", "from", "have", "how", "into",
    "just", "minecraft", "not", "our", "server", "smp", "that", "the", "their",
    "then", "there", "they", "this", "was", "what", "when", "where", "which",
    "who", "will", "with", "would", "you", "your",
}
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_'-]{2,}", re.IGNORECASE)
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:token|password|secret|api[ _-]?key)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b"),
)
LESSON_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
LESSON_MENTION_RE = re.compile(r"<@&?\d+>|<@!\d+>|<#\d+>")
LESSON_ID_RE = re.compile(r"\b\d{15,20}\b")


def _csv(name: str, default: str) -> set[str]:
    return {part.strip().casefold() for part in os.getenv(name, default).split(",") if part.strip()}


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in TOKEN_RE.findall(text)
        if token.casefold() not in STOP_WORDS
    }


def clean_partnership_lesson(content: str) -> str | None:
    content = LESSON_URL_RE.sub("[link omitted]", content)
    content = LESSON_MENTION_RE.sub("[mention omitted]", content)
    content = LESSON_ID_RE.sub("[ID omitted]", content)
    content = " ".join(content.split()).strip()[:1200]
    if len(content) < 40 or any(pattern.search(content) for pattern in SECRET_PATTERNS):
        return None
    return content


class KnowledgeDatabase:
    def __init__(self, path: Path, maximum_rows: int, retention_days: int) -> None:
        self.path = path
        self.maximum_rows = maximum_rows
        self.retention_seconds = retention_days * 86_400
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    channel_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    token_text TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS knowledge_guild_updated
                    ON knowledge(guild_id, updated_at DESC);
                """
            )

    def upsert(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        channel_name: str,
        content: str,
        created_at: int,
    ) -> None:
        token_text = " ".join(sorted(_tokens(content)))
        if not token_text:
            return
        now = int(time.time())
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO knowledge (
                    message_id, guild_id, channel_id, channel_name, content,
                    token_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    channel_name=excluded.channel_name,
                    content=excluded.content,
                    token_text=excluded.token_text,
                    updated_at=excluded.updated_at
                """,
                (
                    message_id, guild_id, channel_id, channel_name, content,
                    token_text, created_at, now,
                ),
            )
            cutoff = now - self.retention_seconds
            database.execute("DELETE FROM knowledge WHERE updated_at < ?", (cutoff,))
            database.execute(
                """
                DELETE FROM knowledge WHERE message_id IN (
                    SELECT message_id FROM knowledge
                    ORDER BY updated_at DESC LIMIT -1 OFFSET ?
                )
                """,
                (self.maximum_rows,),
            )

    def remove(self, message_id: int) -> None:
        with self._connect() as database:
            database.execute("DELETE FROM knowledge WHERE message_id = ?", (message_id,))

    def search(self, guild_id: int, query: str, limit: int) -> list[dict[str, object]]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT message_id, channel_id, channel_name, content, token_text, updated_at
                FROM knowledge
                WHERE guild_id = ?
                ORDER BY updated_at DESC
                LIMIT 800
                """,
                (guild_id,),
            ).fetchall()

        ranked: list[tuple[float, sqlite3.Row]] = []
        now = time.time()
        lowered_query = query.casefold().strip()
        for row in rows:
            row_tokens = set(str(row["token_text"]).split())
            overlap = query_tokens & row_tokens
            if not overlap:
                continue
            score = sum(1.0 + min(len(token), 10) / 20 for token in overlap)
            if lowered_query and lowered_query in str(row["content"]).casefold():
                score += 3
            age_days = max(0, (now - int(row["updated_at"])) / 86_400)
            score += max(0, 1 - age_days / 180)
            ranked.append((score, row))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in ranked[:limit]]

    def count(self, guild_id: int) -> int:
        with self._connect() as database:
            row = database.execute(
                "SELECT COUNT(*) AS total FROM knowledge WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return int(row["total"]) if row else 0


class ServerKnowledge(commands.Cog):
    """Learns safe server facts from trusted messages in public channels."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.learn_channels = _csv("AI_LEARN_CHANNELS", DEFAULT_CHANNELS)
        self.trusted_roles = _csv("AI_LEARN_ROLE_NAMES", DEFAULT_TRUSTED_ROLES)
        self.excluded_words = _csv("AI_LEARN_EXCLUDED_WORDS", DEFAULT_EXCLUDED_WORDS)
        self.backfill_messages = _integer("AI_LEARN_BACKFILL_MESSAGES", 75, 0, 500)
        self.search_results = _integer("AI_KNOWLEDGE_RESULTS", 6, 1, 12)
        database_path = Path(os.getenv("AI_KNOWLEDGE_DB", "/data/ai-knowledge.sqlite3"))
        self.database = KnowledgeDatabase(
            database_path,
            _integer("AI_KNOWLEDGE_MAX_ROWS", 5000, 100, 50_000),
            _integer("AI_KNOWLEDGE_RETENTION_DAYS", 180, 7, 730),
        )
        self._backfilled_guilds: set[int] = set()
        self._backfill_lock = asyncio.Lock()

    def _is_safe_channel(self, channel: discord.abc.GuildChannel) -> bool:
        if not isinstance(channel, discord.TextChannel):
            return False
        if channel.is_nsfw():
            return False
        names = [channel.name.casefold()]
        if channel.category:
            names.append(channel.category.name.casefold())
        return not any(word in name for word in self.excluded_words for name in names)

    def _is_allowed_source(self, message: discord.Message) -> bool:
        channel = message.channel
        if not isinstance(channel, discord.TextChannel) or not self._is_safe_channel(channel):
            return False
        if message.author.bot or message.webhook_id is not None:
            return False
        if self.bot.user is not None and self.bot.user in message.mentions:
            return False
        if channel.name.casefold() in self.learn_channels or "*" in self.learn_channels:
            return True
        member = message.author
        return isinstance(member, discord.Member) and any(
            role.name.casefold() in self.trusted_roles for role in member.roles
        )

    @staticmethod
    def _clean_message(message: discord.Message) -> str | None:
        content = " ".join(message.clean_content.split()).strip()
        if content.startswith(("!", "/")) or content.endswith("?") or len(content) < 20:
            return None
        content = content[:1000]
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            return None
        return content

    async def remember(self, message: discord.Message) -> None:
        if message.guild is None or not self._is_allowed_source(message):
            return
        content = self._clean_message(message)
        if content is None:
            return
        await asyncio.to_thread(
            self.database.upsert,
            message_id=message.id,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", "unknown"),
            content=content,
            created_at=int(message.created_at.timestamp()),
        )

    async def relevant_context(self, guild_id: int, query: str) -> str:
        rows = await asyncio.to_thread(
            self.database.search, guild_id, query, self.search_results
        )
        if not rows:
            return "No relevant trusted server knowledge was found."
        excerpts = []
        for row in rows:
            content = str(row["content"]).replace("```", "'''")
            excerpts.append(f"- #{row['channel_name']}: {content}")
        return "\n".join(excerpts)

    async def remember_partnership_lesson(
        self,
        *,
        guild_id: int,
        source_message_id: int,
        source_channel_id: int,
        content: str,
    ) -> bool:
        cleaned = clean_partnership_lesson(content)
        if cleaned is None:
            return False
        await asyncio.to_thread(
            self.database.upsert,
            message_id=source_message_id,
            guild_id=guild_id,
            channel_id=source_channel_id,
            channel_name="partnership-help",
            content=cleaned,
            created_at=int(time.time()),
        )
        return True

    async def _backfill_guild(self, guild: discord.Guild) -> None:
        if guild.id in self._backfilled_guilds or self.backfill_messages <= 0:
            return
        async with self._backfill_lock:
            if guild.id in self._backfilled_guilds:
                return
            learned = 0
            for channel in guild.text_channels:
                if not self._is_safe_channel(channel):
                    continue
                if channel.name.casefold() not in self.learn_channels and "*" not in self.learn_channels:
                    continue
                me = guild.me
                if me is None or not channel.permissions_for(me).read_message_history:
                    continue
                try:
                    async for message in channel.history(limit=self.backfill_messages, oldest_first=False):
                        before = await asyncio.to_thread(self.database.count, guild.id)
                        await self.remember(message)
                        after = await asyncio.to_thread(self.database.count, guild.id)
                        learned += max(0, after - before)
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("Could not backfill #%s", channel.name)
            self._backfilled_guilds.add(guild.id)
            total = await asyncio.to_thread(self.database.count, guild.id)
            log.info("Knowledge ready for %s: %d stored facts (%d added)", guild.name, total, learned)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            asyncio.create_task(self._backfill_guild(guild))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.remember(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        await asyncio.to_thread(self.database.remove, after.id)
        await self.remember(after)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        await asyncio.to_thread(self.database.remove, message.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerKnowledge(bot))
