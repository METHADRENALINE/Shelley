from typing import List, Optional
import logging

import discord

from .state import (
    get_status_message_id,
    get_welcome_message_id,
    set_status_message_id,
    set_welcome_message_id,
)

logger = logging.getLogger(__name__)


async def get_or_create_status_message(
    channel: discord.TextChannel,
    key: str,
    view: Optional[discord.ui.View] = None,
    use_legacy_fallback: bool = False,
) -> discord.Message:
    guild_id = int(channel.guild.id)
    message_id = get_status_message_id(guild_id, key, use_legacy_fallback=use_legacy_fallback)

    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            set_status_message_id(guild_id, msg.id, key)
            return msg
        except discord.NotFound:
            logger.info("status message not found", extra={"message_id": message_id, "key": key})
        except discord.Forbidden:
            logger.exception("cannot fetch status message because of missing permissions")
        except discord.HTTPException:
            logger.exception("cannot fetch status message from Discord")

    msg = await channel.send("Initializing status...", view=view)
    set_status_message_id(guild_id, msg.id, key)
    return msg

async def recreate_status_message(
    channel: discord.TextChannel,
    key: str,
    view: Optional[discord.ui.View] = None,
) -> discord.Message:
    msg = await channel.send("Initializing status...", view=view)
    set_status_message_id(int(channel.guild.id), msg.id, key)
    return msg

async def edit_status_message(
    msg: discord.Message,
    content: Optional[str],
    embeds: List[discord.Embed],
    view: Optional[discord.ui.View] = None,
) -> None:
    await msg.edit(
        content=content,
        embeds=embeds,
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )

async def get_or_create_welcome_message(
    channel: discord.TextChannel,
    content: Optional[str],
    embeds: List[discord.Embed],
) -> tuple[discord.Message, bool]:
    guild_id = int(channel.guild.id)
    message_id = get_welcome_message_id(guild_id)

    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            set_welcome_message_id(guild_id, msg.id)
            return msg, False
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.exception("cannot fetch welcome message because of missing permissions")
        except discord.HTTPException:
            logger.exception("cannot fetch welcome message from Discord")

    msg = await channel.send(
        content=content,
        embeds=embeds,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    set_welcome_message_id(guild_id, msg.id)
    return msg, True

async def ensure_welcome_message(
    channel: discord.TextChannel,
    content: Optional[str],
    embeds: List[discord.Embed],
    cached_message: Optional[discord.Message],
) -> tuple[discord.Message, bool]:
    if cached_message is not None:
        try:
            msg = await channel.fetch_message(cached_message.id)
            set_welcome_message_id(int(channel.guild.id), msg.id)
            return msg, False
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.exception("cannot fetch cached welcome message because of missing permissions")
        except discord.HTTPException:
            logger.exception("cannot fetch cached welcome message from Discord")

    return await get_or_create_welcome_message(channel, content, embeds)

async def edit_welcome_message_safely(
    msg: discord.Message,
    content: Optional[str],
    embeds: List[discord.Embed],
) -> None:
    await msg.edit(
        content=content,
        embeds=embeds,
        allowed_mentions=discord.AllowedMentions.none(),
    )

async def silent_defer(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=False)

async def silent_cleanup(interaction: discord.Interaction) -> None:
    try:
        await interaction.delete_original_response()
        return
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        await interaction.edit_original_response(content="\u200b")
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
