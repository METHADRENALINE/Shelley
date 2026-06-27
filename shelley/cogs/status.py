import asyncio
import logging
from typing import Dict, List, Optional

import discord
from discord.ext import commands

from ..discord_helpers import edit_status_message, get_or_create_status_message, recreate_status_message
from ..renderers.status import render_bm_status_embeds, render_smp_status_embeds, status_payload_signature
from ..services.minecraft import aggregate_cluster_status, probe_server, probe_server_component, with_hard_timeout
from ..settings import env_name, get_config
from ..state import apply_status_version_memory, clear_starting_status, load_active_starting_statuses, normalize_status_message_ids_for_embed_layout
from ..views.bm import status_message_view

logger = logging.getLogger(__name__)


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

        status_channel_id = int(cfg.status_channel_id)
        update_seconds = int(cfg.update_seconds)
        timeout_seconds = float(cfg.timeout_seconds)
        servers = cfg.servers

        status_messages = list(cfg.status_messages)
        if not status_messages:
            raise RuntimeError("status_messages must contain at least one configured message.")

        ch = await self.bot.fetch_channel(status_channel_id)
        if not isinstance(ch, discord.TextChannel):
            raise RuntimeError("status_channel_id must point to a TextChannel.")
        guild_id = int(ch.guild.id)

        normalize_status_message_ids_for_embed_layout(guild_id)

        messages: Dict[str, discord.Message] = {}
        last_signatures: Dict[str, str] = {}

        for index, message_cfg in enumerate(status_messages):
            key = message_cfg.key
            messages[key] = await get_or_create_status_message(
                ch,
                key,
                use_legacy_fallback=(index == 0),
            )
            logger.info("using status message", extra={"key": key, "message_id": messages[key].id, "channel_id": ch.id})

        while True:
            try:
                snapshots: Dict[str, dict] = {}
                starting_statuses = load_active_starting_statuses(guild_id)

                for s in servers:
                    hard = max(1.0, timeout_seconds * 2.0 + 1.0)
                    gateway_online, players, version = await with_hard_timeout(
                        probe_server(s, timeout_seconds),
                        hard,
                    )

                    if s.components:
                        component_results = await asyncio.gather(
                            *(
                                probe_server_component(component, timeout_seconds)
                                for component in s.components
                            )
                        )
                        component_statuses = [result[0] for result in component_results]
                        status = aggregate_cluster_status(
                            component_statuses,
                            gateway_online,
                        )
                        components = [
                            {
                                "label": component.label,
                                "status": component_status,
                                "players": players,
                            }
                            for component, (component_status, players) in zip(
                                s.components,
                                component_results,
                            )
                        ]
                        snapshots[s.placeholder] = {
                            "status": status,
                            "players": sum(component["players"] for component in components),
                            "version": version,
                            "components": components,
                        }
                    elif gateway_online:
                        clear_starting_status(guild_id, s.placeholder)
                        snapshots[s.placeholder] = {
                            "status": ":green_circle:",
                            "players": int(players or 0),
                            "version": version,
                            "components": [],
                        }
                    elif s.placeholder in starting_statuses:
                        snapshots[s.placeholder] = {
                            "status": ":yellow_circle:",
                            "players": 0,
                            "version": version,
                            "components": [],
                        }
                    else:
                        snapshots[s.placeholder] = {
                            "status": ":red_circle:",
                            "players": 0,
                            "version": version,
                            "components": [],
                        }

                apply_status_version_memory(guild_id, snapshots)

                for index, message_cfg in enumerate(status_messages):
                    key = message_cfg.key
                    message_type = message_cfg.type
                    content: Optional[str] = None
                    embeds: List[discord.Embed] = []
                    control_status: Optional[str] = None

                    if message_type == "separator":
                        content = message_cfg.content
                    else:
                        placeholder = str(message_cfg.status_placeholder)
                        snapshot = snapshots.get(placeholder)
                        if snapshot is None:
                            logger.warning("missing status snapshot", extra={"placeholder": placeholder})
                            continue

                        renderer = str(message_cfg.renderer or "").strip().lower()
                        template_path = str(message_cfg.template_path or "")
                        if renderer == "smp_cluster":
                            embeds = render_smp_status_embeds(template_path, snapshot)
                        elif renderer == "bm":
                            embeds = render_bm_status_embeds(template_path, snapshot)
                        else:
                            raise ValueError(f"Unknown status message renderer: {renderer}")

                        if str(message_cfg.control_target or "").strip().lower() == "bm":
                            control_status = str(snapshot["status"])

                    view = status_message_view(message_cfg.model_dump(), control_status)
                    signature = status_payload_signature(
                        content,
                        embeds,
                        control_status if view is not None else None,
                    )

                    if key not in messages:
                        messages[key] = await get_or_create_status_message(
                            ch,
                            key,
                            view=view,
                            use_legacy_fallback=(index == 0),
                        )

                    if signature != last_signatures.get(key):
                        try:
                            await edit_status_message(
                                messages[key],
                                content,
                                embeds,
                                view=view,
                            )
                            last_signatures[key] = signature
                        except discord.NotFound:
                            messages[key] = await recreate_status_message(
                                ch,
                                key,
                                view=view,
                            )
                            await edit_status_message(
                                messages[key],
                                content,
                                embeds,
                                view=view,
                            )
                            last_signatures[key] = signature
                        except discord.Forbidden as e:
                            logger.warning("forbidden editing status message key=%s: %s", key, e)
                        except discord.HTTPException as e:
                            logger.warning("discord error editing status message key=%s: %s", key, e)

            except Exception as e:
                logger.exception("status loop iteration failed")

            await asyncio.sleep(update_seconds)



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatusCog(bot))
