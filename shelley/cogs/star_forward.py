import asyncio
import logging

import discord
from discord.ext import commands

from ..settings import get_config
from ..state import delete_star_forward, get_star_forward, set_star_forward


logger = logging.getLogger(__name__)


class StarForwardCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()

    async def _count_reactors(self, message: discord.Message, emoji: str) -> int:
        for reaction in message.reactions:
            if str(reaction.emoji) != emoji:
                continue
            user_ids: set[int] = set()
            async for user in reaction.users(limit=None):
                if user.bot:
                    continue
                user_ids.add(user.id)
            return len(user_ids)
        return 0

    async def _delete_star_forward_from_state(
        self,
        guild_id: int,
        source_channel_id: int,
        source_message_id: int,
        target_channel_id: int,
    ) -> None:
        forwarded_message_id = delete_star_forward(guild_id, source_channel_id, source_message_id)
        if not forwarded_message_id:
            return
        try:
            target_channel = await self.bot.fetch_channel(int(target_channel_id))
            if isinstance(target_channel, discord.TextChannel):
                forwarded_message = await target_channel.fetch_message(int(forwarded_message_id))
                await forwarded_message.delete()
                logger.info("deleted star forward", extra={"forwarded_message_id": forwarded_message_id})
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.exception("forbidden deleting star forward")
        except discord.HTTPException:
            logger.exception("discord error deleting star forward")

    async def _handle_star_forward_update(self, payload) -> None:
        cfg = get_config()
        star_cfg = cfg.star_forward
        if not star_cfg.enabled:
            return
        source_channel_ids = {int(x) for x in star_cfg.source_channel_ids}
        target_channel_id = int(star_cfg.target_channel_id)
        emoji = str(star_cfg.emoji)
        threshold = int(star_cfg.threshold)
        guild_id = int(getattr(payload, "guild_id", None) or cfg.runtime_guild_id())
        source_channel_id = int(payload.channel_id)
        source_message_id = int(payload.message_id)
        if source_channel_id not in source_channel_ids:
            return
        payload_emoji = getattr(payload, "emoji", None)
        if payload_emoji is not None and str(payload_emoji) != emoji:
            return
        if self.bot.user is not None and getattr(payload, "user_id", None) == self.bot.user.id:
            return
        try:
            source_channel = await self.bot.fetch_channel(source_channel_id)
            target_channel = await self.bot.fetch_channel(target_channel_id)
            if not isinstance(source_channel, discord.TextChannel):
                logger.warning("star source channel is not a text channel", extra={"channel_id": source_channel_id})
                return
            if not isinstance(target_channel, discord.TextChannel):
                logger.warning("star target channel is not a text channel", extra={"channel_id": target_channel_id})
                return
            source_message = await source_channel.fetch_message(source_message_id)
            reactors_count = await self._count_reactors(source_message, emoji)
        except discord.NotFound:
            await self._delete_star_forward_from_state(guild_id, source_channel_id, source_message_id, target_channel_id)
            return
        except discord.Forbidden:
            logger.exception("forbidden fetching star source or target")
            return
        except discord.HTTPException:
            logger.exception("discord error fetching star source or target")
            return
        async with self._lock:
            forwarded_message_id = get_star_forward(guild_id, source_channel_id, source_message_id)
            if reactors_count >= threshold:
                if forwarded_message_id:
                    try:
                        await target_channel.fetch_message(int(forwarded_message_id))
                        return
                    except discord.NotFound:
                        delete_star_forward(guild_id, source_channel_id, source_message_id)
                is_forwardable = getattr(source_message, "is_forwardable", None)
                if callable(is_forwardable) and not is_forwardable():
                    logger.warning("source message is not forwardable", extra={"source_message_id": source_message_id})
                    return
                forward_method = getattr(source_message, "forward", None)
                if not callable(forward_method):
                    logger.warning("discord.py version does not support message forwarding")
                    return
                try:
                    forwarded = await source_message.forward(target_channel)
                    set_star_forward(guild_id, source_channel_id, source_message_id, target_channel_id, int(forwarded.id))
                    logger.info("forwarded starred message", extra={"forwarded_message_id": int(forwarded.id)})
                except discord.Forbidden:
                    logger.exception("forbidden forwarding starred message")
                except discord.HTTPException:
                    logger.exception("discord error forwarding starred message")
                return
            if forwarded_message_id:
                try:
                    forwarded_message = await target_channel.fetch_message(int(forwarded_message_id))
                    await forwarded_message.delete()
                    logger.info("removed star forward", extra={"forwarded_message_id": int(forwarded_message_id)})
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    logger.exception("forbidden deleting stale star forward")
                except discord.HTTPException:
                    logger.exception("discord error deleting stale star forward")
                delete_star_forward(guild_id, source_channel_id, source_message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload) -> None:
        try:
            await self._handle_star_forward_update(payload)
        except Exception:
            logger.exception("on_raw_reaction_add failed")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload) -> None:
        try:
            await self._handle_star_forward_update(payload)
        except Exception:
            logger.exception("on_raw_reaction_remove failed")

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload) -> None:
        try:
            await self._handle_star_forward_update(payload)
        except Exception:
            logger.exception("on_raw_reaction_clear failed")

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload) -> None:
        try:
            await self._handle_star_forward_update(payload)
        except Exception:
            logger.exception("on_raw_reaction_clear_emoji failed")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StarForwardCog(bot))
