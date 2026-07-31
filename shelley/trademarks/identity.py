from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

import discord

logger = logging.getLogger(__name__)


class GuildMemberResolver:
    def __init__(self, max_concurrency: int = 4) -> None:
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._inflight: dict[tuple[int, int], asyncio.Task[bool]] = {}
        self._lock = asyncio.Lock()

    async def present_ids(
        self,
        guild: discord.Guild | None,
        user_ids: Iterable[int | None],
        *,
        known_present: Iterable[int] = (),
    ) -> frozenset[int]:
        if guild is None:
            return frozenset()
        requested = {int(user_id) for user_id in user_ids if user_id is not None}
        present = {int(user_id) for user_id in known_present} & requested
        missing: list[int] = []
        for user_id in sorted(requested - present):
            if guild.get_member(user_id) is not None:
                present.add(user_id)
            else:
                missing.append(user_id)
        if missing:
            checks = await asyncio.gather(*(self._is_present(guild, user_id) for user_id in missing))
            present.update(user_id for user_id, is_present in zip(missing, checks, strict=True) if is_present)
        return frozenset(present)

    async def _is_present(self, guild: discord.Guild, user_id: int) -> bool:
        key = int(guild.id), int(user_id)
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._fetch_member(guild, user_id))
                self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

    async def _fetch_member(self, guild: discord.Guild, user_id: int) -> bool:
        async with self._semaphore:
            try:
                await guild.fetch_member(int(user_id))
            except discord.NotFound:
                return False
            except discord.Forbidden:
                logger.warning(
                    "cannot verify trademark guild membership because access was denied",
                    extra={"guild_id": int(guild.id), "user_id": int(user_id)},
                )
                return False
            except discord.HTTPException:
                logger.exception(
                    "cannot verify trademark guild membership",
                    extra={"guild_id": int(guild.id), "user_id": int(user_id)},
                )
                return False
            except Exception:
                logger.exception(
                    "unexpected trademark guild membership verification failure",
                    extra={"guild_id": int(guild.id), "user_id": int(user_id)},
                )
                return False
            return True
