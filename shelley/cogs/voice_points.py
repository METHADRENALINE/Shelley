from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Iterable

import discord

from ..config import BotConfig, PointsVoiceConfig
from .points_state import PointsStore
from .text_points import channel_is_counted, int_id_set

try:
    from discord.ext import voice_recv
except ImportError:
    voice_recv = None


logger = logging.getLogger(__name__)


def random_voice_points_amount(config: PointsVoiceConfig) -> int:
    minimum = int(config.award_min)
    maximum = int(config.award_max)
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    return random.randint(minimum, maximum)


def voice_member_is_points_eligible(member: discord.Member, voice_state=None) -> bool:
    voice_state = voice_state if voice_state is not None else getattr(member, "voice", None)
    if getattr(member, "bot", False) or voice_state is None:
        return False
    flags = ("deaf", "self_deaf", "mute", "self_mute")
    return not any(bool(getattr(voice_state, flag, False)) for flag in flags)


if voice_recv is not None:

    class PointsVoiceSink(voice_recv.AudioSink):
        def __init__(self, service: "VoicePointsService") -> None:
            super().__init__()
            self.service = service

        def wants_opus(self) -> bool:
            return True

        def write(self, _user, _data) -> None:
            return None

        def cleanup(self) -> None:
            return None

        @voice_recv.AudioSink.listener()
        def on_voice_member_speaking_start(self, member) -> None:
            self.service.note_speaking_threadsafe(member, True)

        @voice_recv.AudioSink.listener()
        def on_voice_member_speaking_stop(self, member) -> None:
            self.service.note_speaking_threadsafe(member, False)

        @voice_recv.AudioSink.listener()
        def on_voice_member_speaking_state(self, member, _ssrc, state) -> None:
            self.service.note_speaking_state_threadsafe(member, state)

else:
    PointsVoiceSink = None


