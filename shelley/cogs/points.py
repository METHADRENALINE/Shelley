from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..discord_helpers import silent_defer
from ..security import require_administrator
from ..settings import get_config
from .leaderboard_sync import LeaderboardSync
from .points_state import PointsStore
from .text_points import TextPointsService
from .voice_points import VoicePointsService


logger = logging.getLogger(__name__)


class PointsCog(commands.Cog):
    points_group = app_commands.Group(name="points", description="Manage text and voice points.")
    textpoints_group = app_commands.Group(name="textpoints", description="Manage text points.")
    voicepoints_group = app_commands.Group(name="voicepoints", description="Manage voice points.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = PointsStore()
        self.leaderboard = LeaderboardSync(bot, self.store)
        self.text_points = TextPointsService(self.store, self.leaderboard.mark_dirty)
        self.voice_points = VoicePointsService(bot, self.store, self.leaderboard.mark_dirty)
        self.text_poll_task: asyncio.Task | None = None
        self.leaderboard_task: asyncio.Task | None = None
        self.voice_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self.voice_points.set_loop(asyncio.get_running_loop())
        self.text_poll_task = asyncio.create_task(self.text_poll_loop())
        self.leaderboard_task = asyncio.create_task(self.leaderboard_loop())
        self.voice_task = asyncio.create_task(self.voice_loop())

    async def cog_unload(self) -> None:
        for task in (self.text_poll_task, self.leaderboard_task, self.voice_task):
            if task:
                task.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.voice_points.set_loop(asyncio.get_running_loop())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self.text_points.award_for_message(message, get_config(), source="gateway")
        except Exception:
            logger.exception("message points award failed")

    async def text_poll_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                config = get_config()
                if config.points.enabled and config.points.text.enabled:
                    await self.text_points.poll_channels(self.bot, config)
                    sleep_seconds = max(2.0, float(config.points.text.poll_seconds))
                else:
                    sleep_seconds = 30.0
            except Exception:
                logger.exception("text points poll loop failed")
                sleep_seconds = 10.0
            await asyncio.sleep(sleep_seconds)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, _before: discord.VoiceState, after: discord.VoiceState) -> None:
        try:
            await self.voice_points.on_voice_state_update(member, after, get_config())
        except Exception:
            logger.exception("voice state update handling failed")

    def note_voice_speaking_threadsafe(self, member: discord.Member, speaking: bool) -> None:
        self.voice_points.note_speaking_threadsafe(member, speaking)

    def note_voice_speaking_state_threadsafe(self, member: discord.Member, state) -> None:
        self.voice_points.note_speaking_state_threadsafe(member, state)

    async def voice_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                config = get_config()
                self.voice_points.log_events = bool(config.points.log_events)
                if config.points.enabled and config.points.voice.enabled:
                    await self.voice_points.ensure_monitor(config)
                    await self.voice_points.award_due(config)
                    sleep_seconds = max(0.5, float(config.points.voice.check_seconds))
                else:
                    self.voice_points.clear_tracking()
                    sleep_seconds = 30.0
            except Exception:
                logger.exception("voice points loop failed")
                sleep_seconds = 10.0
            await asyncio.sleep(sleep_seconds)

    async def leaderboard_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                config = get_config()
                if config.points.enabled and config.points.leaderboard.enabled:
                    await self.leaderboard.sync(config)
                    sleep_seconds = max(2.0, float(config.points.leaderboard.update_seconds))
                else:
                    sleep_seconds = 30.0
            except Exception:
                logger.exception("leaderboard loop failed")
                sleep_seconds = 10.0
            await asyncio.sleep(sleep_seconds)

    async def _require_admin_guild(self, interaction: discord.Interaction) -> discord.Guild | None:
        if interaction.guild is None:
            await interaction.response.send_message("Эта команда работает только на сервере.", ephemeral=True)
            return None
        if not await require_administrator(interaction):
            return None
        return interaction.guild

    async def _reset(self, interaction: discord.Interaction, kind: str) -> None:
        guild = await self._require_admin_guild(interaction)
        if guild is None:
            return
        await silent_defer(interaction)
        changed = self.store.reset_points(int(guild.id), "all" if kind == "all" else kind)
        self.leaderboard.mark_dirty()
        await interaction.followup.send(f"Готово. Обновлено записей: {changed}.", ephemeral=True)

    async def _add(self, interaction: discord.Interaction, kind: str, amount: int, user: discord.Member) -> None:
        guild = await self._require_admin_guild(interaction)
        if guild is None:
            return
        await silent_defer(interaction)
        total = self.store.add_points(
            int(guild.id),
            int(user.id),
            "text" if kind == "text" else "voice",
            int(amount),
            str(user),
            str(user.display_name),
        )
        self.leaderboard.mark_dirty()
        await interaction.followup.send(f"Готово. Теперь у {user.mention}: {total}.", ephemeral=True)

    async def _remove(self, interaction: discord.Interaction, kind: str, amount: int, user: discord.Member) -> None:
        guild = await self._require_admin_guild(interaction)
        if guild is None:
            return
        await silent_defer(interaction)
        total = self.store.remove_points(
            int(guild.id),
            int(user.id),
            "text" if kind == "text" else "voice",
            int(amount),
            str(user),
            str(user.display_name),
        )
        self.leaderboard.mark_dirty()
        await interaction.followup.send(f"Готово. Теперь у {user.mention}: {total}.", ephemeral=True)

    @points_group.command(name="reset", description="Reset text and voice points on this server.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def points_reset(self, interaction: discord.Interaction) -> None:
        await self._reset(interaction, "all")

    @textpoints_group.command(name="reset", description="Reset text points on this server.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def textpoints_reset(self, interaction: discord.Interaction) -> None:
        await self._reset(interaction, "text")

    @textpoints_group.command(name="add", description="Add text points to a member.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def textpoints_add(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1], user: discord.Member) -> None:
        await self._add(interaction, "text", int(amount), user)

    @textpoints_group.command(name="remove", description="Remove text points from a member.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def textpoints_remove(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1], user: discord.Member) -> None:
        await self._remove(interaction, "text", int(amount), user)

    @voicepoints_group.command(name="reset", description="Reset voice points on this server.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def voicepoints_reset(self, interaction: discord.Interaction) -> None:
        await self._reset(interaction, "voice")

    @voicepoints_group.command(name="add", description="Add voice points to a member.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def voicepoints_add(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1], user: discord.Member) -> None:
        await self._add(interaction, "voice", int(amount), user)

    @voicepoints_group.command(name="remove", description="Remove voice points from a member.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def voicepoints_remove(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1], user: discord.Member) -> None:
        await self._remove(interaction, "voice", int(amount), user)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PointsCog(bot))
