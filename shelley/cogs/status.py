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
ProbeResult = tuple[bool, int | None, str | None]
ComponentProbeResult = tuple[str, int, str | None]


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


def server_probe_hard_timeout(timeout_seconds: float) -> float:
    return max(1.0, timeout_seconds + 1.0)


async def probe_gateway_safely(
    server: ServerConfig,
    timeout_seconds: float,
) -> ProbeResult:
    try:
        return await with_hard_timeout(
            probe_server(server, timeout_seconds),
            server_probe_hard_timeout(timeout_seconds),
        )
    except TimeoutError:
        logger.warning(
            "minecraft gateway probe timed out",
            extra={"placeholder": server.placeholder, "kind": server.kind},
        )
    except Exception:
        logger.exception(
            "minecraft gateway probe failed",
            extra={"placeholder": server.placeholder, "kind": server.kind},
        )
    return False, None, None


async def probe_component_safely(
    server: ServerConfig,
    component: Any,
    timeout_seconds: float,
) -> ComponentProbeResult:
    try:
        return await with_hard_timeout(
            probe_server_component(component, timeout_seconds),
            max(1.0, timeout_seconds + 3.0),
        )
    except TimeoutError:
        logger.warning(
            "minecraft component probe timed out",
            extra={"placeholder": server.placeholder, "component": component.label},
        )
    except Exception:
        logger.exception(
            "minecraft component probe failed",
            extra={"placeholder": server.placeholder, "component": component.label},
        )
    return ":red_circle:", 0, None


async def collect_component_results(
    server: ServerConfig,
    timeout_seconds: float,
) -> list[ComponentProbeResult]:
    return list(await asyncio.gather(*(probe_component_safely(server, component, timeout_seconds) for component in server.components)))


def build_cluster_snapshot(
    server: ServerConfig,
    component_results: list[ComponentProbeResult],
    gateway_online: bool,
    gateway_version: str | None,
) -> StatusSnapshot:
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


def offline_snapshot(
    server: ServerConfig,
    starting_statuses: set[str],
) -> StatusSnapshot:
    if server.components:
        return {
            "status": ":red_circle:",
            "players": 0,
            "version": None,
            "components": [
                {
                    "label": component.label,
                    "status": ":red_circle:",
                    "players": 0,
                    "version": None,
                }
                for component in server.components
            ],
        }
    status = ":yellow_circle:" if server.placeholder in starting_statuses else ":red_circle:"
    return {
        "status": status,
        "players": 0,
        "version": None,
        "components": [],
    }


async def collect_server_snapshot(
    guild_id: int,
    server: ServerConfig,
    timeout_seconds: float,
    starting_statuses: set[str],
) -> StatusSnapshot:
    try:
        if server.components:
            gateway_result, component_results = await asyncio.gather(
                probe_gateway_safely(server, timeout_seconds),
                collect_component_results(server, timeout_seconds),
            )
            gateway_online, _players, version = gateway_result
            return build_cluster_snapshot(
                server,
                component_results,
                gateway_online,
                version,
            )

        gateway_online, players, version = await probe_gateway_safely(
            server,
            timeout_seconds,
        )
        if gateway_online:
            try:
                clear_starting_status(guild_id, server.placeholder)
            except Exception:
                logger.exception(
                    "failed to clear starting status",
                    extra={"placeholder": server.placeholder},
                )
            return {
                "status": ":green_circle:",
                "players": int(players or 0),
                "version": version,
                "components": [],
            }
        return offline_snapshot(server, starting_statuses)
    except Exception:
        logger.exception(
            "server snapshot collection failed",
            extra={"placeholder": server.placeholder},
        )
        return offline_snapshot(server, starting_statuses)


async def collect_status(config: BotConfig, guild_id: int) -> dict[str, StatusSnapshot]:
    try:
        starting_statuses = load_active_starting_statuses(guild_id)
    except Exception:
        logger.exception("failed to load starting statuses")
        starting_statuses = set()
    timeout_seconds = float(config.timeout_seconds)
    results = await asyncio.gather(
        *(
            collect_server_snapshot(
                guild_id,
                server,
                timeout_seconds,
                starting_statuses,
            )
            for server in config.servers
        )
    )
    snapshots = {server.placeholder: snapshot for server, snapshot in zip(config.servers, results, strict=True)}
    try:
        apply_status_version_memory(guild_id, snapshots)
    except Exception:
        logger.exception("failed to update status version memory")
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
            await publish_status_message(
                channel,
                message_cfg,
                index,
                content,
                embeds,
                control_status,
                messages,
                last_signatures,
            )
        except KeyError:
            logger.warning(
                "missing status snapshot",
                extra={"placeholder": message_cfg.status_placeholder},
            )
        except Exception:
            logger.exception(
                "status message update failed",
                extra={"key": message_cfg.key},
            )


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
