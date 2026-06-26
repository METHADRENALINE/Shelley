from typing import List, Optional

import discord

from .state import (
    get_status_message_id,
    get_welcome_message_id,
    set_status_message_id,
    set_welcome_message_id,
)

async def get_or_create_status_message(
    channel: discord.TextChannel,
    state_path: str,
    key: str,
    view: Optional[discord.ui.View] = None,
    use_legacy_fallback: bool = False,
) -> discord.Message:
    message_id = get_status_message_id(state_path, key, use_legacy_fallback=use_legacy_fallback)

    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            set_status_message_id(state_path, msg.id, key)
            return msg
        except Exception:
            pass

    msg = await channel.send("Initializing status...", view=view)
    set_status_message_id(state_path, msg.id, key)
    return msg

async def recreate_status_message(
    channel: discord.TextChannel,
    state_path: str,
    key: str,
    view: Optional[discord.ui.View] = None,
) -> discord.Message:
    msg = await channel.send("Initializing status...", view=view)
    set_status_message_id(state_path, msg.id, key)
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
    state_path: str,
    content: Optional[str],
    embeds: List[discord.Embed],
) -> tuple[discord.Message, bool]:
    message_id = get_welcome_message_id(state_path)

    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            set_welcome_message_id(state_path, msg.id)
            return msg, False
        except discord.NotFound:
            pass

    msg = await channel.send(
        content=content,
        embeds=embeds,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    set_welcome_message_id(state_path, msg.id)
    return msg, True

async def ensure_welcome_message(
    channel: discord.TextChannel,
    state_path: str,
    content: Optional[str],
    embeds: List[discord.Embed],
    cached_message: Optional[discord.Message],
) -> tuple[discord.Message, bool]:
    if cached_message is not None:
        try:
            msg = await channel.fetch_message(cached_message.id)
            set_welcome_message_id(state_path, msg.id)
            return msg, False
        except discord.NotFound:
            pass

    return await get_or_create_welcome_message(channel, state_path, content, embeds)

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
    except Exception:
        pass
    try:
        await interaction.edit_original_response(content="\u200b")
    except Exception:
        pass
