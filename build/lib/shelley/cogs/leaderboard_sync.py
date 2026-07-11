from __future__ import annotations

import io
import json
import logging
from typing import Literal

import discord

from ..config import BotConfig
from ..state import state_repository
from .leaderboard_renderer import render_points_leaderboard_png
from .points_state import PointsRow, PointsStore

logger = logging.getLogger(__name__)

LEADERBOARD_LIMIT = 10


class LeaderboardSync:
    def __init__(self, bot: discord.Client, store: PointsStore) -> None:
        self.bot = bot
        self.store = store
        self.message: discord.Message | None = None
        self.signature: str | None = None
        self.dirty = True

    def mark_dirty(self) -> None:
        self.dirty = True

    async def resolve_board_name(self, guild: discord.Guild | None, row: PointsRow) -> str:
        if guild is not None:
            member = guild.get_member(int(row.user_id))
            if member is not None:
                return str(member.display_name or member.name)
        user = self.bot.get_user(int(row.user_id))
        if user is not None:
            return str(getattr(user, "global_name", None) or user.name)
        return str(row.last_display_name or row.last_name or row.user_id)

    async def rows(self, guild: discord.Guild, field: Literal["text_points", "voice_points"]) -> list[tuple[int, str, int]]:
        rows = self.store.top(int(guild.id), field, LEADERBOARD_LIMIT)
        result: list[tuple[int, str, int]] = []
        for rank, row in enumerate(rows, start=1):
            name = await self.resolve_board_name(guild, row)
            result.append((rank, name, row.points_for(field)))
        return result

    async def build_message(self, guild: discord.Guild, config: BotConfig) -> tuple[list[discord.Embed], list[discord.File], str]:
        leaderboard_config = config.points.leaderboard
        text_color = int(leaderboard_config.text_color)
        voice_color = int(leaderboard_config.voice_color)
        placeholder_text = str(leaderboard_config.placeholder_text)
        text_title = "Текст поинты"
        voice_title = "Войс поинты"
        text_rows = await self.rows(guild, "text_points")
        voice_rows = await self.rows(guild, "voice_points")
        assets = [
            (text_title, "text", "points-text.png", text_color, text_rows),
            (voice_title, "voice", "points-voice.png", voice_color, voice_rows),
        ]
        embeds: list[discord.Embed] = []
        files: list[discord.File] = []
        signature_payload: list[dict] = []
        for title, icon_kind, filename, color, rows in assets:
            png = render_points_leaderboard_png(rows, icon_kind=icon_kind, accent_color=color, placeholder_text=placeholder_text)
            file = discord.File(io.BytesIO(png), filename=filename)
            embed = discord.Embed(title=title, color=int(color))
            embed.set_image(url=f"attachment://{filename}")
            files.append(file)
            embeds.append(embed)
            signature_payload.append(
                {
                    "title": title,
                    "icon": icon_kind,
                    "color": int(color),
                    "rows": rows,
                    "placeholder_text": placeholder_text,
                    "limit": LEADERBOARD_LIMIT,
                    "image_version": 8,
                }
            )
        signature = json.dumps(signature_payload, ensure_ascii=False, sort_keys=True)
        return embeds, files, signature

    async def sync(self, config: BotConfig) -> None:
        leaderboard_config = config.points.leaderboard
        channel_id = int(leaderboard_config.channel_id)
        if channel_id <= 0:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning("leaderboard channel is not a text channel", extra={"channel_id": channel_id})
            return
        guild_id = int(channel.guild.id)
        repo = state_repository(guild_id)
        message_id = int(repo.get("points_leaderboard_message_id", 0) or 0)
        embeds, files, signature = await self.build_message(channel.guild, config)
        if not self.dirty and signature == self.signature:
            return
        message = self.message
        if message is None or message.channel.id != channel.id:
            message = None
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    message = None
                except discord.HTTPException:
                    logger.exception("cannot fetch leaderboard message", extra={"message_id": message_id})
        if message is None:
            message = await channel.send(embeds=embeds, files=files, allowed_mentions=discord.AllowedMentions.none())
            repo.set("points_leaderboard_message_id", int(message.id))
            logger.info("created leaderboard message", extra={"message_id": int(message.id), "channel_id": channel_id})
        else:
            await message.edit(embeds=embeds, attachments=files, view=None, allowed_mentions=discord.AllowedMentions.none())
        self.message = message
        self.signature = signature
        self.dirty = False
