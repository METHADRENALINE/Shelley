from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from dataclasses import replace

import discord

from ..config import MeowConfig
from .models import MEDAL_COLOR, MeowError, Reward, Season
from .repository import SeasonRepository

logger = logging.getLogger(__name__)


class SeasonRoles:
    def __init__(self, repository: SeasonRepository, config: MeowConfig | None = None) -> None:
        self.repository = repository
        self.config = config or MeowConfig()

    @property
    def protected_ids(self) -> set[int]:
        return {
            self.config.blocked_role_id,
            self.config.ranks_below_role_id,
            self.config.ranks_above_role_id,
        } - {0}

    def is_blocked(self, member: discord.Member) -> bool:
        return bool(self.config.blocked_role_id and any(role.id == self.config.blocked_role_id for role in member.roles))

    @staticmethod
    def require_manage_roles(guild: discord.Guild) -> None:
        if guild.me is None or not guild.me.guild_permissions.manage_roles:
            raise MeowError("Shelley needs Manage Roles to run a season.")

    @staticmethod
    def reason(season: Season) -> str:
        return f"Meow season {season.id}"

    @staticmethod
    def staging_name(season: Season, reward: Reward) -> str:
        return f"meow {season.id} {reward.key}"

    @classmethod
    def creation_reason(cls, season: Season, reward: Reward) -> str:
        return f"{cls.reason(season)} reward {reward.key}"

    def require_safe_role(self, role: discord.Role, *, earned_medal: bool = False) -> None:
        guild = getattr(role, "guild", None)
        channel_grants = guild is not None and any(channel.overwrites_for(role).pair()[0].value for channel in guild.channels)
        protected = role.id in self.protected_ids
        if earned_medal and role.id == self.config.ranks_above_role_id:
            protected = role.id in {self.config.blocked_role_id, self.config.ranks_below_role_id}
        if protected or not role.is_assignable() or role.permissions.value or channel_grants:
            raise MeowError("Meow can only manage its own assignable roles without extra permissions.")

    def _rank_bounds(self, guild: discord.Guild, roles: dict[int, discord.Role]) -> tuple[discord.Role, discord.Role] | None:
        blocked = self.config.blocked_role_id
        if blocked and (blocked == guild.id or blocked not in roles):
            raise MeowError("The configured blocked role is missing or is @everyone.")
        if not self.config.ranks_above_role_id:
            return None
        lower = roles.get(self.config.ranks_above_role_id)
        upper = roles.get(self.config.ranks_below_role_id)
        me = guild.me
        if lower is None or upper is None or me is None or not lower < upper:
            raise MeowError("Set two existing rank bounds in the correct order.")
        if lower.is_default() or not upper < me.top_role:
            raise MeowError("Keep both rank bounds below Shelley and above @everyone.")
        return lower, upper

    def check_hierarchy(self, guild: discord.Guild) -> None:
        self.require_manage_roles(guild)
        self._rank_bounds(guild, {role.id: role for role in guild.roles})

    async def place_ranks(self, guild: discord.Guild, season: Season, roles: dict[int, discord.Role]) -> None:
        bounds = self._rank_bounds(guild, roles)
        if bounds is None:
            return
        for reward in season.design.ordered_ranks:
            role_id = season.roles.get(reward.key)
            role = roles.get(role_id) if role_id else None
            if role is None:
                raise MeowError("A rank role is missing.")
            self.require_safe_role(role)
            roles, bounds = await self._place_rank(guild, season, role, roles, bounds)

    async def _place_rank(
        self,
        guild: discord.Guild,
        season: Season,
        role: discord.Role,
        roles: dict[int, discord.Role],
        bounds: tuple[discord.Role, discord.Role],
    ) -> tuple[dict[int, discord.Role], tuple[discord.Role, discord.Role]]:
        attempted: set[int] = set()
        for _ in range(3):
            lower, upper = bounds
            if lower < role < upper:
                return roles, bounds
            preferred = lower.position if role < lower else lower.position + 1
            candidates = (preferred, upper.position, lower.position + 1)
            position = next((value for value in candidates if value > 0 and value not in attempted), None)
            if position is None:
                break
            attempted.add(position)
            roles = {item.id: item for item in await guild.edit_role_positions({role: position}, reason=self.reason(season))}
            current = self._rank_bounds(guild, roles)
            updated = roles.get(role.id)
            if current is None or updated is None:
                break
            bounds, role = current, updated
            self.require_safe_role(role)
        if bounds[0] < role < bounds[1]:
            return roles, bounds
        raise MeowError("Discord could not place the rank between the configured roles.")

    async def _recover_roles(
        self,
        guild: discord.Guild,
        season: Season,
        rewards: tuple[Reward, ...],
        existing: dict[int, discord.Role],
    ) -> dict[str, discord.Role]:
        untracked = [reward for reward in rewards if season.roles.get(reward.key) not in existing]
        if not untracked:
            return {}
        names = {role.name: role for role in existing.values()}
        recovered = {}
        reasons = {}
        for reward in untracked:
            staged = names.get(self.staging_name(season, reward))
            if staged is not None:
                recovered[reward.key] = staged
            else:
                reasons[self.creation_reason(season, reward)] = reward.key
        me = guild.me
        if reasons and me is not None and me.guild_permissions.view_audit_log:
            try:
                async for entry in guild.audit_logs(limit=100, action=discord.AuditLogAction.role_create, user=me):
                    reason = entry.reason or ""
                    key = reasons.get(reason)
                    target_id = getattr(entry.target, "id", None)
                    role = existing.get(target_id) if target_id is not None else None
                    if key is not None and role is not None:
                        recovered[key] = role
                        del reasons[reason]
                        if not reasons:
                            break
            except discord.Forbidden:
                logger.warning("Meow could not recover role IDs from the audit log in guild %s", guild.id)
        return recovered

    async def _ensure_role(
        self,
        guild: discord.Guild,
        season: Season,
        reward: Reward,
        existing: dict[int, discord.Role],
        *,
        medal: bool,
        recovered: discord.Role | None = None,
    ) -> discord.Role:
        role_id = season.roles.get(reward.key)
        role = existing.get(role_id) if role_id else None
        reason = self.reason(season)
        color = MEDAL_COLOR if medal else reward.color
        name = reward.role_symbol(medal=medal)
        if role is None:
            role = recovered or await guild.create_role(
                name=name,
                color=color,
                permissions=discord.Permissions.none(),
                reason=self.creation_reason(season, reward),
            )
            self.require_safe_role(role)
            await asyncio.to_thread(self.repository.store_role, season.id, reward.key, role.id)
        self.require_safe_role(role)
        if role.name != name or role.color.value != color:
            updated = await role.edit(name=name, color=color, reason=reason)
            if updated is not None:
                role = updated
        me = guild.me
        if medal and me is not None and role.position < me.top_role.position - 1:
            await guild.edit_role_positions({role: me.top_role.position - 1}, reason=reason)
        return role

    async def ensure(self, guild: discord.Guild, season: Season) -> dict[str, discord.Role]:
        self.require_manage_roles(guild)
        existing = {role.id: role for role in await guild.fetch_roles()}
        self._rank_bounds(guild, existing)
        recovered = await self._recover_roles(guild, season, season.design.rewards, existing)
        result = {}
        for reward in season.design.rewards:
            result[reward.key] = await self._ensure_role(
                guild,
                season,
                reward,
                existing,
                medal=reward == season.design.medal,
                recovered=recovered.get(reward.key),
            )
        if self.config.ranks_above_role_id:
            positions = {role.id: role for role in await guild.fetch_roles()}
            placed = {key: role.id for key, role in result.items()}
            await self.place_ranks(guild, replace(season, roles=placed), positions)
        await asyncio.to_thread(self.repository.mark_roles_synced, season.id, season.revision)
        return result

    async def award_medal(self, guild: discord.Guild, season: Season) -> None:
        self.require_manage_roles(guild)
        medal = season.design.medal
        winner_id = season.winner_id
        if medal is None or winner_id is None:
            raise MeowError("The season is missing a winner or medal.")
        try:
            member = await guild.fetch_member(winner_id)
        except discord.NotFound as exc:
            raise MeowError("The winner must rejoin the server to receive the medal.") from exc
        existing = {role.id: role for role in await guild.fetch_roles()}
        self._rank_bounds(guild, existing)
        medal_id = season.roles.get(medal.key)
        already_held = any(role.id == medal_id for role in member.roles)
        if (member.bot or member.pending or self.is_blocked(member)) and not already_held:
            await asyncio.to_thread(self.repository.reopen_medal_award, season)
            return
        recovered = await self._recover_roles(guild, season, (medal,), existing)
        role = await self._ensure_role(guild, season, medal, existing, medal=True, recovered=recovered.get(medal.key))
        if not already_held or role.id != medal_id:
            member = await guild.fetch_member(winner_id)
            if member.bot or member.pending or self.is_blocked(member):
                await asyncio.to_thread(self.repository.reopen_medal_award, season)
                return
            if all(held.id != role.id for held in member.roles):
                await member.add_roles(role, reason=self.reason(season))
        await asyncio.to_thread(self.repository.finish, season.id, member.name)

    async def restore_medals(self, member: discord.Member, medal_ids: Collection[int]) -> None:
        if member.bot or member.pending:
            return
        held = {role.id for role in member.roles}
        missing = sorted(set(medal_ids) - held)
        if not missing:
            return
        guild = member.guild
        self.require_manage_roles(guild)
        roles = {role.id: role for role in await guild.fetch_roles()}
        for role_id in missing:
            role = roles.get(role_id)
            if role is None:
                continue
            try:
                self.require_safe_role(role, earned_medal=True)
            except MeowError:
                logger.warning("Skipped unsafe saved Meow medal role %s in guild %s", role_id, guild.id)
                continue
            await member.add_roles(role, reason="Restore an earned Meow medal")

    async def apply_pending(self, guild: discord.Guild, season: Season) -> None:
        changes = await asyncio.to_thread(self.repository.pending_ranks, season.id)
        if not changes:
            return
        self.require_manage_roles(guild)
        roles = {role.id: role for role in guild.roles}
        rank_ids = {season.roles[reward.key] for reward in season.design.ranks if reward.key in season.roles}
        if not rank_ids.issubset(roles):
            roles = {role.id: role for role in await guild.fetch_roles()}
        bounds = self._rank_bounds(guild, roles)
        reason = self.reason(season)
        for change in changes:
            role_id = season.roles.get(change.reward_key) if change.reward_key is not None else None
            role = roles.get(role_id) if role_id is not None else None
            if change.reward_key is not None and role is None:
                raise MeowError("A seasonal role is missing; wait for Shelley to repair it.")
            if role is not None:
                self.require_safe_role(role)
            try:
                member = await guild.fetch_member(change.user_id)
            except discord.NotFound:
                await asyncio.to_thread(self.repository.retry_rank_later, season.id, change.user_id)
                continue
            try:
                if member.bot or member.pending:
                    await asyncio.to_thread(self.repository.retry_rank_later, season.id, change.user_id)
                    continue
                if self.is_blocked(member):
                    await self.remove_active_ranks(member, season)
                    await asyncio.to_thread(self.repository.retry_rank_later, season.id, change.user_id)
                    continue
                held = {item.id for item in member.roles}
                other_ranks = (held & rank_ids) - {role_id}
                obsolete = [roles[obsolete_id] for obsolete_id in other_ranks if obsolete_id in roles]
                for old_role in obsolete:
                    self.require_safe_role(old_role)
                if role is not None and role.id not in held:
                    if bounds is not None and not bounds[0] < role < bounds[1]:
                        raise MeowError("Season ranks must be between the configured roles.")
                    await member.add_roles(role, reason=reason)
                if obsolete:
                    await member.remove_roles(*obsolete, reason=reason)
            except discord.HTTPException as exc:
                await asyncio.to_thread(self.repository.retry_rank_later, season.id, change.user_id)
                logger.warning("Meow rank could not be assigned in guild %s (HTTP %s)", guild.id, exc.status)
                if exc.status == 429 or exc.status >= 500:
                    raise
                continue
            await asyncio.to_thread(self.repository.mark_rank_applied, season.id, change.user_id, role_id)

    async def remove_active_ranks(self, member: discord.Member, season: Season) -> None:
        rank_ids = {season.roles[rank.key] for rank in season.design.ranks if rank.key in season.roles}
        held = [role for role in member.roles if role.id in rank_ids]
        for role in held:
            self.require_safe_role(role)
        if held:
            await member.remove_roles(*held, reason=self.reason(season))
        await asyncio.to_thread(self.repository.mark_rank_applied, season.id, member.id, None)

    async def cleanup(self, guild: discord.Guild, season: Season) -> None:
        self.require_manage_roles(guild)
        removable = season.design.ranks if season.state == "finished" else season.design.rewards
        if removable:
            existing = {role.id: role for role in await guild.fetch_roles()}
            recovered = await self._recover_roles(guild, season, removable, existing)
            to_delete = {season.roles[reward.key] for reward in removable if reward.key in season.roles} | {
                role.id for role in recovered.values()
            }
            for role_id in to_delete & existing.keys():
                role = existing[role_id]
                self.require_safe_role(role)
                try:
                    await role.delete(reason=self.reason(season))
                except discord.NotFound:
                    pass
        await asyncio.to_thread(self.repository.mark_cleaned, season.id)
