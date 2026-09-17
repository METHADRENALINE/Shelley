from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import discord
from discord import app_commands
from discord.ext import commands

from ..actions import run_remote_action
from ..security import require_administrator
from ..settings import get_config

logger = logging.getLogger(__name__)

DISCORD_FILES_PER_MESSAGE = 10
DISCORD_EMBEDS_PER_MESSAGE = 10
DISCORD_EMBED_CHARACTERS_PER_MESSAGE = 6000
NOTIFY_SESSION_TTL_SECONDS = 1800
NOTIFY_EMOJI_SHORTCODE_PATTERN = re.compile(r"(?<![\w\\]):([A-Za-z0-9_]{2,32}):(?!\w)")
DISCORD_MARKUP_PATTERN = re.compile(r"<[^<>\n]+>")
MAX_DISCORD_SNOWFLAKE = (1 << 64) - 1


class NotifyMessageError(RuntimeError):
    pass


@dataclass
class NotifyAttachment:
    path: Path
    filename: str
    spoiler: bool = False
    description: str | None = None

    def to_file(self) -> discord.File:
        return discord.File(
            str(self.path),
            filename=self.filename,
            spoiler=self.spoiler,
            description=self.description,
        )


@dataclass
class NotifyEmbed:
    data: dict[str, object]

    def to_embed(self) -> discord.Embed:
        return discord.Embed.from_dict(deepcopy(self.data))


@dataclass
class NotifySession:
    guild_id: int
    user_id: int
    channel_id: int
    target_channel_id: int
    content: str
    temp_dir: Path
    message_id: int | None = None
    created_at: float = field(default_factory=time.monotonic)
    attachments: list[NotifyAttachment] = field(default_factory=list)
    embeds: list[NotifyEmbed] = field(default_factory=list)

    def is_expired(self) -> bool:
        return time.monotonic() - self.created_at > NOTIFY_SESSION_TTL_SECONDS

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def touch(self) -> None:
        self.created_at = time.monotonic()


def notify_message_content(text: str) -> str:
    value = str(text).strip()
    return value if value else "\u200b"


