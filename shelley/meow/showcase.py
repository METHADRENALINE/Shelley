from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from hashlib import blake2s
from io import BytesIO

import discord
from discord.ext import commands

from ..config import MeowConfig
from .art import CardRenderer
from .era_archive import archive_embed, archive_file
from .models import (
    CUSTOM_EMOJI,
    FINAL_STAGE,
    MEDAL_COLOR,
    ArchivedMedal,
    EraBoard,
    MeowError,
    PointSource,
    Season,
    SeasonDesign,
    era_title,
    number,
)
from .repository import SeasonRepository

NO_MENTIONS = discord.AllowedMentions.none()
SOURCE_RU = {
    "text": "\u0422\u0435\u043a\u0441\u0442\u043e\u0432\u044b\u0435 \u043e\u0447\u043a\u0438",
    "voice": "\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u044b\u0435 \u043e\u0447\u043a\u0438",
}
POINT_SYMBOLS = {"text": "\U0001f4ac", "voice": "\U0001f3a4"}
STATE_RU = {
    "preview": "\u041c\u0443\u0440\u043b\u044b\u043a\u0438",
    "active": "\u041c\u0443\u0440\u043b\u044b\u043a\u0438",
    "awarding": "\u041c\u0435\u0434\u0430\u043b\u044c \u0432\u044b\u0434\u0430\u0451\u0442\u0441\u044f",
    "finished": "\u0421\u0435\u0437\u043e\u043d \u0437\u0430\u0432\u0435\u0440\u0448\u0451\u043d",
    "cancelled": "\u0421\u0435\u0437\u043e\u043d \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d",
}


