import asyncio
import logging
import time
from dataclasses import dataclass

import discord
from discord.ext import commands

from ..config import BotConfig
from ..discord_helpers import edit_welcome_message_safely, ensure_welcome_message, get_or_create_welcome_message
from ..renderers.welcome import load_welcome_message_payload
from ..settings import file_sha256, get_config
from ..state import get_welcome_message_file_hash, set_welcome_message_file_hash

logger = logging.getLogger(__name__)


@dataclass
class WelcomeState:
    message: discord.Message | None = None
    last_loaded_hash: str | None = None
    last_synced_hash: str | None = None
    last_payload: tuple[str | None, list[discord.Embed]] | None = None
    last_error: str | None = None
    next_presence_check_at: float = 0.0


async def fetch_welcome_channel(bot: commands.Bot, channel_id: int) -> discord.TextChannel:
    channel = await bot.fetch_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError("welcome_channel_id must point to a TextChannel.")
    return channel


def load_welcome_payload(path: str, state: WelcomeState) -> tuple[str, str | None, list[discord.Embed]]:
    current_hash = file_sha256(path)
    if current_hash != state.last_loaded_hash or state.last_payload is None:
        content, embeds = load_welcome_message_payload(path)
        state.last_payload = (content, embeds)
        state.last_loaded_hash = current_hash
        return current_hash, content, embeds
    content, embeds = state.last_payload
    return current_hash, content, embeds


async def refresh_welcome_presence(
    channel: discord.TextChannel,
    content: str | None,
    embeds: list[discord.Embed],
    state: WelcomeState,
    presence_check_seconds: int,
) -> bool:
    now = time.monotonic()
    if state.message is not None and now < state.next_presence_check_at:
        return False
    state.message, message_was_created = await ensure_welcome_message(channel, content, embeds, state.message)
    state.next_presence_check_at = now + presence_check_seconds
    if message_was_created:
        state.last_synced_hash = None
    return message_was_created


async def publish_welcome_message(
    channel: discord.TextChannel,
    guild_id: int,
    current_hash: str,
    content: str | None,
    embeds: list[discord.Embed],
    state: WelcomeState,
    message_was_created: bool,
    presence_check_seconds: int,
) -> None:
    if current_hash == state.last_synced_hash:
        return
    try:
        if state.message is None:
            state.message, message_was_created = await get_or_create_welcome_message(channel, content, embeds)
        if not message_was_created:
            await edit_welcome_message_safely(state.message, content, embeds)
    except discord.NotFound:
        state.message, message_was_created = await get_or_create_welcome_message(channel, content, embeds)
        if not message_was_created:
            await edit_welcome_message_safely(state.message, content, embeds)
        state.next_presence_check_at = time.monotonic() + presence_check_seconds

    set_welcome_message_file_hash(guild_id, current_hash)
    state.last_synced_hash = current_hash
    if state.message is not None:
        logger.info("synced welcome message", extra={"message_id": state.message.id, "channel_id": channel.id})


async def sync_welcome_iteration(
    channel: discord.TextChannel,
    guild_id: int,
    message_path: str,
    state: WelcomeState,
    presence_check_seconds: int,
) -> None:
    current_hash, content, embeds = load_welcome_payload(message_path, state)
    message_was_created = await refresh_welcome_presence(channel, content, embeds, state, presence_check_seconds)
    await publish_welcome_message(channel, guild_id, current_hash, content, embeds, state, message_was_created, presence_check_seconds)
    if state.last_error is not None:
        logger.info("welcome loop recovered")
        state.last_error = None


def handle_welcome_not_found(state: WelcomeState) -> None:
    state.message = None
    state.last_synced_hash = None
    state.next_presence_check_at = 0.0
    logger.info("welcome message was deleted; recreating on next iteration")


def handle_welcome_error(error: Exception, state: WelcomeState) -> None:
    current_error = repr(error)
    if current_error != state.last_error:
        logger.exception("welcome loop iteration failed")
    state.last_error = current_error


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
        cfg: BotConfig = get_config()

        welcome_channel_id = cfg.welcome_channel_id
        if not welcome_channel_id:
            logger.info("welcome_channel_id is not configured; welcome loop disabled")
            return

        update_seconds = int(cfg.welcome_update_seconds or cfg.update_seconds)
        presence_check_seconds = max(update_seconds, int(cfg.welcome_presence_check_seconds))
        channel = await fetch_welcome_channel(self.bot, int(welcome_channel_id))
        guild_id = int(channel.guild.id)
        state = WelcomeState(last_synced_hash=get_welcome_message_file_hash(guild_id))

        while True:
            try:
                await sync_welcome_iteration(channel, guild_id, cfg.welcome_message_path, state, presence_check_seconds)
            except discord.NotFound:
                handle_welcome_not_found(state)
            except discord.Forbidden as e:
                logger.warning("forbidden syncing welcome message: %s", e)
            except discord.HTTPException as e:
                logger.warning("discord error syncing welcome message: %s", e)
            except Exception as e:
                handle_welcome_error(e, state)

            await asyncio.sleep(update_seconds)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))
