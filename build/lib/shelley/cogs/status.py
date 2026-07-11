import asyncio
import logging
from collections import Counter
from typing import Any

import discord
from discord.ext import commands

from ..config import BotConfig, ServerConfig, StatusMessageConfig
from ..discord_helpers import edit_status_message, get_or_create_status_message, recreate_status_message
from ..renderers.status import render_bm_status_embeds, render_smp_status_embeds, status_payload_signature
from ..services.minecraft import aggregate_cluster_status, probe_server, probe_server_component, with_hard_timeout
from ..settings import get_config
from ..state import (
    apply_status_version_memory,
    clear_starting_status,
    load_active_starting_statuses,
    normalize_status_message_ids_for_embed_layout,
)
from ..views.bm import status_message_view

logger = logging.getLogger(__name__)

StatusSnapshot = dict[str, Any]


async def fetch_status_channel(bot: commands.Bot, channel_id: int) -> discord.TextChannel:
    channel = await bot.fetch_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError("status_channel_id must point to a TextChannel.")
    return channel


async def ensure_status_messages(
    channel: discord.TextChannel,
    status_messages: list[StatusMessageConfig],
) -> dict[str, discord.Message]:
    messages: dict[str, discord.Message] = {}
    for index, message_cfg in enumerate(status_messages):
        key = message_cfg.key
        messages[key] = await get_or_create_status_message(
            channel,
            key,
            use_legacy_fallback=(index == 0),
        )
        logger.info("using status message", extra={"key": key, "message_id": messages[key].id, "channel_id": channel.id})
    return messages


def choose_cluster_version(gateway_version: str | None, components: list[dict[str, Any]]) -> str | None:
    versions = [
        str(component.get("version") or "").strip()
        for component in components
        if str(component.get("status") or "") == ":green_circle:" and str(component.get("version") or "").strip()
    ]
    if versions:
        return Counter(versions).most_common(1)[0][0]
    return gateway_version


async def collect_component_snapshots(
    server: ServerConfig,
    timeout_seconds: float,
    gateway_online: bool,
    gateway_version: str | None,
) -> StatusSnapshot:
    component_results = await asyncio.gather(*(probe_server_component(component, timeout_seconds) for component in server.components))
    component_statuses = [result[0] for result in component_results]
    status = aggregate_cluster_status(component_statuses, gateway_online)
    components = [
        {
            "label": component.label,
            "status": component_status,
            "players": players,
            "version": component_version,
        }
        for component, (component_status, players, component_version) in zip(server.components, component_results, strict=True)
    ]
    return {
        "status": status,
        "players": sum(component["players"] for component in components),
        "version": choose_cluster_version(gateway_version, components),
        "components": components,
    }


async def collect_server_snapshot(
    guild_id: int,
    server: ServerConfig,
    timeout_seconds: float,
    starting_statuses: set[str],
) -> StatusSnapshot:
    hard_timeout = max(1.0, timeout_seconds * 2.0 + 1.0)
    gateway_online, players, version = await with_hard_timeout(probe_server(server, timeout_seconds), hard_timeout)
    if server.components:
        return await collect_component_snapshots(server, timeout_seconds, gateway_online, version)
    if gateway_online:
        clear_starting_status(guild_id, server.placeholder)
        return {
            "status": ":green_circle:",
            "players": int(players or 0),
            "version": version,
            "components": [],
        }
    if server.placeholder in starting_statuses:
        return {
            "status": ":yellow_circle:",
            "players": 0,
            "version": version,
            "components": [],
        }
    return {
        "status": ":red_circle:",
        "players": 0,
        "version": version,
        "components": [],
    }


async def collect_status(config: BotConfig, guild_id: int) -> dict[str, StatusSnapshot]:
    snapshots: dict[str, StatusSnapshot] = {}
    starting_statuses = load_active_starting_statuses(guild_id)
    timeout_seconds = float(config.timeout_seconds)
    for server in config.servers:
        snapshots[server.placeholder] = await collect_server_snapshot(guild_id, server, timeout_seconds, starting_statuses)
    apply_status_version_memory(guild_id, snapshots)
    return snapshots


