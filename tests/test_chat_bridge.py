import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from shelley.chat_bridge.config import BridgeRoute, ChatBridgeConfig
from shelley.chat_bridge.protocol import PublicEvent, clean_text
from shelley.chat_bridge.store import BridgeStore
from shelley.cogs.chat_bridge import ChatBridgeCog


def config(**changes):
    data = {
        "enabled": True,
        "routes": {
            "game": {
                "guild_id": 123456789012345678,
                "channel_id": 123456789012345679,
                "nodes": {"server": {"token_env": "TEST_BRIDGE_TOKEN", "receive_discord": True}},
            }
        },
    }
    data.update(changes)
    return ChatBridgeConfig.model_validate(data)


def cog():
    value = object.__new__(ChatBridgeCog)
    value.cfg = config()
    value.store = Mock()
    value.store.enqueue.return_value = True
    value.store.pending.return_value = []
    value.bot = Mock()
    value.cooldowns = {}
    return value


def message(**changes):
    values = dict(
        author=SimpleNamespace(id=17, bot=False, name="username", display_name="nickname"),
        webhook_id=None,
        guild=SimpleNamespace(id=123456789012345678),
        channel=SimpleNamespace(id=123456789012345679),
        clean_content="hello",
        id=88,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def event(**changes):
    values = dict(id=str(uuid4()), timestamp=datetime.now(UTC).isoformat(), kind="chat", text="public")
    values.update(changes)
    return values


def test_routes_require_one_destination_and_separate_channels():
    route = config().routes["game"].model_dump()
    route["nodes"]["server"]["receive_discord"] = False
    with pytest.raises(ValidationError):
        BridgeRoute.model_validate(route)
    with pytest.raises(ValidationError):
        config(routes={"one": config().routes["game"], "two": config().routes["game"]})
    with pytest.raises(ValidationError):
        config(max_message_length=2001)
    with pytest.raises(ValidationError):
        BridgeRoute.model_validate({**config().routes["game"].model_dump(), "discord_format": "{text.__class__}"})
    with pytest.raises(ValidationError):
        BridgeRoute.model_validate({**config().routes["game"].model_dump(), "allowed_bot_user_ids": [0]})


def test_secrets_are_required_only_when_enabled(tmp_path, monkeypatch):
    ChatBridgeConfig().validate_runtime()
    value = config(tls_certificate=str(tmp_path / "cert"), tls_private_key=str(tmp_path / "key"))
    with pytest.raises(ValueError):
        value.validate_runtime()
    (tmp_path / "cert").touch()
    (tmp_path / "key").touch()
    monkeypatch.delenv("TEST_BRIDGE_TOKEN", raising=False)
    with pytest.raises(ValueError):
        value.validate_runtime()
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", secrets.token_urlsafe(32))
    value.validate_runtime()


@pytest.mark.parametrize("kind", ["private", "console", "actionbar", "command"])
def test_protocol_rejects_nonpublic_event_types(kind):
    with pytest.raises(ValidationError):
        PublicEvent.model_validate(event(kind=kind))


def test_protocol_rejects_naive_timestamp_and_removes_format_controls():
    with pytest.raises(ValidationError):
        PublicEvent.model_validate(event(timestamp="2026-01-01T00:00:00"))
    assert clean_text("§cpublic\u202esecret\x00\nnext") == "publicsecret\nnext"


@pytest.mark.parametrize(
    "changes",
    [
        {"guild": None},
        {"guild": SimpleNamespace(id=999)},
        {"channel": SimpleNamespace(id=999)},
        {"webhook_id": 12},
        {"author": SimpleNamespace(id=17, bot=True, name="bot")},
        {"clean_content": ""},
    ],
)
def test_discord_ignores_other_channels_bots_and_empty_messages(changes):
    value = cog()
    asyncio.run(value.on_message(message(**changes)))
    value.store.enqueue.assert_not_called()


def test_discord_uses_username_and_cooldown():
    value = cog()

    async def run():
        await value.on_message(message())
        await value.on_message(message(id=89))

    asyncio.run(run())
    value.store.enqueue.assert_called_once()
    args = value.store.enqueue.call_args.args
    assert args[:6] == ("game", "server", "minecraft", "88", "username", "hello")


def test_discord_accepts_only_configured_bots():
    value = cog()
    value.cfg.routes["game"].allowed_bot_user_ids = {42}

    async def run():
        await value.on_message(message(author=SimpleNamespace(id=41, bot=True, name="blocked")))
        await value.on_message(message(author=SimpleNamespace(id=42, bot=True, name="allowed")))

    asyncio.run(run())
    value.store.enqueue.assert_called_once()
    assert value.store.enqueue.call_args.args[4] == "allowed"


def test_exchange_auth_expiry_and_acknowledgement(monkeypatch):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", token)
    value = cog()
    fresh = event()
    stale = event(timestamp=(datetime.now(UTC) - timedelta(minutes=10)).isoformat())
    future = event(timestamp=(datetime.now(UTC) + timedelta(minutes=10)).isoformat())

    async def run():
        app = web.Application()
        app.router.add_post("/bridge/{route}/{node}/exchange", value.exchange)
        async with TestClient(TestServer(app)) as client:
            url = "/bridge/game/server/exchange"
            response = await client.post(url, json={"events": [fresh]})
            assert response.status == 401
            response = await client.post(
                url,
                headers={"Authorization": "Bearer " + token},
                json={
                    "events": [fresh, stale, future],
                    "acknowledged": ["12"],
                },
            )
            assert response.status == 200
            data = await response.json()
            assert set(data["accepted"]) == {fresh["id"], stale["id"], future["id"]}

    asyncio.run(run())
    value.store.enqueue.assert_called_once()
    value.store.acknowledge.assert_called_once_with("game", "server", ["12"])


def test_database_failure_is_retryable_and_does_not_acknowledge(monkeypatch):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", token)
    value = cog()
    value.store.enqueue.side_effect = RuntimeError("database unavailable")

    async def run():
        app = web.Application()
        app.router.add_post("/bridge/{route}/{node}/exchange", value.exchange)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/bridge/game/server/exchange", headers={"Authorization": "Bearer " + token}, json={"events": [event()]}
            )
            assert response.status == 503
            assert await response.text() == "Bridge storage unavailable"

    asyncio.run(run())