def safe_display(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def russian_points(source: PointSource) -> str:
    return SOURCE_RU[source]


def server_emoji(guild: discord.Guild | None, emoji_id: int) -> str | None:
    emoji = guild.get_emoji(emoji_id) if guild is not None and emoji_id else None
    return str(emoji) if emoji is not None and emoji.available else None


def season_icon(guild: discord.Guild | None, value: str | None) -> str:
    match = CUSTOM_EMOJI.fullmatch(value or "")
    return (server_emoji(guild, int(match[1])) or "") if match else ""


@dataclass
class RenderedShowcase:
    view: discord.ui.LayoutView
    files: list[discord.File]

    def close(self) -> None:
        for file in self.files:
            file.close()


class SeasonShowcase:
    def __init__(self, bot: commands.Bot, repository: SeasonRepository, config: MeowConfig) -> None:
        self.bot = bot
        self.repository = repository
        self.config = config
        self.channel_id = config.channel_id
        self.renderer = CardRenderer()
        self._message: discord.Message | None = None
        self._pending_message: tuple[int | None, int, int, discord.Message] | None = None
        self._archive_messages: dict[int, discord.Message] = {}
        self._pending_archives: dict[int, tuple[int, str, discord.Message]] = {}
        self._publish_lock = asyncio.Lock()

    async def channel(self, guild: discord.Guild) -> discord.TextChannel:
        channel: discord.abc.GuildChannel | discord.abc.PrivateChannel | discord.Thread | None
        channel = guild.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.channel_id)
            except discord.HTTPException as exc:
                raise MeowError("Shelley cannot access the configured showcase channel.") from exc
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise MeowError("The showcase must be a text channel on this server.")
        me = guild.me
        if me is None:
            raise MeowError("Shelley is still connecting. Try again shortly.")
        permissions = channel.permissions_for(me)
        if not all(
            (
                permissions.view_channel,
                permissions.send_messages,
                permissions.attach_files,
                permissions.read_message_history,
            )
        ):
            raise MeowError("Shelley needs channel access, Send Messages, Attach Files and Read Message History.")
        return channel

    async def render(
        self,
        design: SeasonDesign,
        season: Season | None = None,
        *,
        guild: discord.Guild | None = None,
    ) -> RenderedShowcase:
        state = season.state if season else "preview"
        point_id = self.config.text_points_emoji_id if design.source == "text" else self.config.voice_points_emoji_id
        point_icon = server_emoji(guild, point_id) or POINT_SYMBOLS[design.source]
        subtitle = f"{STATE_RU[state]}  \u00b7  {point_icon} {russian_points(design.source)}"
        if slot := design.era_slot:
            stage = (
                "\u0424\u0438\u043d\u0430\u043b\u044c\u043d\u044b\u0439 \u0430\u043a\u0442"
                if slot.stage == FINAL_STAGE
                else f"\u0421\u0435\u0437\u043e\u043d {slot.stage}"
            )
            subtitle += f"\n-# {era_title(slot.era)}  \u00b7  {stage}"
        if season and season.winner_id:
            subtitle += f"\n<@{season.winner_id}>"
        title = safe_display(design.name or "\u041d\u043e\u0432\u044b\u0439 \u0441\u0435\u0437\u043e\u043d")
        icon = season_icon(guild, design.emoji)
        header = f"## {icon + ' ' if icon else ''}{title}\n-# {subtitle}"
        if design.introduction:
            header += f"\n\n{design.introduction}"
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(header), accent_color=design.accent))
        images = await self.renderer.cards(design.rewards, design.source, design.accent, design.medal.key if design.medal else None)
        files: list[discord.File] = []
        for index, (reward, image) in enumerate(zip(design.rewards, images, strict=True), 1):
            filename = f"meow-{index}.png"
            alt = f"{reward.name} \u00b7 {number(reward.points)} \u00b7 {russian_points(design.source)}"
            role_id = season.roles.get(reward.key) if season else None
            role_line = f"-# <@&{role_id}>\n" if role_id and state in ("active", "awarding") else ""
            medal = design.medal is not None and reward.key == design.medal.key
            view.add_item(
                discord.ui.Container(
                    discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{filename}", description=alt)),
                    discord.ui.TextDisplay(f"{role_line}{reward.description}"),
                    accent_color=MEDAL_COLOR if medal else reward.color or design.accent,
                )
            )
            files.append(discord.File(BytesIO(image), filename=filename, description=alt))
        return RenderedShowcase(view, files)

    async def sync(self, guild: discord.Guild, season: Season | None) -> None:
        async with self._publish_lock:
            if season is None:
                await self._sync_era_boards(guild)
            else:
                await self._sync(guild, season)

    async def _sync(self, guild: discord.Guild, season: Season) -> None:
        saved = await asyncio.to_thread(self.repository.showcase, guild.id)
        archive_id = await self._sync_era_boards(guild)
        same_channel = saved.channel_id == self.channel_id
        archived_first = archive_id is None or (saved.message_id or 0) > archive_id
        current = (
            same_channel
            and saved.message_id is not None
            and saved.season_id == season.id
            and saved.revision == season.revision
            and archived_first
        )
        if current and saved.retired_message_id is None:
            return
        channel = await self.channel(guild)
        if same_channel and saved.retired_message_id is not None and saved.retired_message_id != saved.message_id:
            await self._remove_retired(channel, saved.retired_message_id)
        if current:
            return
        rendered = await self.render(season.design, season, guild=guild)
        try:
            message = await self._existing_message(channel, saved.message_id if saved.channel_id == channel.id else None)
            trial = await asyncio.to_thread(self.repository.trial_showcase, guild.id)
            if (
                message is None
                and trial.channel_id == channel.id
                and trial.message_id is not None
                and trial.message_id != saved.retired_message_id
            ):
                message = await self._fetch_owned(channel, trial.message_id)
            retired = None
            if message is not None and archive_id is not None and message.id < archive_id:
                retired = message.id
                message = None
            if message is not None:
                try:
                    message = await message.edit(
                        view=rendered.view,
                        attachments=rendered.files,
                        allowed_mentions=NO_MENTIONS,
                    )
                except discord.NotFound:
                    message = None
            pending = self._pending_message
            if message is None and pending and pending[:3] == (archive_id, season.id, season.revision):
                message = pending[3]
            if message is None:
                nonce = blake2s(
                    f"{channel.id}:{season.id}:{season.revision}:{archive_id}".encode(),
                    digest_size=10,
                ).hexdigest()
                message = await channel.send(
                    view=rendered.view,
                    files=rendered.files,
                    allowed_mentions=NO_MENTIONS,
                    silent=True,
                    nonce=nonce,
                )
                self._pending_message = (archive_id, season.id, season.revision, message)
            old_id = saved.message_id
            if (
                retired is None
                and same_channel
                and old_id is not None
                and old_id != message.id
                and archive_id is not None
                and old_id < archive_id
            ):
                retired = old_id
            self._message = message
            await asyncio.to_thread(
                self.repository.save_showcase,
                guild.id,
                channel.id,
                message.id,
                season.id,
                season.revision,
                retired_message_id=retired,
            )
            self._pending_message = None
            if retired is not None:
                await self._remove_retired(channel, retired)
        finally:
            rendered.close()

    async def post_draft(self, guild: discord.Guild, design: SeasonDesign) -> discord.Message:
        async with self._publish_lock:
            archive_id = await self._sync_era_boards(guild)
            channel = await self.channel(guild)
            saved = await asyncio.to_thread(self.repository.showcase, guild.id)
            if saved.season_id is not None:
                raise MeowError("A season is already published. Use the private preview.")
            trial = await asyncio.to_thread(self.repository.trial_showcase, guild.id)
            message = None
            if trial.channel_id == channel.id and trial.message_id:
                message = await self._fetch_owned(channel, trial.message_id)
            retired = message if message is not None and archive_id is not None and message.id < archive_id else None
            if retired is not None:
                message = None
            rendered = await self.render(design, guild=guild)
            try:
                if message is not None:
                    message = await message.edit(view=rendered.view, attachments=rendered.files, allowed_mentions=NO_MENTIONS)
                else:
                    message = await channel.send(
                        view=rendered.view,
                        files=rendered.files,
                        allowed_mentions=NO_MENTIONS,
                        silent=True,
                    )
                await asyncio.to_thread(self.repository.save_trial_showcase, guild.id, channel.id, message.id)
                if retired is not None:
                    try:
                        await retired.delete()
                    except discord.NotFound:
                        pass
                return message
            finally:
                rendered.close()

    async def _sync_era_boards(self, guild: discord.Guild) -> int | None:
        results = await asyncio.to_thread(self.repository.era_results, guild.id)
        if not results:
            return None
        saved = await asyncio.to_thread(self.repository.era_boards, guild.id)
        channel = await self.channel(guild)
        latest = None
        for era, rows in results.items():
            message_id = await self._sync_era_board(channel, era, rows, saved.get(era))
            latest = max(latest or 0, message_id)
        return latest

    async def _sync_era_board(
        self,
        channel: discord.TextChannel,
        era: int,
        rows: list[ArchivedMedal],
        saved: EraBoard | None,
    ) -> int:
        payload = json.dumps([asdict(row) for row in rows], sort_keys=True)
        signature = blake2s(payload.encode()).hexdigest()
        message = self._archive_messages.get(era)
        if saved is not None and saved.channel_id == channel.id and saved.message_id is not None:
            if message is None or message.id != saved.message_id or message.channel.id != channel.id:
                message = await self._fetch_owned(channel, saved.message_id)
            if message is not None and saved.signature == signature:
                self._archive_messages[era] = message
                return message.id
            if message is not None and saved.signature is None and era == 1 and len(rows) == 6:
                await asyncio.to_thread(
                    self.repository.save_era_board,
                    channel.guild.id,
                    era,
                    channel.id,
                    message.id,
                    signature,
                )
                self._archive_messages[era] = message
                return message.id
        else:
            message = None
        pending = self._pending_archives.get(era)
        if message is None and pending is not None and pending[0] == channel.id:
            message = pending[2]
            if pending[1] == signature:
                await self._remember_era_board(channel, era, message, signature)
                return message.id
        file = await archive_file(rows, era=era)
        embed = archive_embed(era)
        try:
            if message is not None:
                try:
                    message = await message.edit(embed=embed, attachments=[file], allowed_mentions=NO_MENTIONS)
                except discord.NotFound:
                    message = None
            if message is None:
                nonce = blake2s(f"meow-era:{channel.id}:{era}".encode(), digest_size=10)
                message = await channel.send(
                    embed=embed,
                    file=file,
                    allowed_mentions=NO_MENTIONS,
                    silent=True,
                    nonce=nonce.hexdigest(),
                )
            self._pending_archives[era] = (channel.id, signature, message)
            await self._remember_era_board(channel, era, message, signature)
            return message.id
        finally:
            file.close()

    async def _remember_era_board(self, channel: discord.TextChannel, era: int, message: discord.Message, signature: str) -> None:
        await asyncio.to_thread(self.repository.save_era_board, channel.guild.id, era, channel.id, message.id, signature)
        self._archive_messages[era] = message
        self._pending_archives.pop(era, None)

    async def _remove_retired(self, channel: discord.TextChannel, message_id: int) -> None:
        message = await self._fetch_owned(channel, message_id)
        if message is not None:
            try:
                await message.delete()
            except discord.NotFound:
                pass
        await asyncio.to_thread(self.repository.clear_retired_showcase, channel.guild.id, message_id)

    async def _existing_message(self, channel: discord.TextChannel, message_id: int | None) -> discord.Message | None:
        if self._message is not None and self._message.channel.id == channel.id and self._message.id == message_id:
            return self._message
        if message_id is None:
            return None
        return await self._fetch_owned(channel, message_id)

    async def _fetch_owned(self, channel: discord.TextChannel, message_id: int) -> discord.Message | None:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        if self.bot.user is None or message.author.id != self.bot.user.id:
            raise MeowError("The saved showcase message is not owned by Shelley.")
        return message
