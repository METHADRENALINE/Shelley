from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

import discord
from discord.ext import commands

from ..config import BotConfig
from .models import Draft, MeowError, Season
from .repository import SeasonRepository
from .roles import SeasonRoles
from .showcase import SeasonShowcase

logger = logging.getLogger(__name__)


class MeowService:
    def __init__(self, bot: commands.Bot, config: BotConfig, repository: SeasonRepository) -> None:
        self.bot = bot
        self.config = config
        self.guild_id = config.runtime_guild_id()
        self.repository = repository
        self.roles = SeasonRoles(repository, config.meow)
        self.showcase = SeasonShowcase(bot, repository, config.meow)
        self._wake = asyncio.Event()
        self._retry: dict[str, float] = {}
        self._ineligible_medal: dict[int, float] = {}
        self._members_to_refresh: dict[int, datetime | None] = {}
        self._restore_on_ready = True
        self.problems: dict[str, str] = {}

    def wake(self, *, retry: bool = False) -> None:
        if retry:
            self._retry.clear()
        self._wake.set()

    def refresh_member(self, user_id: int, *, joined_at: datetime | None = None) -> None:
        self._ineligible_medal.pop(user_id, None)
        self._members_to_refresh[user_id] = joined_at or self._members_to_refresh.get(user_id)
        self.wake()

    def restore_online_members(self) -> None:
        self._restore_on_ready = True
        self.wake(retry=True)

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            self._wake.clear()
            guild = self.bot.get_guild(self.guild_id)
            if guild is not None:
                try:
                    await self.reconcile(guild)
                    self.problems.pop("Storage", None)
                except Exception:
                    logger.exception("Meow season storage unavailable")
                    self.problems["Storage"] = "Could not read the season state."
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=6)
            except TimeoutError:
                pass

    async def preflight(self, guild: discord.Guild, draft: Draft) -> None:
        draft.design.ready()
        if not self.config.points.enabled or not getattr(self.config.points, draft.design.source).enabled:
            raise MeowError(f"{draft.design.source.title()} points are disabled.")
        self.roles.check_hierarchy(guild)
        await self.showcase.channel(guild)
        rendered = await self.showcase.render(draft.design, guild=guild, reveal_medal=True)
        rendered.close()

    async def _step(self, label: str, action: Callable[[], Awaitable[None]]) -> None:
        if time.monotonic() < self._retry.get(label, 0):
            return
        try:
            await action()
        except MeowError as exc:
            self.problems[label] = str(exc)
            self._retry[label] = time.monotonic() + 30
            logger.warning("Meow %s: %s", label.lower(), exc)
        except discord.HTTPException as exc:
            self.problems[label] = f"Discord rejected the request (HTTP {exc.status})."
            self._retry[label] = time.monotonic() + 30
            logger.warning("Meow %s failed (HTTP %s)", label.lower(), exc.status)
        except Exception:
            self.problems[label] = "An internal error occurred. Shelley will retry."
            self._retry[label] = time.monotonic() + 30
            logger.exception("Meow %s failed", label.lower())
        else:
            self.problems.pop(label, None)
            self._retry.pop(label, None)

    async def _prepare(self, guild: discord.Guild, season: Season) -> None:
        await self.showcase.channel(guild)
        rendered = await self.showcase.render(season.design, guild=guild, reveal_medal=True)
        rendered.close()
        await self.roles.ensure(guild, season)
        await asyncio.to_thread(self.repository.activate, guild.id, season.id)

    async def _apply_ranks(self, guild: discord.Guild, season: Season) -> None:
        if season.role_revision != season.revision:
            await self.roles.ensure(guild, season)
            dashboard = await asyncio.to_thread(self.repository.dashboard, guild.id)
            if dashboard.live is None or dashboard.live.id != season.id or dashboard.live.state != "active":
                return
            season = dashboard.live
        elif self.config.meow.ranks_above_role_id:
            await self.roles.place_ranks(guild, season, {role.id: role for role in guild.roles})
        await asyncio.to_thread(self.repository.sync_rank_targets, season)
        await self.roles.apply_pending(guild, season)

    async def _select_medal(self, guild: discord.Guild, season: Season) -> None:
        self.roles.check_hierarchy(guild)
        now = time.monotonic()
        self._ineligible_medal = {user_id: until for user_id, until in self._ineligible_medal.items() if until > now}
        candidates = await asyncio.to_thread(self.repository.medal_candidates, season, list(self._ineligible_medal))
        for user_id in candidates:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                self._ineligible_medal[user_id] = now + 120
                continue
            if member.bot or member.pending or self.roles.is_blocked(member):
                self._ineligible_medal[user_id] = now + 120
                continue
            await asyncio.to_thread(self.repository.claim_medal, season, user_id)
            return

    async def _queue_missed_joins(self, guild: discord.Guild, live: Season | None) -> None:
        medals = await asyncio.to_thread(self.repository.earned_medal_roles, guild.id)
        ranks = await asyncio.to_thread(self.repository.assigned_rank_ids, live.id) if live is not None and live.state == "active" else {}
        if (medals or ranks) and self.bot.intents.members and not guild.chunked:
            await asyncio.wait_for(guild.chunk(cache=True), timeout=15)
        for user_id in medals.keys() | ranks.keys():
            member = guild.get_member(user_id)
            if member is None:
                continue
            held = {role.id for role in member.roles}
            rank_id = ranks.get(user_id)
            missing_rank = rank_id is not None and rank_id not in held
            missing_medal = member.joined_at is not None and bool(medals.get(user_id, set()) - held)
            if missing_rank or missing_medal:
                self.refresh_member(user_id, joined_at=member.joined_at)
            elif rank_id is not None and self.roles.is_blocked(member):
                self.refresh_member(user_id)

    async def _refresh_member(self, guild: discord.Guild, live: Season | None, user_id: int, *, joined_at: datetime | None) -> None:
        active = live if live is not None and live.state == "active" else None
        medals = (
            await asyncio.to_thread(self.repository.earned_medal_roles, guild.id, user_id, joined_at=joined_at)
            if joined_at is not None
            else {}
        )
        if active is None and not medals:
            return
        if active is not None:
            await asyncio.to_thread(
                self.repository.refresh_member_rank,
                guild.id,
                user_id,
                rejoined=joined_at is not None,
            )
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return
        if member.bot or member.pending:
            return
        if active is not None and self.roles.is_blocked(member):
            await self.roles.remove_active_ranks(member, active)
            await asyncio.to_thread(self.repository.retry_rank_later, active.id, user_id)
        if user_id in medals and member.joined_at == joined_at:
            await self.roles.restore_medals(member, medals[user_id])

    async def _refresh_members(self, guild: discord.Guild, live: Season | None) -> None:
        if self._restore_on_ready:
            self._restore_on_ready = False
            try:
                await self._queue_missed_joins(guild, live)
            except Exception:
                self._restore_on_ready = True
                raise
        for user_id, joined_at in list(self._members_to_refresh.items())[:20]:
            self._members_to_refresh.pop(user_id, None)
            try:
                await self._refresh_member(guild, live, user_id, joined_at=joined_at)
            except Exception:
                self._members_to_refresh[user_id] = self._members_to_refresh.get(user_id) or joined_at
                raise

    async def _cleanup(self, guild: discord.Guild, retired: list[Season]) -> None:
        for season in retired:
            await self.roles.cleanup(guild, season)

    async def reconcile(self, guild: discord.Guild) -> None:
        dashboard = await asyncio.to_thread(self.repository.dashboard, guild.id)
        await self._step("Member rewards", lambda: self._refresh_members(guild, dashboard.live))
        live = dashboard.live
        if live is not None and live.state == "awarding":
            await self._step("Medal", lambda: self.roles.award_medal(guild, live))
            dashboard = await asyncio.to_thread(self.repository.dashboard, guild.id)
        else:
            self.problems.pop("Medal", None)
        pending = dashboard.preparing
        if pending is not None and (dashboard.live is None or dashboard.live.state != "awarding"):
            await self._step("Setup", lambda: self._prepare(guild, pending))
            dashboard = await asyncio.to_thread(self.repository.dashboard, guild.id)
        if dashboard.preparing is None:
            self.problems.pop("Setup", None)
        active = dashboard.live
        if active is not None and active.state == "active":
            await self._step("Eligibility", lambda: self._select_medal(guild, active))
            dashboard = await asyncio.to_thread(self.repository.dashboard, guild.id)
            eligible = dashboard.live
            if eligible is not None and eligible.state == "awarding":
                await self._step("Medal", lambda: self.roles.award_medal(guild, eligible))
                dashboard = await asyncio.to_thread(self.repository.dashboard, guild.id)
            elif eligible is not None and eligible.state == "active":
                await self._step("Ranks", lambda: self._apply_ranks(guild, eligible))
        else:
            self.problems.pop("Eligibility", None)
        if dashboard.live is None or dashboard.live.state != "active":
            self.problems.pop("Ranks", None)
        display = dashboard.shown
        retired = await asyncio.to_thread(self.repository.pending_cleanup, guild.id)
        if retired:
            await self._step("Cleanup", lambda: self._cleanup(guild, retired))
        else:
            self.problems.pop("Cleanup", None)
        await self._step("Showcase", lambda: self.showcase.sync(guild, display))
