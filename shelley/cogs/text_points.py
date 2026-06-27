from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterable

import discord

from ..config import BotConfig, PointsTextConfig
from .points_state import PointsStore

logger = logging.getLogger(__name__)


def int_id_set(values: Iterable[int] | None) -> set[int]:
    return {int(value) for value in values or [] if int(value) > 0}


def channel_is_counted(channel_id: int, include_ids: set[int], exclude_ids: set[int]) -> bool:
    if include_ids and int(channel_id) not in include_ids:
        return False
    return int(channel_id) not in exclude_ids


def message_counting_channel_id(message: discord.Message) -> int | None:
    channel = message.channel
    if isinstance(channel, discord.TextChannel):
        return int(channel.id)
    if isinstance(channel, discord.Thread) and channel.parent_id is not None:
        return int(channel.parent_id)
    return None


def random_points_amount(config: PointsTextConfig) -> int:
    minimum = int(config.award_min)
    maximum = int(config.award_max)
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    return random.randint(minimum, maximum)


class TextPointsService:
    def __init__(self, store: PointsStore, mark_dirty) -> None:
        self.store = store
        self.mark_dirty = mark_dirty

    async def award_for_message(self, message: discord.Message, config: BotConfig, source: str) -> bool:
        if message.guild is None or message.author.bot:
            return False
        counting_channel_id = message_counting_channel_id(message)
        if counting_channel_id is None:
            return False
        points_config = config.points
        text_config = points_config.text
        if not points_config.enabled or not text_config.enabled:
            return False
        include_ids = int_id_set(text_config.channel_ids)
        exclude_ids = int_id_set(text_config.excluded_channel_ids)
        if not channel_is_counted(counting_channel_id, include_ids, exclude_ids):
            return False
        now = time.time()
        amount = random_points_amount(text_config)
        award = self.store.award(
            guild_id=int(message.guild.id),
            user_id=int(message.author.id),
            kind="text",
            amount=amount,
            now=now,
            cooldown=max(1.0, float(text_config.interval_seconds)),
            name=str(message.author),
            display_name=str(getattr(message.author, "display_name", message.author)),
            text_channel_id=counting_channel_id,
            text_message_id=int(message.id),
        )
        if award is None:
            if points_config.log_events:
                logger.info("text points cooldown", extra={"user_id": int(message.author.id), "source": source})
            return False
        self.mark_dirty()
        logger.info(
            "awarded text points",
            extra={
                "user_id": int(message.author.id),
                "channel_id": counting_channel_id,
                "amount": award.amount,
                "total": award.total,
                "source": source,
            },
        )
        return True

    async def poll_channels(self, bot: discord.Client, config: BotConfig) -> None:
        text_config = config.points.text
        include_ids = int_id_set(text_config.channel_ids)
        exclude_ids = int_id_set(text_config.excluded_channel_ids)
        channel_ids = sorted(channel_id for channel_id in include_ids if channel_id not in exclude_ids)
        if not channel_ids:
            return
        now = discord.utils.utcnow()
        for channel_id in channel_ids:
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    logger.exception("cannot fetch text points channel", extra={"channel_id": channel_id})
                    continue
            if not isinstance(channel, discord.TextChannel):
                continue
            guild_id = int(channel.guild.id)
            cursor = self.store.get_text_cursor(guild_id, channel_id)
            messages: list[discord.Message] = []
            try:
                if cursor:
                    after = discord.Object(id=cursor)
                    async for message in channel.history(limit=int(text_config.poll_limit), after=after, oldest_first=True):
                        messages.append(message)
                else:
                    async for message in channel.history(limit=int(text_config.poll_limit), oldest_first=False):
                        if (now - message.created_at).total_seconds() <= float(text_config.poll_initial_lookback_seconds):
                            messages.append(message)
            except discord.Forbidden:
                logger.warning("cannot poll text points channel because history is forbidden", extra={"channel_id": channel_id})
                continue
            except discord.HTTPException:
                logger.exception("cannot poll text points channel", extra={"channel_id": channel_id})
                continue
            if not messages:
                continue
            messages.sort(key=lambda item: int(item.id))
            max_seen_id = max(int(message.id) for message in messages)
            for message in messages:
                await self.award_for_message(message, config, source="poll")
            self.store.set_text_cursor(guild_id, channel_id, max_seen_id)