def parse_notify_message_id(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    candidate = str(value).strip()
    if not candidate.isascii() or not candidate.isdecimal():
        raise ValueError("message_id must contain only digits")
    message_id = int(candidate)
    if message_id <= 0 or message_id > MAX_DISCORD_SNOWFLAKE:
        raise ValueError("message_id is outside the Discord snowflake range")
    return message_id


def resolve_notify_emojis(text: str, emojis: Iterable[object]) -> str:
    emoji_by_name: dict[str, str] = {}
    for emoji in emojis:
        name = str(getattr(emoji, "name", "") or "")
        emoji_id = getattr(emoji, "id", None)
        if not name or emoji_id is None or not bool(getattr(emoji, "available", True)):
            continue
        is_usable = getattr(emoji, "is_usable", None)
        if callable(is_usable) and not is_usable():
            continue
        emoji_by_name.setdefault(name, str(emoji))

    if not emoji_by_name:
        return text

    def replace_shortcodes(value: str) -> str:
        return NOTIFY_EMOJI_SHORTCODE_PATTERN.sub(
            lambda match: emoji_by_name.get(match.group(1), match.group(0)),
            value,
        )

    resolved: list[str] = []
    position = 0
    for match in DISCORD_MARKUP_PATTERN.finditer(text):
        resolved.append(replace_shortcodes(text[position : match.start()]))
        resolved.append(match.group(0))
        position = match.end()
    resolved.append(replace_shortcodes(text[position:]))
    return "".join(resolved)


def resolve_notify_channels(text: str, channels: Iterable[object]) -> str:
    mentions_by_name: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for channel in channels:
        name = str(getattr(channel, "name", "") or "")
        mention = str(getattr(channel, "mention", "") or "")
        if not name or not mention:
            continue
        key = name.casefold()
        mentions_by_name.setdefault(key, set()).add(mention)
        names.setdefault(key, name)

    unique = {key: next(iter(mentions)) for key, mentions in mentions_by_name.items() if len(mentions) == 1}
    if not unique:
        return text

    alternatives = "|".join(re.escape(names[key]) for key in sorted(unique, key=lambda item: len(names[item]), reverse=True))
    pattern = re.compile(
        rf"(?<![\w\\])#({alternatives})(?![\w-])",
        flags=re.IGNORECASE,
    )

    def replace_references(value: str) -> str:
        return pattern.sub(
            lambda match: unique.get(match.group(1).casefold(), match.group(0)),
            value,
        )

    resolved: list[str] = []
    position = 0
    for match in DISCORD_MARKUP_PATTERN.finditer(text):
        resolved.append(replace_references(text[position : match.start()]))
        resolved.append(match.group(0))
        position = match.end()
    resolved.append(replace_references(text[position:]))
    return "".join(resolved)


def notify_attachment_batches(
    attachments: list[NotifyAttachment],
) -> list[list[NotifyAttachment]]:
    return [attachments[index : index + DISCORD_FILES_PER_MESSAGE] for index in range(0, len(attachments), DISCORD_FILES_PER_MESSAGE)]


def notify_embed_batches(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    characters = 0
    for embed in embeds:
        size = len(embed)
        if current and (len(current) >= DISCORD_EMBEDS_PER_MESSAGE or characters + size > DISCORD_EMBED_CHARACTERS_PER_MESSAGE):
            batches.append(current)
            current = []
            characters = 0
        current.append(embed)
        characters += size
    if current:
        batches.append(current)
    return batches


def parse_notify_embed_color(value: str) -> int | None:
    candidate = str(value).strip().removeprefix("#")
    if not candidate:
        return None
    if len(candidate) != 6 or any(character not in "0123456789abcdefABCDEF" for character in candidate):
        raise ValueError("Embed color must use the #RRGGBB format.")
    return int(candidate, 16)


def create_notify_embed(title: str, description: str, color: str) -> NotifyEmbed:
    clean_title = str(title).strip()
    clean_description = str(description).strip()
    if not clean_title and not clean_description:
        raise ValueError("Embed title or description is required.")
    embed = discord.Embed(
        title=clean_title or None,
        description=clean_description or None,
        color=parse_notify_embed_color(color),
    )
    if len(embed) > 6000:
        raise ValueError("Embed content exceeds Discord's 6000 character limit.")
    return NotifyEmbed(cast(dict[str, object], embed.to_dict()))


def safe_attachment_filename(filename: str) -> str:
    name = Path(str(filename)).name.strip()
    return name or "attachment"


def notify_session_text(session: NotifySession) -> str:
    embed_count = len(session.embeds)
    if session.message_id is not None:
        return f"Notification {session.message_id} is ready for editing.\nEmbeds: {embed_count}\nUse Preview before Publish."
    count = len(session.attachments)
    plural = "" if count == 1 else "s"
    return f"Notification draft is ready.\nEmbeds: {embed_count}\nAttached file{plural}: {count}\nUse Preview before Publish."


class NotifyModal(discord.ui.Modal):
    def __init__(self, cog: AdminCog, target_channel_id: int) -> None:
        super().__init__(title="Notify")
        self.cog = cog
        self.target_channel_id = target_channel_id
        self.message_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=2000,
        )
        self.file_upload: discord.ui.FileUpload = discord.ui.FileUpload(
            required=False,
            min_values=0,
            max_values=DISCORD_FILES_PER_MESSAGE,
        )
        self.add_item(self.message_input)
        self.add_item(
            discord.ui.Label(
                text="Files",
                description="Optional, up to 10 files here. Add more later if needed.",
                component=self.file_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        session = await self.cog.create_notify_session(
            interaction,
            notify_message_content(str(self.message_input.value)),
            self.target_channel_id,
        )
        if session is None:
            return
        await self.cog.store_notify_attachments(session, self.file_upload.values)
        await self.cog.send_notify_session_response(interaction, session)


class NotifyFilesModal(discord.ui.Modal):
    def __init__(self, cog: AdminCog, key: tuple[int, int, int]) -> None:
        super().__init__(title="Add notify files")
        self.cog = cog
        self.key = key
        self.file_upload: discord.ui.FileUpload = discord.ui.FileUpload(
            required=True,
            min_values=1,
            max_values=DISCORD_FILES_PER_MESSAGE,
        )
        self.add_item(
            discord.ui.Label(
                text="Files",
                description="Upload up to 10 more files.",
                component=self.file_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None or session.is_expired():
            self.cog.close_notify_session(self.key)
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        await self.cog.store_notify_attachments(session, self.file_upload.values)
        session.touch()
        await self.cog.send_notify_session_response(interaction, session)


class NotifyEditModal(discord.ui.Modal):
    def __init__(
        self,
        cog: AdminCog,
        key: tuple[int, int, int],
        content: str,
    ) -> None:
        super().__init__(title="Edit notify message")
        self.cog = cog
        self.key = key
        self.message_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            default="" if content == "\u200b" else content,
            required=True,
            max_length=2000,
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None or session.is_expired():
            self.cog.close_notify_session(self.key)
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        session.content = notify_message_content(str(self.message_input.value))
        session.touch()
        await self.cog.send_notify_session_response(interaction, session)


class NotifyEmbedModal(discord.ui.Modal):
    def __init__(self, cog: AdminCog, key: tuple[int, int, int], index: int | None = None) -> None:
        super().__init__(title="Edit notify embed" if index is not None else "Add notify embed")
        self.cog = cog
        self.key = key
        self.index = index
        existing = self.existing_embed()
        self.title_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Title",
            default=str(existing.get("title", "")),
            required=False,
            max_length=256,
        )
        self.description_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=str(existing.get("description", "")),
            required=False,
            max_length=4000,
        )
        color = existing.get("color")
        self.color_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Color",
            placeholder="#5865F2",
            default=f"#{int(color):06X}" if isinstance(color, int) else "",
            required=False,
            max_length=7,
        )
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.color_input)

    def existing_embed(self) -> dict[str, object]:
        if self.index is None:
            return {}
        session = self.cog.notify_sessions.get(self.key)
        if session is None or self.index >= len(session.embeds):
            return {}
        return session.embeds[self.index].data

    async def on_submit(self, interaction: discord.Interaction) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None or session.is_expired():
            self.cog.close_notify_session(self.key)
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        try:
            embed = create_notify_embed(
                str(self.title_input.value),
                str(self.description_input.value),
                str(self.color_input.value),
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        if self.index is None:
            if session.message_id is not None and len(session.embeds) >= DISCORD_EMBEDS_PER_MESSAGE:
                await interaction.response.send_message("An existing Discord message can contain at most 10 embeds.", ephemeral=True)
                return
            session.embeds.append(embed)
        elif self.index < len(session.embeds):
            session.embeds[self.index] = embed
        else:
            await interaction.response.send_message("That embed no longer exists.", ephemeral=True)
            return
        session.touch()
        await self.cog.send_notify_session_response(interaction, session)


class NotifySessionView(discord.ui.View):
    def __init__(self, cog: AdminCog, key: tuple[int, int, int]) -> None:
        super().__init__(timeout=NOTIFY_SESSION_TTL_SECONDS)
        self.cog = cog
        self.key = key
        session = cog.notify_sessions.get(key)
        if session is not None and session.message_id is not None:
            self.remove_item(self.add_files)
            self.remove_item(self.clear_files)
        if session is None or not session.embeds:
            self.edit_last_embed.disabled = True
            self.remove_last_embed.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        session = self.cog.notify_sessions.get(self.key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return False
        if int(interaction.user.id) != session.user_id:
            await interaction.response.send_message("Only the author can use this notification draft.", ephemeral=True)
            return False
        return await require_administrator(interaction)

    async def on_timeout(self) -> None:
        self.cog.close_notify_session(self.key)

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success, row=0)
    async def publish(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.publish_notify_session(interaction, self.key)

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.secondary, row=0)
    async def preview(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.preview_notify_session(interaction, self.key)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.cog.close_notify_session(self.key)
        await interaction.response.edit_message(content="Notification draft cancelled.", view=None)

    @discord.ui.button(label="Edit message", style=discord.ButtonStyle.primary, row=1)
    async def edit_message(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        await interaction.response.send_modal(NotifyEditModal(self.cog, self.key, session.content))

    @discord.ui.button(label="Add embed", style=discord.ButtonStyle.primary, row=1)
    async def add_embed(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(NotifyEmbedModal(self.cog, self.key))

    @discord.ui.button(label="Edit last embed", style=discord.ButtonStyle.secondary, row=1)
    async def edit_last_embed(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None or not session.embeds:
            await interaction.response.send_message("This notification has no embeds.", ephemeral=True)
            return
        await interaction.response.send_modal(NotifyEmbedModal(self.cog, self.key, len(session.embeds) - 1))

    @discord.ui.button(label="Remove last embed", style=discord.ButtonStyle.secondary, row=1)
    async def remove_last_embed(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None or not session.embeds:
            await interaction.response.send_message("This notification has no embeds.", ephemeral=True)
            return
        session.embeds.pop()
        session.touch()
        await interaction.response.edit_message(content=notify_session_text(session), view=NotifySessionView(self.cog, self.key))

    @discord.ui.button(label="Add files", style=discord.ButtonStyle.primary, row=2)
    async def add_files(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        await interaction.response.send_modal(NotifyFilesModal(self.cog, self.key))

    @discord.ui.button(label="Clear files", style=discord.ButtonStyle.secondary, row=2)
    async def clear_files(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        self.cog.clear_notify_attachments(session)
        session.touch()
        await interaction.response.edit_message(content=notify_session_text(session), view=NotifySessionView(self.cog, self.key))


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.notify_sessions: dict[tuple[int, int, int], NotifySession] = {}

    @app_commands.command(name="notify", description="Create or edit a notification.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(message_id="Message ID to edit, or leave empty to create a notification")
    async def notify(
        self,
        interaction: discord.Interaction,
        message_id: str | None = None,
    ) -> None:
        if not await require_administrator(interaction):
            return

        try:
            cfg = get_config()
            notify_channel_id = int(cfg.notify_channel_id)
            parsed_message_id = parse_notify_message_id(message_id)
            if parsed_message_id is not None:
                await interaction.response.defer(ephemeral=True, thinking=True)
                await self.create_notify_edit_session(
                    interaction,
                    notify_channel_id,
                    parsed_message_id,
                )
                return
            await interaction.response.send_modal(NotifyModal(self, notify_channel_id))
        except ValueError as error:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=str(error), view=None)
            else:
                await interaction.response.send_message(str(error), ephemeral=True)
        except discord.HTTPException:
            logger.exception("notify command failed because of Discord API error")
            if not interaction.response.is_done():
                await interaction.response.send_message("Discord rejected the notify command.", ephemeral=True)
            else:
                await interaction.edit_original_response(content="Discord rejected the notify command.", view=None)
        except Exception:
            logger.exception("notify command failed")
            if not interaction.response.is_done():
                await interaction.response.send_message("Notify command failed.", ephemeral=True)
            else:
                await interaction.edit_original_response(content="Notify command failed.", view=None)

    async def create_notify_session(
        self,
        interaction: discord.Interaction,
        content: str,
        target_channel_id: int,
        message_id: int | None = None,
    ) -> NotifySession | None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("Notify can only be used inside a server channel.", ephemeral=True)
            return None

        key = self.notify_key(
            int(interaction.guild_id),
            int(interaction.user.id),
            int(interaction.channel_id),
        )
        self.close_notify_session(key)
        session = NotifySession(
            guild_id=int(interaction.guild_id),
            user_id=int(interaction.user.id),
            channel_id=int(interaction.channel_id),
            target_channel_id=int(target_channel_id),
            content=content,
            temp_dir=Path(tempfile.mkdtemp(prefix="shelley_notify_")),
            message_id=int(message_id) if message_id is not None else None,
        )
        self.notify_sessions[key] = session
        return session

    async def create_notify_edit_session(
        self,
        interaction: discord.Interaction,
        target_channel_id: int,
        message_id: int,
    ) -> None:
        try:
            channel = await self.fetch_notify_channel(target_channel_id)
            message = await self.fetch_editable_notify_message(channel, message_id)
            session = await self.create_notify_session(
                interaction,
                notify_message_content(message.content),
                target_channel_id,
                message_id,
            )
            if session is None:
                return
            session.embeds = [NotifyEmbed(cast(dict[str, object], embed.to_dict())) for embed in message.embeds]
            key = self.notify_key(
                session.guild_id,
                session.user_id,
                session.channel_id,
            )
            await interaction.edit_original_response(
                content=notify_session_text(session),
                view=NotifySessionView(self, key),
            )
        except discord.NotFound:
            await interaction.edit_original_response(
                content="Notification not found in the configured channel.",
                view=None,
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="Shelley cannot access that notification.",
                view=None,
            )
        except NotifyMessageError as error:
            await interaction.edit_original_response(content=str(error), view=None)

    async def send_notify_session_response(
        self,
        interaction: discord.Interaction,
        session: NotifySession,
    ) -> None:
        key = self.notify_key(session.guild_id, session.user_id, session.channel_id)
        content = notify_session_text(session)
        view = NotifySessionView(self, key)
        if getattr(interaction, "message", None) is not None:
            try:
                await interaction.response.edit_message(content=content, view=view)
                return
            except discord.HTTPException:
                logger.exception("failed to edit notify draft response")
        if interaction.response.is_done():
            await interaction.followup.send(content, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(content, view=view, ephemeral=True)

    async def store_notify_attachments(
        self,
        session: NotifySession,
        attachments: list[discord.Attachment],
    ) -> None:
        for attachment in attachments:
            await self.store_notify_attachment(session, attachment)

    async def store_notify_attachment(
        self,
        session: NotifySession,
        attachment: discord.Attachment,
    ) -> None:
        filename = safe_attachment_filename(attachment.filename)
        stored_name = f"{len(session.attachments) + 1:04d}_{filename}"
        path = session.temp_dir / stored_name
        await attachment.save(path)
        session.attachments.append(
            NotifyAttachment(
                path=path,
                filename=filename,
                spoiler=bool(getattr(attachment, "is_spoiler", lambda: False)()),
                description=getattr(attachment, "description", None),
            )
        )

    async def fetch_notify_channel(self, channel_id: int) -> discord.TextChannel:
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            channel = await self.bot.fetch_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            raise NotifyMessageError("notify_channel_id must point to a text channel.")
        return channel

    async def fetch_editable_notify_message(
        self,
        channel: discord.TextChannel,
        message_id: int,
    ) -> discord.Message:
        message = await channel.fetch_message(int(message_id))
        bot_user = self.bot.user
        if bot_user is None or int(message.author.id) != int(bot_user.id):
            raise NotifyMessageError("Only notifications sent by Shelley can be edited.")
        return message

    def resolve_notify_content(
        self,
        session: NotifySession,
        channel: discord.TextChannel,
    ) -> str:
        guild_channels = (*channel.guild.channels, *channel.guild.threads)
        content = resolve_notify_channels(session.content, guild_channels)
        return resolve_notify_emojis(
            content,
            (*channel.guild.emojis, *self.bot.emojis),
        )

    def resolve_notify_embeds(
        self,
        session: NotifySession,
        channel: discord.TextChannel,
    ) -> list[discord.Embed]:
        guild_channels = (*channel.guild.channels, *channel.guild.threads)
        emojis = (*channel.guild.emojis, *self.bot.emojis)

        def resolve(value: object) -> str:
            content = resolve_notify_channels(str(value), guild_channels)
            return resolve_notify_emojis(content, emojis)

        resolved: list[discord.Embed] = []
        for draft in session.embeds:
            data = deepcopy(draft.data)
            for key in ("title", "description"):
                if data.get(key):
                    data[key] = resolve(data[key])
            fields = data.get("fields")
            if isinstance(fields, list):
                for item in fields:
                    if not isinstance(item, dict):
                        continue
                    if item.get("name"):
                        item["name"] = resolve(item["name"])
                    if item.get("value"):
                        item["value"] = resolve(item["value"])
            for key in ("footer", "author"):
                item = data.get(key)
                text_key = "text" if key == "footer" else "name"
                if isinstance(item, dict) and item.get(text_key):
                    item[text_key] = resolve(item[text_key])
            resolved.append(discord.Embed.from_dict(data))
        return resolved

    async def edit_notify_message(
        self,
        channel: discord.TextChannel,
        session: NotifySession,
        content: str,
        embeds: list[discord.Embed],
    ) -> None:
        if session.message_id is None:
            raise NotifyMessageError("Notification message ID is missing.")
        if len(embeds) > DISCORD_EMBEDS_PER_MESSAGE:
            raise NotifyMessageError("An existing Discord message can contain at most 10 embeds.")
        message = await self.fetch_editable_notify_message(
            channel,
            session.message_id,
        )
        await message.edit(content=content, embeds=embeds)

    @staticmethod
    def notify_batches(
        content: str,
        embeds: list[discord.Embed],
        attachments: list[NotifyAttachment],
    ) -> list[tuple[str | None, list[discord.Embed], list[NotifyAttachment]]]:
        embed_batches = notify_embed_batches(embeds)
        attachment_batches = notify_attachment_batches(attachments)
        count = max(1, len(embed_batches), len(attachment_batches))
        return [
            (
                content if index == 0 else None,
                embed_batches[index] if index < len(embed_batches) else [],
                attachment_batches[index] if index < len(attachment_batches) else [],
            )
            for index in range(count)
        ]

    async def preview_notify_session(
        self,
        interaction: discord.Interaction,
        key: tuple[int, int, int],
    ) -> None:
        session = self.notify_sessions.get(key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        try:
            channel = await self.fetch_notify_channel(session.target_channel_id)
            batches = self.notify_batches(
                self.resolve_notify_content(session, channel),
                self.resolve_notify_embeds(session, channel),
                session.attachments,
            )
            for index, (content, embeds, attachments) in enumerate(batches):
                files = [attachment.to_file() for attachment in attachments]
                if index == 0:
                    await interaction.response.send_message(content=content, embeds=embeds, files=files, ephemeral=True)
                else:
                    await interaction.followup.send(content=content or "", embeds=embeds, files=files, ephemeral=True)
            session.touch()
        except discord.Forbidden:
            await self.send_notify_preview_error(interaction, "Shelley cannot access the configured notification channel.")
        except discord.HTTPException:
            logger.exception("notify preview failed because of Discord API error")
            await self.send_notify_preview_error(interaction, "Discord rejected the notification preview.")
        except Exception:
            logger.exception("notify preview failed")
            await self.send_notify_preview_error(interaction, "Notification preview failed.")

    @staticmethod
    async def send_notify_preview_error(interaction: discord.Interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def send_notify_publication(
        self,
        channel: discord.TextChannel,
        content: str,
        embeds: list[discord.Embed],
        attachments: list[NotifyAttachment],
    ) -> None:
        sent: list[discord.Message] = []
        try:
            for batch_content, batch_embeds, batch_attachments in self.notify_batches(content, embeds, attachments):
                files = [attachment.to_file() for attachment in batch_attachments]
                sent.append(await channel.send(content=batch_content, embeds=batch_embeds, files=files))
        except Exception:
            for message in reversed(sent):
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.exception("failed to roll back a partial notification")
            raise

    async def publish_notify_session(
        self,
        interaction: discord.Interaction,
        key: tuple[int, int, int],
    ) -> None:
        session = self.notify_sessions.get(key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            channel = await self.fetch_notify_channel(session.target_channel_id)
            content = self.resolve_notify_content(session, channel)
            embeds = self.resolve_notify_embeds(session, channel)
            if session.message_id is not None:
                await self.edit_notify_message(channel, session, content, embeds)
                self.close_notify_session(key)
                await interaction.edit_original_response(content="Notification updated.", view=None)
                return

            await self.send_notify_publication(channel, content, embeds, session.attachments)

            self.close_notify_session(key)
            await interaction.edit_original_response(content="Notification sent.", view=None)
        except discord.NotFound:
            await interaction.edit_original_response(
                content="Notification not found in the configured channel.",
                view=None,
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="Shelley cannot access that notification.",
                view=None,
            )
        except NotifyMessageError as error:
            await interaction.edit_original_response(content=str(error), view=None)
        except discord.HTTPException:
            logger.exception("notify publish failed because of Discord API error")
            await interaction.edit_original_response(content="Discord rejected the notification.", view=None)
        except Exception:
            logger.exception("notify publish failed")
            await interaction.edit_original_response(content="Notification failed.", view=None)

    def clear_notify_attachments(self, session: NotifySession) -> None:
        for attachment in session.attachments:
            try:
                attachment.path.unlink(missing_ok=True)
            except OSError:
                logger.exception("failed to remove notify attachment temp file")
        session.attachments.clear()

    def close_notify_session(self, key: tuple[int, int, int]) -> None:
        session = self.notify_sessions.pop(key, None)
        if session is not None:
            session.cleanup()

    @staticmethod
    def notify_key(guild_id: int, user_id: int, channel_id: int) -> tuple[int, int, int]:
        return int(guild_id), int(user_id), int(channel_id)

    @app_commands.command(name="reboot", description="Reboot a configured server machine.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(target="Target server, for example bm")
    async def reboot(self, interaction: discord.Interaction, target: str):
        await run_remote_action(interaction, target, "reboot_command", "reboot")

    @app_commands.command(name="start", description="Start a configured server.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(target="Target server, for example bm")
    async def start(self, interaction: discord.Interaction, target: str):
        await run_remote_action(interaction, target, "start_command", "start")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
