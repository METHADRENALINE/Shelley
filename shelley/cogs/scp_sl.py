from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands

from ..discord_helpers import edit_status_message
from ..scp_sl.client import ScpSlApiError, ScpSlClient, SnapshotCache
from ..scp_sl.protocol import ModeId, Snapshot, Vote
from ..scp_sl.renderer import payload_signature, render_status
from ..settings import get_config
from ..state import get_status_message_id, set_status_message_id

if TYPE_CHECKING:
    from .status import StatusCog

logger = logging.getLogger(__name__)
STATUS_KEY = "scpsl"


class ModeButton(discord.ui.Button):
    def __init__(self, cog: ScpSlCog, snapshot: Snapshot, mode: ModeId, label: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"scpsl:{snapshot.boot_id}:{snapshot.poll.id}:{mode}",
            row=0,
        )
        self.cog = cog
        self.boot_id = snapshot.boot_id
        self.poll_id = snapshot.poll.id
        self.mode = mode

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_vote(interaction, self.boot_id, self.poll_id, self.mode)


class ModeView(discord.ui.View):
    def __init__(self, cog: ScpSlCog, snapshot: Snapshot) -> None:
        super().__init__(timeout=None)
        for mode in snapshot.available_modes:
            self.add_item(ModeButton(cog, snapshot, mode.id, mode.name_ru))


class ScpSlCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = get_config().scp_sl
        self.client = ScpSlClient(self.config)
        self.cache = SnapshotCache(self.config.stale_seconds)
        self.channel: discord.TextChannel | None = None
        self.message: discord.Message | None = None
        self._view: ModeView | None = None
        self._signature: str | None = None
        self._save_message_id = False
        self._tasks: list[asyncio.Task] = []
        self._changed = asyncio.Event()
        self._api_available: bool | None = None
        self._retired_messages: list[discord.Message] = []
        self._retired_retry_at = 0.0

    async def cog_load(self) -> None:
        if not self.config.enabled:
            return
        await self.client.start()
        self._tasks = [asyncio.create_task(self.poll_loop()), asyncio.create_task(self.publish_loop())]

    async def cog_unload(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self._view is not None:
            self._view.stop()
        await self.client.close()

    async def poll_once(self) -> None:
        try:
            self.cache.update(await self.client.snapshot())
        except ScpSlApiError:
            self.cache.fail()
            self._report_health(False, "API request failed")
        else:
            self._report_health(self.cache.current() is not None, "stale or non-advancing server heartbeat")
        self._changed.set()

    def _report_health(self, available: bool, reason: str) -> None:
        if self._api_available == available:
            return
        self._api_available = available
        if available:
            logger.info("SCP:SL API and server heartbeat available")
        else:
            logger.warning("SCP:SL status unavailable: %s", reason)

    async def poll_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.poll_once()
            except Exception as error:
                self.cache.fail()
                self._changed.set()
                logger.warning("SCP:SL poll failed (%s)", type(error).__name__)
            await asyncio.sleep(max(0, self.config.poll_seconds - (time.monotonic() - started)))

    async def publish_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            self._changed.clear()
            try:
                await self.publish_once()
            except Exception as error:
                logger.warning("SCP:SL Discord publication failed (%s)", type(error).__name__)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=self.config.poll_seconds)

    async def publish_once(self) -> None:
        status_cog = cast("StatusCog | None", self.bot.get_cog("StatusCog"))
        anchor_id = 0
        if status_cog is not None:
            if not status_cog.messages_ready.is_set():
                return
            anchor_id = max((message.id for message in status_cog.messages.values()), default=0)
        if self.channel is None:
            channel = await self.bot.fetch_channel(int(get_config().status_channel_id))
            if not isinstance(channel, discord.TextChannel):
                raise RuntimeError("status_channel_id must point to a TextChannel")
            self.channel = channel
        if self.message is None and not self._retired_messages:
            message_id = get_status_message_id(int(self.channel.guild.id), STATUS_KEY)
            if message_id:
                try:
                    self.message = await self.channel.fetch_message(message_id)
                except discord.NotFound:
                    pass
        if self.message is not None and self.message.id < anchor_id:
            self._retired_messages.append(self.message)
            self.message = None
            self._signature = None
        now = datetime.now(UTC)
        snapshot = self.cache.current(wall=now)
        embeds = render_status(self.config, snapshot, now)
        view = ModeView(self, snapshot) if snapshot is not None and snapshot.voting(now) else None
        signature = payload_signature(embeds, view, self.config.separator_content)
        if self.message is not None and self._signature == signature:
            if view is not None:
                view.stop()
            await self._remove_retired_messages()
            return
        self._signature = None
        if self._view is not None:
            self._view.stop()
            self._view = None
        try:
            if self.message is None:
                await self._create_message(embeds, view)
            else:
                try:
                    await edit_status_message(self.message, self.config.separator_content, embeds, view=view)
                except discord.NotFound:
                    await self._create_message(embeds, view)
            if self._save_message_id and self.message is not None:
                set_status_message_id(int(self.channel.guild.id), self.message.id, STATUS_KEY)
                self._save_message_id = False
        except Exception:
            if view is not None:
                view.stop()
            raise
        self._view = view
        self._signature = signature
        await self._remove_retired_messages()

    async def _remove_retired_messages(self) -> None:
        if self._save_message_id or time.monotonic() < self._retired_retry_at:
            return
        for message in tuple(self._retired_messages):
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as error:
                logger.warning("Could not remove previous SCP:SL status message (%s)", type(error).__name__)
                self._retired_retry_at = time.monotonic() + 30
                return
            self._retired_messages.remove(message)

    async def _create_message(self, embeds: list[discord.Embed], view: ModeView | None) -> None:
        if self.channel is None:
            raise RuntimeError("SCP:SL status channel is unavailable")
        if view is None:
            self.message = await self.channel.send(
                content=self.config.separator_content, embeds=embeds, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            self.message = await self.channel.send(
                content=self.config.separator_content, embeds=embeds, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        self._save_message_id = True

    async def _private_result(self, interaction: discord.Interaction, text: str | None = None) -> None:
        try:
            if text is None:
                await interaction.delete_original_response()
            else:
                await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def handle_vote(self, interaction: discord.Interaction, boot_id: str, poll_id: str | None, mode: ModeId) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            return
        member = interaction.user
        message = self.message
        channel = self.channel
        snapshot = self.cache.current()
        if (
            channel is None
            or message is None
            or not isinstance(member, discord.Member)
            or member.bot
            or interaction.guild_id != channel.guild.id
            or member.guild.id != channel.guild.id
            or interaction.channel_id != channel.id
            or interaction.message is None
            or interaction.message.id != message.id
            or self.bot.user is None
            or interaction.message.author.id != self.bot.user.id
            or snapshot is None
            or snapshot.boot_id != boot_id
            or snapshot.poll.id != poll_id
            or poll_id is None
            or not snapshot.voting(datetime.now(UTC))
            or mode not in {item.id for item in snapshot.available_modes}
        ):
            await self._private_result(interaction)
            return
        try:
            result = await self.client.vote(Vote(boot_id=boot_id, poll_id=poll_id, discord_user_id=str(member.id), mode=mode))
        except ScpSlApiError:
            self.cache.fail()
            self._report_health(False, "vote request failed")
            self._changed.set()
            await self._private_result(
                interaction, "\u0421\u0435\u0440\u0432\u0435\u0440 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d!"
            )
            return
        if result.result == "accepted":
            await self._private_result(interaction, "\u0413\u043e\u043b\u043e\u0441 \u043f\u0440\u0438\u043d\u044f\u0442!")
        else:
            await self._private_result(interaction)


async def setup(bot: commands.Bot) -> None:
    if get_config().scp_sl.enabled:
        await bot.add_cog(ScpSlCog(bot))
