from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..db import get_database
from ..security import require_administrator
from ..settings import get_config
from .admin_views import AdminSession, private_message
from .repository import SeasonRepository
from .service import MeowService

logger = logging.getLogger(__name__)


class MeowCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        config = get_config()
        self.service = MeowService(bot, config, SeasonRepository(get_database(config)))
        self.task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self.task = asyncio.create_task(self.service.run(), name="meow")

    async def cog_unload(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    @app_commands.command(name="meow", description="Manage seasonal activity rewards.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def meow(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.service.guild_id:
            await private_message(interaction, "Meow is not configured on this server.")
            return
        if not await require_administrator(interaction):
            return
        await AdminSession(self.service, interaction).open()

    @meow.error
    async def command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        logger.error("Meow command failed", exc_info=error)
        await private_message(interaction, "Shelley could not open Meow. Try again shortly.")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id == self.service.guild_id and payload.channel_id == self.service.showcase.channel_id:
            await asyncio.to_thread(
                self.service.repository.invalidate_showcase,
                self.service.guild_id,
                payload.message_id,
            )
            self.service.wake()

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if role.guild.id == self.service.guild_id:
            await asyncio.to_thread(self.service.repository.invalidate_role, role.guild.id, role.id)
            self.service.wake()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.service.restore_online_members()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id == self.service.guild_id and not member.bot:
            self.service.refresh_member(member.id, joined_at=member.joined_at)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.guild.id != self.service.guild_id or after.bot:
            return
        if before.pending and not after.pending:
            self.service.refresh_member(after.id, joined_at=after.joined_at)
        elif self.service.roles.is_blocked(before) != self.service.roles.is_blocked(after):
            self.service.refresh_member(after.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MeowCog(bot))
