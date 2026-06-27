import asyncio
import logging
import time
from typing import List, Optional

import discord
from discord.ext import commands

from ..discord_helpers import edit_welcome_message_safely, ensure_welcome_message, get_or_create_welcome_message
from ..renderers.welcome import load_welcome_message_payload
from ..settings import env_name, file_sha256, get_config
from ..state import get_welcome_message_file_hash, set_welcome_message_file_hash

logger = logging.getLogger(__name__)


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self.welcome_loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    async def welcome_loop(self) -> None:
        await self.bot.wait_until_ready()
        cfg = get_config()

        welcome_channel_id = cfg.welcome_channel_id
        if not welcome_channel_id:
            logger.info("welcome_channel_id is not configured; welcome loop disabled")
            return

        update_seconds = int(cfg.welcome_update_seconds or cfg.update_seconds)
        message_path = cfg.welcome_message_path

        ch = await self.bot.fetch_channel(int(welcome_channel_id))
        if not isinstance(ch, discord.TextChannel):
            raise RuntimeError("welcome_channel_id must point to a TextChannel.")
        guild_id = int(ch.guild.id)

        message: Optional[discord.Message] = None
        last_loaded_hash: Optional[str] = None
        last_synced_hash = get_welcome_message_file_hash(guild_id)
        last_payload: Optional[tuple[Optional[str], List[discord.Embed]]] = None
        last_error: Optional[str] = None
        presence_check_seconds = max(
            update_seconds,
            int(cfg.welcome_presence_check_seconds),
        )
        next_presence_check_at = 0.0

        while True:
            try:
                current_hash = file_sha256(message_path)

                if current_hash != last_loaded_hash or last_payload is None:
                    content, embeds = load_welcome_message_payload(message_path)
                    last_payload = (content, embeds)
                    last_loaded_hash = current_hash
                else:
                    content, embeds = last_payload

                now = time.monotonic()
                should_check_presence = message is None or now >= next_presence_check_at
                message_was_created = False

                if should_check_presence:
                    message, message_was_created = await ensure_welcome_message(
                        ch,
                        content,
                        embeds,
                        message,
                    )
                    next_presence_check_at = now + presence_check_seconds

                if message_was_created:
                    last_synced_hash = None

                if current_hash != last_synced_hash:
                    try:
                        if not message_was_created:
                            await edit_welcome_message_safely(message, content, embeds)
                    except discord.NotFound:
                        message, message_was_created = await get_or_create_welcome_message(
                            ch,
                            content,
                            embeds,
                        )
                        if not message_was_created:
                            await edit_welcome_message_safely(message, content, embeds)
                        next_presence_check_at = time.monotonic() + presence_check_seconds

                    set_welcome_message_file_hash(guild_id, current_hash)
                    last_synced_hash = current_hash
                    logger.info("synced welcome message", extra={"message_id": message.id, "channel_id": ch.id})

                if last_error is not None:
                    logger.info("welcome loop recovered")
                    last_error = None

            except discord.NotFound:
                message = None
                last_synced_hash = None
                next_presence_check_at = 0.0
                logger.info("welcome message was deleted; recreating on next iteration")
            except discord.Forbidden as e:
                logger.warning("forbidden syncing welcome message: %s", e)
            except discord.HTTPException as e:
                logger.warning("discord error syncing welcome message: %s", e)
            except Exception as e:
                error = repr(e)
                if error != last_error:
                    logger.exception("welcome loop iteration failed")
                last_error = error

            await asyncio.sleep(update_seconds)



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))