def test_publishing_disables_mentions_and_uses_stable_nonce():
    value = cog()
    value.store.pending.return_value = [{"sequence": 1, "event_id": str(uuid4()), "node": "server", "content": "@everyone **hello**"}]
    channel = Mock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=123456789012345678)
    channel.id = 123456789012345679
    value.bot.get_channel.return_value = channel
    payloads = []

    async def send(channel_id, *, params):
        payloads.append(dict(params.payload))
        return {"id": "55"}

    value.bot.http.send_message = send
    asyncio.run(value.publish_route("game"))
    asyncio.run(value.publish_route("game"))
    assert payloads[0]["nonce"] == payloads[1]["nonce"]
    assert payloads[0]["enforce_nonce"] is True
    assert payloads[0]["allowed_mentions"]["parse"] == []
    assert payloads[0]["flags"] == 4
    assert value.store.delivered.call_args.args == (1, 55)


def test_discord_failure_keeps_event_pending():
    value = cog()
    channel = Mock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(id=123456789012345678)
    channel.id = 123456789012345679
    value.bot.get_channel.return_value = channel
    value.store.pending.return_value = [{"sequence": 1, "event_id": str(uuid4()), "node": "server", "content": "hello"}]
    value.bot.http.send_message = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied"))
    with pytest.raises(discord.Forbidden):
        asyncio.run(value.publish_route("game"))
    value.store.delivered.assert_not_called()


def test_postgresql_queue_deduplication_routing_and_expiry():
    dsn = os.environ.get("SHELLEY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("SHELLEY_TEST_DATABASE_URL is not configured")
    from pathlib import Path

    from psycopg import connect, sql
    from psycopg.conninfo import make_conninfo

    from shelley.db import Database

    schema = "bridge_test_" + uuid4().hex
    with connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db = Database(make_conninfo(dsn, options="-c search_path=" + schema))
    try:
        db.execute((Path(__file__).parents[1] / "shelley/schema/006_chat_bridge.sql").read_text())
        store = BridgeStore(db, 2)
        expiry = datetime.now(UTC) + timedelta(minutes=1)
        assert store.enqueue("one", "node", "minecraft", "1", "user", "hello", expiry)
        assert store.enqueue("one", "node", "minecraft", "1", "user", "hello", expiry)
        assert len(store.pending("one", "minecraft", "node")) == 1
        assert store.pending("two", "minecraft", "node") == []
        store.acknowledge("one", "wrong", ["1"])
        assert len(store.pending("one", "minecraft", "node")) == 1
        store.acknowledge("one", "node", ["1"])
        assert store.pending("one", "minecraft", "node") == []
        assert store.enqueue("one", "node", "discord", "2", "", "old", datetime.now(UTC) - timedelta(seconds=1))
        assert store.pending("one", "discord") == []
        assert store.enqueue("one", "node", "discord", "3", "", "one", expiry)
        assert store.enqueue("one", "node", "discord", "4", "", "two", expiry)
        assert not store.enqueue("one", "node", "discord", "5", "", "three", expiry)
    finally:
        db.close()
        with connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