class VoicePointsService:
    def __init__(self, bot: discord.Client, store: PointsStore, mark_dirty) -> None:
        self.bot = bot
        self.store = store
        self.mark_dirty = mark_dirty
        self.loop: asyncio.AbstractEventLoop | None = None
        self.started_at: dict[int, float] = {}
        self.active_seconds: dict[int, float] = {}
        self.warning_printed = False
        self.log_events = False

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    async def on_voice_state_update(self, member: discord.Member, after: discord.VoiceState, config: BotConfig) -> None:
        if member.bot:
            return
        voice_config = config.points.voice
        if not config.points.enabled or not voice_config.enabled:
            self.set_speaking_state(int(member.id), False)
            return
        include_ids = int_id_set(voice_config.channel_ids)
        exclude_ids = int_id_set(voice_config.excluded_channel_ids)
        after_channel_id = int(after.channel.id) if after.channel is not None else 0
        if (
            not after_channel_id
            or not channel_is_counted(after_channel_id, include_ids, exclude_ids)
            or not voice_member_is_points_eligible(member, after)
        ):
            self.set_speaking_state(int(member.id), False)

    def note_speaking_threadsafe(self, member: discord.Member, speaking: bool) -> None:
        user_id = getattr(member, "id", None)
        if user_id is None or getattr(member, "bot", False):
            return
        if not voice_member_is_points_eligible(member):
            speaking = False
        loop = self.loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(self.set_speaking_state, int(user_id), bool(speaking))

    def note_speaking_state_threadsafe(self, member: discord.Member, state) -> None:
        value = getattr(state, "value", state)
        try:
            speaking = int(value) != 0
        except (TypeError, ValueError):
            speaking = bool(value)
        self.note_speaking_threadsafe(member, speaking)

    def set_speaking_state(self, user_id: int, speaking: bool) -> None:
        user_id = int(user_id)
        now = time.monotonic()
        if speaking:
            if user_id not in self.started_at:
                self.started_at[user_id] = now
                if self.log_events:
                    logger.info("voice speaking started", extra={"user_id": user_id})
            return
        started = self.started_at.pop(user_id, None)
        if started is None:
            return
        seconds = max(0.0, now - float(started))
        self.active_seconds[user_id] = float(self.active_seconds.get(user_id, 0.0)) + seconds
        if self.log_events:
            logger.info("voice speaking stopped", extra={"user_id": user_id, "active_seconds": seconds})

    def clear_tracking(self) -> None:
        for user_id in list(self.started_at):
            self.set_speaking_state(user_id, False)
        self.started_at.clear()
        self.active_seconds.clear()

    async def ensure_monitor(self, config: BotConfig) -> None:
        voice_config = config.points.voice
        if voice_recv is None or PointsVoiceSink is None:
            if not self.warning_printed:
                logger.warning("discord-ext-voice-recv is not installed; voice points are disabled")
                self.warning_printed = True
            return
        channel_ids = [
            channel_id
            for channel_id in int_id_set(voice_config.channel_ids)
            if channel_id not in int_id_set(voice_config.excluded_channel_ids)
        ]
        if not channel_ids:
            return
        target_channel = None
        for channel_id in channel_ids:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    logger.exception("cannot fetch voice points channel", extra={"channel_id": channel_id})
                    continue
            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                target_channel = channel
                break
        if target_channel is None:
            return
        voice_client = discord.utils.get(self.bot.voice_clients, guild=target_channel.guild)
        if voice_client is not None and getattr(getattr(voice_client, "channel", None), "id", None) != target_channel.id:
            await voice_client.move_to(target_channel)
        if voice_client is None or not voice_client.is_connected():
            voice_client = await target_channel.connect(
                cls=voice_recv.VoiceRecvClient,
                self_mute=True,
                self_deaf=False,
            )
        is_listening = getattr(voice_client, "is_listening", None)
        if callable(is_listening) and is_listening():
            return
        listen = getattr(voice_client, "listen", None)
        if not callable(listen):
            logger.warning("current voice client does not support receive listening")
            return
        listen(PointsVoiceSink(self))
        logger.info("listening for voice activity", extra={"channel_id": int(target_channel.id)})

    def eligible_members(self, config: BotConfig) -> dict[int, discord.Member]:
        voice_config = config.points.voice
        include_ids = int_id_set(voice_config.channel_ids)
        exclude_ids = int_id_set(voice_config.excluded_channel_ids)
        members: dict[int, discord.Member] = {}
        for channel_id in include_ids:
            if channel_id in exclude_ids:
                continue
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                if not hasattr(channel, "members"):
                    continue
            if not hasattr(channel, "members"):
                continue
            eligible = [
                member
                for member in channel.members
                if voice_member_is_points_eligible(member)
            ]
            if len(eligible) < 2:
                continue
            for member in eligible:
                members[int(member.id)] = member
        return members

    async def award_due(self, config: BotConfig) -> None:
        voice_config = config.points.voice
        eligible_members = self.eligible_members(config)
        counted_member_ids = set(eligible_members)
        for user_id in list(self.started_at):
            if user_id not in counted_member_ids:
                self.set_speaking_state(user_id, False)
        for user_id in list(self.active_seconds):
            if user_id not in counted_member_ids:
                self.active_seconds.pop(user_id, None)
        if not counted_member_ids:
            self.active_seconds.clear()
            return
        now_wall = time.time()
        now_mono = time.monotonic()
        cooldown = max(1.0, float(voice_config.interval_seconds))
        minimum_speaking = max(0.1, float(voice_config.active_microphone_seconds))
        awarded = False
        for user_id in sorted(counted_member_ids):
            accumulated = float(self.active_seconds.get(user_id, 0.0))
            if user_id in self.started_at:
                accumulated += max(0.0, now_mono - float(self.started_at[user_id]))
            if accumulated < minimum_speaking:
                continue
            member = eligible_members.get(user_id)
            award = self.store.award(
                guild_id=int(member.guild.id) if member else config.runtime_guild_id(),
                user_id=user_id,
                kind="voice",
                amount=random_voice_points_amount(voice_config),
                now=now_wall,
                cooldown=cooldown,
                name=str(member) if member else None,
                display_name=str(getattr(member, "display_name", member)) if member else None,
            )
            if award is None:
                continue
            self.active_seconds[user_id] = 0.0
            if user_id in self.started_at:
                self.started_at[user_id] = now_mono
            awarded = True
            logger.info("awarded voice points", extra={"user_id": user_id, "amount": award.amount, "total": award.total})
        if awarded:
            self.mark_dirty()
