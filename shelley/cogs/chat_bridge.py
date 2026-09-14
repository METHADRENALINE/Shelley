from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import ssl
import time
from datetime import UTC, datetime, timedelta

import discord
from aiohttp import web
from discord.ext import commands
from discord.http import handle_message_parameters
from pydantic import ValidationError

from ..chat_bridge.protocol import Exchange, clean_text
from ..chat_bridge.store import BridgeStore
from ..db import get_database
from ..settings import get_config

logger = logging.getLogger(__name__)


class ChatBridgeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = get_config().chat_bridge
        self.store = BridgeStore(get_database(get_config()), self.cfg.max_queue)
        self.runner: web.AppRunner | None = None
        self.worker: asyncio.Task | None = None
        self.cooldowns: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        if not self.cfg.enabled:
            return
        self.cfg.validate_runtime()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cfg.tls_certificate, self.cfg.tls_private_key)
        app = web.Application(client_max_size=128 * 1024)
        app.router.add_post("/bridge/{route}/{node}/exchange", self.exchange)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, self.cfg.bind_host, self.cfg.port, ssl_context=context).start()
        except Exception:
            await self.runner.cleanup()
            raise
        self.worker = asyncio.create_task(self.publish_loop(), name="chat-bridge")

    async def cog_unload(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.runner is not None:
            await self.runner.cleanup()

    async def exchange(self, request: web.Request) -> web.Response:
        route_name, node_name = request.match_info["route"], request.match_info["node"]
        route = self.cfg.routes.get(route_name)
        node = route.nodes.get(node_name) if route else None
        if (
            route is None
            or node is None
            or not hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + os.environ[node.token_env]).encode())
        ):
            raise web.HTTPUnauthorized()
        try:
            payload = Exchange.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid bridge event")
        now = datetime.now(UTC)
        accepted: list[str] = []
        try:
            await asyncio.to_thread(self.store.acknowledge, route_name, node_name, payload.acknowledged)
            for event in payload.events:
                event_id = str(event.id)
                text = clean_text(event.text)[: self.cfg.max_message_length]
                age = (now - event.timestamp).total_seconds()
                if (
                    not route.minecraft_to_discord
                    or (event.kind == "broadcast" and not route.public_announcements)
                    or not text
                    or age > self.cfg.message_ttl_seconds
                    or age < -30
                ):
                    accepted.append(event_id)
                    continue
                inserted = await asyncio.to_thread(
                    self.store.enqueue,
                    route_name,
                    node_name,
                    "discord",
                    event_id,
                    "",
                    text,
                    event.timestamp + timedelta(seconds=self.cfg.message_ttl_seconds),
                )
                if inserted:
                    accepted.append(event_id)
            messages = (
                await asyncio.to_thread(self.store.pending, route_name, "minecraft", node_name)
                if node.receive_discord and route.discord_to_minecraft
                else []
            )
        except Exception:
            logger.exception("chat bridge database exchange failed for route %s node %s", route_name, node_name)
            raise web.HTTPServiceUnavailable(text="Bridge storage unavailable")
        return web.json_response(
            {
                "accepted": accepted,
                "messages": [{"id": row["event_id"], "username": row["username"], "text": row["content"]} for row in messages],
            }
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self.cfg.enabled or message.webhook_id or message.guild is None:
            return
        for name, route in self.cfg.routes.items():
            if not route.discord_to_minecraft or message.guild.id != route.guild_id or message.channel.id != route.channel_id:
                continue
            if message.author.bot and message.author.id not in route.allowed_bot_user_ids:
                return
            text = clean_text(message.clean_content)[: self.cfg.max_message_length]
            if not text:
                return
            now = time.monotonic()
            self.cooldowns = {key: deadline for key, deadline in self.cooldowns.items() if deadline > now}
            key = (message.channel.id, message.author.id)
            if self.cooldowns.get(key, 0) > now:
                return
            self.cooldowns[key] = now + self.cfg.user_interval_seconds
            node_name = next(key for key, node in route.nodes.items() if node.receive_discord)
            try:
                await asyncio.to_thread(
                    self.store.enqueue,
                    name,
                    node_name,
                    "minecraft",
                    str(message.id),
                    clean_text(message.author.name),
                    text,
                    datetime.now(UTC) + timedelta(seconds=self.cfg.message_ttl_seconds),
                )
            except Exception:
                logger.exception("could not queue Discord chat for route %s", name)
            return

    async def publish_route(self, name: str) -> None:
        route = self.cfg.routes[name]
        if not route.minecraft_to_discord:
            return
        rows = await asyncio.to_thread(self.store.pending, name, "discord")
        if not rows:
            return
        channel = self.bot.get_channel(route.channel_id) or await self.bot.fetch_channel(route.channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != route.guild_id:
            logger.error("chat bridge target does not match configured guild for route %s", name)
            return
        for row in rows:
            node = route.nodes.get(row["node"])
            if node is None:
                continue
            text = route.discord_format.format(
                text=discord.utils.escape_markdown(row["content"]), server=discord.utils.escape_markdown(node.label)
            )
            nonce = str(int.from_bytes(hashlib.sha256((name + row["node"] + row["event_id"]).encode()).digest()[:8], "big"))
            with handle_message_parameters(content=text[:2000], allowed_mentions=discord.AllowedMentions.none(), nonce=nonce) as params:
                assert params.payload is not None
                params.payload["enforce_nonce"] = True
                params.payload["flags"] = 4
                result = await self.bot.http.send_message(channel.id, params=params)
            await asyncio.to_thread(self.store.delivered, row["sequence"], int(result["id"]))

    async def publish_loop(self) -> None:
        await self.bot.wait_until_ready()
        last_cleanup = 0.0
        while True:
            for name in self.cfg.routes:
                try:
                    await self.publish_route(name)
                except discord.HTTPException as error:
                    logger.warning("chat bridge Discord request failed for route %s status %s code %s", name, error.status, error.code)
                except Exception:
                    logger.exception("chat bridge publishing failed for route %s", name)
            if time.monotonic() - last_cleanup > 3600:
                try:
                    await asyncio.to_thread(self.store.cleanup, self.cfg.retention_hours)
                    last_cleanup = time.monotonic()
                except Exception:
                    logger.exception("chat bridge queue cleanup failed")
            await asyncio.sleep(self.cfg.poll_seconds)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatBridgeCog(bot))