def render_status_embed(
    message_cfg: StatusMessageConfig, snapshots: dict[str, StatusSnapshot]
) -> tuple[str | None, list[discord.Embed], str | None]:
    if message_cfg.type == "separator":
        return message_cfg.content, [], None

    placeholder = str(message_cfg.status_placeholder)
    snapshot = snapshots.get(placeholder)
    if snapshot is None:
        raise KeyError(f"missing status snapshot: {placeholder}")

    renderer = str(message_cfg.renderer or "").strip().lower()
    template_path = str(message_cfg.template_path or "")
    if renderer == "smp_cluster":
        embeds = render_smp_status_embeds(template_path, snapshot)
    elif renderer == "bm":
        embeds = render_bm_status_embeds(template_path, snapshot)
    else:
        raise ValueError(f"Unknown status message renderer: {renderer}")

    control_status = str(snapshot["status"]) if str(message_cfg.control_target or "").strip().lower() == "bm" else None
    return None, embeds, control_status


async def publish_status_message(
    channel: discord.TextChannel,
    message_cfg: StatusMessageConfig,
    index: int,
    content: str | None,
    embeds: list[discord.Embed],
    control_status: str | None,
    messages: dict[str, discord.Message],
    last_signatures: dict[str, str],
) -> None:
    key = message_cfg.key
    view = status_message_view(message_cfg.model_dump(), control_status)
    signature = status_payload_signature(content, embeds, control_status if view is not None else None)

    if key not in messages:
        messages[key] = await get_or_create_status_message(
            channel,
            key,
            view=view,
            use_legacy_fallback=(index == 0),
        )

    if signature == last_signatures.get(key):
        return

    try:
        await edit_status_message(messages[key], content, embeds, view=view)
        last_signatures[key] = signature
    except discord.NotFound:
        messages[key] = await recreate_status_message(channel, key, view=view)
        await edit_status_message(messages[key], content, embeds, view=view)
        last_signatures[key] = signature
    except discord.Forbidden as e:
        logger.warning("forbidden editing status message key=%s: %s", key, e)
    except discord.HTTPException as e:
        logger.warning("discord error editing status message key=%s: %s", key, e)


async def publish_status_messages(
    channel: discord.TextChannel,
    status_messages: list[StatusMessageConfig],
    snapshots: dict[str, StatusSnapshot],
    messages: dict[str, discord.Message],
    last_signatures: dict[str, str],
) -> None:
    for index, message_cfg in enumerate(status_messages):
        try:
            content, embeds, control_status = render_status_embed(message_cfg, snapshots)
        except KeyError:
            logger.warning("missing status snapshot", extra={"placeholder": message_cfg.status_placeholder})
            continue
        await publish_status_message(channel, message_cfg, index, content, embeds, control_status, messages, last_signatures)


def handle_status_error() -> None:
    logger.exception("status loop iteration failed")


class StatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self.status_loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    async def status_loop(self) -> None:
        await self.bot.wait_until_ready()
        cfg = get_config()
        status_messages = list(cfg.status_messages)
        if not status_messages:
            raise RuntimeError("status_messages must contain at least one configured message.")

        channel = await fetch_status_channel(self.bot, int(cfg.status_channel_id))
        guild_id = int(channel.guild.id)
        normalize_status_message_ids_for_embed_layout(guild_id)
        messages = await ensure_status_messages(channel, status_messages)
        last_signatures: dict[str, str] = {}

        while True:
            try:
                snapshots = await collect_status(cfg, guild_id)
                await publish_status_messages(channel, status_messages, snapshots, messages, last_signatures)
            except Exception:
                handle_status_error()
            await asyncio.sleep(int(cfg.update_seconds))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatusCog(bot))
