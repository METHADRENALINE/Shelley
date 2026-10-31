import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from discord.ui.view import ViewStore
from pydantic import BaseModel, ConfigDict, ValidationError

from shelley.cogs.scp_sl import STATUS_KEY, ModeView, ScpSlCog
from shelley.scp_sl.client import MAX_RESPONSE_BYTES, ScpSlApiError, ScpSlClient, SnapshotCache
from shelley.scp_sl.config import ScpSlConfig
from shelley.scp_sl.protocol import Snapshot, VoteResult
from shelley.scp_sl.renderer import payload_signature, render_status

BOOT_ID = "11111111-1111-1111-1111-111111111111"
POLL_ID = "22222222-2222-2222-2222-222222222222"
TOKEN = "t" * 32


def config(**changes):
    return ScpSlConfig(enabled=True, display_address="example.com", **changes)


def snapshot(**changes):
    now = datetime.now(UTC)
    values = {
        "schema_version": 1,
        "boot_id": BOOT_ID,
        "heartbeat_sequence": 1,
        "heartbeat_at": now,
        "online_names": ["Zulu", "@everyone_*"],
        "phase": "voting",
        "mode": None,
        "mode_name_ru": None,
        "available_modes": [
            {"id": "plain", "name_ru": "\u041e\u0431\u044b\u0447\u043d\u044b\u0439"},
            {"id": "perks", "name_ru": "\u041f\u0435\u0440\u043a\u0438"},
            {"id": "paravoz", "name_ru": "\u041f\u0430\u0440\u0430\u0432\u043e\u0437"},
        ],
        "poll": {"id": POLL_ID, "deadline": now + timedelta(seconds=15), "open": True},
    }
    values.update(changes)
    return Snapshot.model_validate(values)


def cog(monkeypatch):
    cfg = SimpleNamespace(scp_sl=config(), status_channel_id=20)
    monkeypatch.setattr("shelley.cogs.scp_sl.get_config", lambda: cfg)
    bot = Mock()
    bot.user.id = 50
    value = ScpSlCog(bot)
    value.channel = Mock(spec=discord.TextChannel)
    value.channel.id = 20
    value.channel.guild.id = 10
    value.channel.send = AsyncMock()
    value.message = Mock(spec=discord.Message)
    value.message.id = 30
    value.message.edit = AsyncMock()
    value.client = Mock()
    value.client.snapshot = AsyncMock(return_value=snapshot())
    value.client.vote = AsyncMock(return_value=VoteResult(result="accepted"))
    value.cache.update(snapshot())
    return value


def interaction():
    value = Mock()
    value.user = Mock(spec=discord.Member)
    value.user.id = 123
    value.user.bot = False
    value.user.guild.id = 10
    value.guild_id = 10
    value.channel_id = 20
    value.message.id = 30
    value.message.author.id = 50
    value.response.defer = AsyncMock()
    value.edit_original_response = AsyncMock()
    value.delete_original_response = AsyncMock()
    return value


def test_config_defaults_and_runtime_secret(monkeypatch):
    config = ScpSlConfig()
    assert not config.enabled
    assert config.logo_url == config.display_address == ""
    assert config.poll_seconds == 1
    assert config.timeout_seconds == 2
    assert config.stale_seconds == 5
    monkeypatch.delenv(config.token_env, raising=False)
    config.validate_runtime()
    config = ScpSlConfig(enabled=True, display_address="example.com")
    with pytest.raises(ValueError):
        config.validate_runtime()
    monkeypatch.setenv(config.token_env, TOKEN)
    config.validate_runtime()
    monkeypatch.setenv(config.token_env, "secret\nheader")
    with pytest.raises(ValueError):
        config.validate_runtime()


@pytest.mark.parametrize(
    "changes",
    [
        {"display_address": "example.com:7777"},
        {"token_env": "unsafe-name"},
        {"api_url": "http://user:secret@localhost:8790"},
        {"api_url": "file:///secret"},
        {"api_url": "http://localhost:8790?token=secret"},
        {"logo_url": "javascript:alert(1)"},
        {"stale_seconds": 6},
        {"enabled": "true"},
    ],
)
def test_config_rejects_unsafe_values(changes):
    with pytest.raises(ValidationError):
        ScpSlConfig(**changes)


class TransportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ok: bool


def test_client_reuses_session_authenticates_and_closes(monkeypatch):
    monkeypatch.setenv("SHELLEY_SCP_SL_TOKEN", TOKEN)
    authorizations = []

    async def handler(request):
        authorizations.append(request.headers.get("Authorization"))
        return web.json_response({"ok": True})

    async def run():
        app = web.Application()
        app.router.add_get("/test", handler)
        async with TestServer(app) as server:
            client = ScpSlClient(config(api_url=str(server.make_url(""))))
            await client.start()
            session = client._session
            try:
                for _ in range(2):
                    assert (await client.request("GET", "/test", TransportResponse)).ok
                    await client.start()
                    assert client._session is session
            finally:
                await client.close()
            assert session.closed
            assert client._session is None

    asyncio.run(run())
    assert authorizations == ["Bearer " + TOKEN] * 2


@pytest.mark.parametrize("kind", ["redirect", "oversize", "chunked", "invalid", "error", "strict"])
def test_client_fails_closed_on_bad_responses(kind, monkeypatch):
    monkeypatch.setenv("SHELLEY_SCP_SL_TOKEN", TOKEN)
    redirects = []

    async def target(request):
        redirects.append(request.path)
        return web.json_response({"ok": True})

    async def handler(request):
        if kind == "redirect":
            raise web.HTTPFound("/target")
        if kind == "oversize":
            return web.Response(body=b"x" * (MAX_RESPONSE_BYTES + 1))
        if kind == "chunked":
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"x" * (MAX_RESPONSE_BYTES + 1))
            await response.write_eof()
            return response
        if kind == "invalid":
            return web.Response(text="not JSON")
        if kind == "strict":
            return web.json_response({"ok": "true"})
        raise web.HTTPServiceUnavailable()

    async def run():
        app = web.Application()
        app.router.add_get("/test", handler)
        app.router.add_get("/target", target)
        async with TestServer(app) as server:
            client = ScpSlClient(config(api_url=str(server.make_url(""))))
            await client.start()
            try:
                with pytest.raises(ScpSlApiError):
                    await client.request("GET", "/test", TransportResponse)
            finally:
                await client.close()

    asyncio.run(run())
    assert not redirects


def test_stale_heartbeat_sequence_and_failures_hide_cached_status():
    wall = datetime.now(UTC)
    cache = SnapshotCache()
    first = snapshot(heartbeat_at=wall)
    cache.update(first, now=100)
    assert cache.current(now=104, wall=wall + timedelta(seconds=4)) is first
    cache.update(snapshot(heartbeat_at=wall + timedelta(seconds=4)), now=104)
    assert cache.current(now=105, wall=wall + timedelta(seconds=4)) is None
    cache.update(snapshot(heartbeat_sequence=2, heartbeat_at=wall + timedelta(seconds=5)), now=105)
    assert cache.current(now=105, wall=wall + timedelta(seconds=5)) is not None
    assert cache.current(now=105, wall=wall + timedelta(seconds=10)) is None
    cache.fail()
    assert cache.current(now=105, wall=wall + timedelta(seconds=5)) is None
    assert cache.snapshot is not None
    cache.update(snapshot(heartbeat_sequence=1, heartbeat_at=wall + timedelta(seconds=5)), now=106)
    assert cache.current(now=106, wall=wall + timedelta(seconds=5)) is None


def test_boot_change_resets_sequence_and_future_heartbeat_is_rejected():
    wall = datetime.now(UTC)
    cache = SnapshotCache()
    cache.update(snapshot(heartbeat_sequence=200, heartbeat_at=wall), now=100)
    cache.update(snapshot(boot_id=POLL_ID, heartbeat_at=wall + timedelta(seconds=5)), now=105)
    assert cache.current(now=105, wall=wall + timedelta(seconds=5)) is not None
    assert cache.current(now=105, wall=wall - timedelta(seconds=1)) is None
    assert cache.current(now=110, wall=wall + timedelta(seconds=5)) is None


def test_renderer_uses_exact_player_count_sorted_escaped_names_and_localized_modes():
    value = snapshot(
        online_names=["Zulu", "@everyone_*", "Alpha"],
        phase="round",
        mode="perks",
        mode_name_ru="Custom_*",
        poll={"id": POLL_ID, "deadline": None, "open": False},
    )
    embeds = render_status(config(logo_url="https://example.com/logo.png"), value)
    assert len(embeds) == 2
    assert embeds[0].image.url == "https://example.com/logo.png"
    text = embeds[1].description
    assert "3 \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u0430" in text
    assert "@\u200beveryone\\_\\*, Alpha, Zulu" in text
    assert "Custom\\_\\*" in text
    assert ":green_circle:" in text
    assert "\u0412\u0435\u0440\u0441\u0438\u044f" not in text
    assert "15" not in text


def test_renderer_offline_and_empty_have_no_mode_or_voting_label():
    empty = snapshot(
        online_names=[],
        phase="empty",
        mode="plain",
        mode_name_ru="hidden",
        available_modes=[],
        poll={"id": None, "deadline": None, "open": False},
    )
    for value in [None, empty]:
        text = render_status(config(), value)[0].description
        assert "0 \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432" in text
        assert "\u0420\u0435\u0436\u0438\u043c" not in text
        assert "hidden" not in text
        assert "\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435 \u0437\u0430" not in text
        assert (":red_circle:" if value is None else ":green_circle:") in text


def test_signatures_ignore_heartbeat_deadline_but_include_poll_and_modes(monkeypatch):
    value = cog(monkeypatch)
    first = snapshot()
    second = snapshot(heartbeat_sequence=2)

    async def signature(data):
        view = ModeView(value, data)
        assert view.is_persistent()
        assert all(len(item.custom_id) <= 100 and POLL_ID in item.custom_id for item in view.children)
        result = payload_signature(render_status(value.config, data), view)
        view.stop()
        return result

    async def run():
        assert await signature(first) == await signature(second)
        changed = snapshot(poll={"id": BOOT_ID, "deadline": first.poll.deadline, "open": True})
        view = ModeView(value, changed)
        assert payload_signature(render_status(value.config, changed), view) != await signature(first)
        view.stop()
        text = render_status(value.config, first)[0].description
        assert "*\u0412\u0435\u0434\u0451\u0442\u0441\u044f \u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435*" in text
        assert text.endswith(
            "### **\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435 \u0437\u0430 \u0440\u0435\u0436\u0438\u043c**"
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid",
    [
        "bot",
        "guild",
        "member_guild",
        "channel",
        "message",
        "author",
        "nonmember",
        "boot",
        "poll",
        "mode",
        "stale",
        "closed",
        "expired",
        "empty",
    ],
)
def test_callback_security_never_forwards_invalid_votes(monkeypatch, invalid):
    value = cog(monkeypatch)
    event = interaction()
    boot_id, poll_id, mode = BOOT_ID, POLL_ID, "plain"
    if invalid == "bot":
        event.user.bot = True
    elif invalid == "guild":
        event.guild_id = 99
    elif invalid == "member_guild":
        event.user.guild.id = 99
    elif invalid == "channel":
        event.channel_id = 99
    elif invalid == "message":
        event.message.id = 99
    elif invalid == "author":
        event.message.author.id = 99
    elif invalid == "nonmember":
        event.user = Mock(spec=discord.User)
    elif invalid == "boot":
        boot_id = POLL_ID
    elif invalid == "poll":
        poll_id = BOOT_ID
    elif invalid == "mode":
        mode = "invalid"
    elif invalid == "stale":
        value.cache.fail()
    elif invalid in {"closed", "expired"}:
        value.cache.update(
            snapshot(poll={"id": POLL_ID, "deadline": datetime.now(UTC) - timedelta(seconds=1), "open": invalid == "expired"})
        )
    elif invalid == "empty":
        value.cache.update(snapshot(online_names=[], phase="empty", available_modes=[], poll={"id": None, "deadline": None, "open": False}))
    asyncio.run(value.handle_vote(event, boot_id, poll_id, mode))
    event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    value.client.vote.assert_not_awaited()
    event.edit_original_response.assert_not_awaited()
    event.delete_original_response.assert_awaited_once()


def test_votes_are_authoritative_accepted_private_and_duplicate_silent(monkeypatch):
    value = cog(monkeypatch)
    value.client.vote.side_effect = [VoteResult(result="accepted"), VoteResult(result="duplicate")]
    first, second = interaction(), interaction()

    async def run():
        await value.handle_vote(first, BOOT_ID, POLL_ID, "plain")
        await value.handle_vote(second, BOOT_ID, POLL_ID, "perks")

    asyncio.run(run())
    assert value.client.vote.await_count == 2
    request = value.client.vote.await_args_list[0].args[0]
    assert request.model_dump() == {"boot_id": BOOT_ID, "poll_id": POLL_ID, "mode": "plain", "discord_user_id": "123"}
    assert (
        first.edit_original_response.await_args.kwargs["content"] == "\u0413\u043e\u043b\u043e\u0441 \u043f\u0440\u0438\u043d\u044f\u0442!"
    )
    second.edit_original_response.assert_not_awaited()
    second.delete_original_response.assert_awaited_once()


def test_vote_api_failure_marks_offline_and_only_replies_privately(monkeypatch):
    value = cog(monkeypatch)
    value.client.vote.side_effect = ScpSlApiError("unavailable")
    event = interaction()
    asyncio.run(value.handle_vote(event, BOOT_ID, POLL_ID, "plain"))
    assert value.cache.current() is None
    event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    event.edit_original_response.assert_awaited_once()
    value.channel.send.assert_not_awaited()


def test_publish_deduplicates_then_hides_modes_when_api_fails(monkeypatch):
    value = cog(monkeypatch)

    async def run():
        await value.publish_once()
        assert value.message.edit.await_count == 1
        await value.publish_once()
        assert value.message.edit.await_count == 1
        value.client.snapshot.side_effect = ScpSlApiError("unavailable")
        await value.poll_once()
        await value.publish_once()
        assert value.message.edit.await_count == 2
        args = value.message.edit.await_args.kwargs
        assert args["view"] is None
        assert ":red_circle:" in args["embeds"][-1].description
        assert "\u0420\u0435\u0436\u0438\u043c" not in args["embeds"][-1].description
        await value.publish_once()
        assert value.message.edit.await_count == 2

    asyncio.run(run())


@pytest.mark.parametrize("error_type", [discord.Forbidden, discord.HTTPException])
def test_fetch_errors_do_not_recreate_messages(monkeypatch, error_type):
    value = cog(monkeypatch)
    value.message = None
    monkeypatch.setattr("shelley.cogs.scp_sl.get_status_message_id", Mock(return_value=30))
    response = SimpleNamespace(status=403, reason="Forbidden")
    value.channel.fetch_message = AsyncMock(side_effect=error_type(response, "unavailable"))
    with pytest.raises(error_type):
        asyncio.run(value.publish_once())
    value.channel.send.assert_not_awaited()


def test_notfound_recreates_with_stable_state_key_and_no_duplicate_next_tick(monkeypatch):
    value = cog(monkeypatch)
    response = SimpleNamespace(status=404, reason="Not Found")
    value.message.edit.side_effect = discord.NotFound(response, "deleted")
    replacement = Mock(spec=discord.Message)
    replacement.id = 31
    replacement.edit = AsyncMock()
    value.channel.send.return_value = replacement
    save = Mock()
    monkeypatch.setattr("shelley.cogs.scp_sl.set_status_message_id", save)

    async def run():
        await value.publish_once()
        await value.publish_once()

    asyncio.run(run())
    value.channel.send.assert_awaited_once()
    save.assert_called_once_with(10, 31, STATUS_KEY)
    assert STATUS_KEY == "scpsl"
    assert value.message is replacement


def test_startup_fetches_saved_scpsl_message_without_sending(monkeypatch):
    value = cog(monkeypatch)
    saved = value.message
    value.message = None
    value.channel.fetch_message = AsyncMock(return_value=saved)
    lookup = Mock(return_value=30)
    monkeypatch.setattr("shelley.cogs.scp_sl.get_status_message_id", lookup)
    asyncio.run(value.publish_once())
    lookup.assert_called_once_with(10, "scpsl")
    value.channel.fetch_message.assert_awaited_once_with(30)
    value.channel.send.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"heartbeat_sequence": "1"},
        {"boot_id": "unsafe"},
        {"online_names": ["a" * 101]},
        {"online_names": ["x"] * 129},
        {"online_names": ["@everyone\ntext"]},
        {"available_modes": [{"id": "plain", "name_ru": "x"}] * 2},
        {"online_names": ["one"]},
    ],
)
def test_strict_protocol_rejects_unsafe_or_inconsistent_snapshots(changes):
    with pytest.raises(ValidationError):
        snapshot(**changes)


def test_single_player_has_only_api_supplied_two_modes(monkeypatch):
    value = cog(monkeypatch)
    data = snapshot(online_names=["one"], available_modes=[{"id": "plain", "name_ru": "A"}, {"id": "perks", "name_ru": "B"}])

    async def run():
        view = ModeView(value, data)
        assert [item.label for item in view.children] == ["A", "B"]
        view.stop()

    asyncio.run(run())


def test_player_changes_do_not_unregister_new_view_callbacks(monkeypatch):
    value = cog(monkeypatch)

    async def run():
        store = ViewStore(Mock())

        async def edit(**kwargs):
            view = kwargs["view"]
            if view is not None:
                store.add_view(view, message_id=value.message.id)

        value.message.edit.side_effect = edit
        await value.publish_once()
        first_view = value._view
        value.cache.update(snapshot(heartbeat_sequence=2, online_names=["New", "Names"]))
        await value.publish_once()
        assert first_view.is_finished()
        assert value._view is not first_view
        registry = store._views[value.message.id]
        for button in value._view.children:
            assert registry[(button.type.value, button.custom_id)] is button
        assert store._synced_message_views[value.message.id] is value._view
        value._view.stop()

    asyncio.run(run())


def test_edit_failure_retries_without_creating_message_or_losing_callbacks(monkeypatch):
    value = cog(monkeypatch)

    async def run():
        await value.publish_once()
        value.cache.update(snapshot(heartbeat_sequence=2, online_names=["New", "Names"]))
        response = SimpleNamespace(status=403, reason="Forbidden")
        value.message.edit.side_effect = discord.Forbidden(response, "unavailable")
        with pytest.raises(discord.Forbidden):
            await value.publish_once()
        assert value._view is None
        value.message.edit.side_effect = None
        await value.publish_once()
        assert value._view is not None
        assert not value._view.is_finished()
        value.channel.send.assert_not_awaited()
        value._view.stop()

    asyncio.run(run())


def test_poll_logs_api_failure_only_on_transition_without_sensitive_exception(monkeypatch, caplog):
    value = cog(monkeypatch)
    value.client.snapshot.side_effect = ScpSlApiError(TOKEN)

    async def run():
        await value.poll_once()
        await value.poll_once()

    asyncio.run(run())
    assert caplog.text.count("SCP:SL status unavailable") == 1
    assert TOKEN not in caplog.text


def test_wire_protocol_endpoints_and_503_snapshot_rejection(monkeypatch):
    monkeypatch.setenv("SHELLEY_SCP_SL_TOKEN", TOKEN)
    votes = []
    requests = []
    state = snapshot()

    async def status(request):
        requests.append((request.method, request.path, request.headers.get("Authorization")))
        return web.json_response(state.model_dump(mode="json"))

    async def vote(request):
        requests.append((request.method, request.path, request.headers.get("Authorization")))
        votes.append(await request.json())
        return web.json_response({"result": "accepted" if len(votes) == 1 else "duplicate"})

    async def stale(request):
        return web.json_response(state.model_dump(mode="json"), status=503)

    async def run():
        app = web.Application()
        app.router.add_get("/status", status)
        app.router.add_post("/vote", vote)
        app.router.add_get("/stale", stale)
        async with TestServer(app) as server:
            client = ScpSlClient(config(api_url=str(server.make_url(""))))
            await client.start()
            value = cog(monkeypatch)
            value.client = client
            try:
                assert await client.snapshot() == state
                await value.handle_vote(interaction(), BOOT_ID, POLL_ID, "plain")
                await value.handle_vote(interaction(), BOOT_ID, POLL_ID, "perks")
                with pytest.raises(ScpSlApiError):
                    await client.request("GET", "/stale", Snapshot)
            finally:
                await client.close()

    asyncio.run(run())
    assert [request[:2] for request in requests] == [("GET", "/status"), ("POST", "/vote"), ("POST", "/vote")]
    assert all(request[2] == "Bearer " + TOKEN for request in requests)
    assert votes == [
        {"boot_id": BOOT_ID, "poll_id": POLL_ID, "discord_user_id": "123", "mode": "plain"},
        {"boot_id": BOOT_ID, "poll_id": POLL_ID, "discord_user_id": "123", "mode": "perks"},
    ]


def test_failed_edit_then_reverted_payload_still_registers_working_view(monkeypatch):
    value = cog(monkeypatch)
    original = value.cache.snapshot

    async def run():
        await value.publish_once()
        value.cache.update(snapshot(online_names=["New", "Names"]))
        value.message.edit.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="Error"), "retry")
        with pytest.raises(discord.HTTPException):
            await value.publish_once()
        value.cache.update(original)
        value.message.edit.side_effect = None
        await value.publish_once()
        assert value.message.edit.await_count == 3
        assert value._view is not None
        assert not value._view.is_finished()
        value._view.stop()

    asyncio.run(run())


def test_state_save_failure_retries_same_message_without_sending_duplicate(monkeypatch):
    value = cog(monkeypatch)
    value.message = None
    monkeypatch.setattr("shelley.cogs.scp_sl.get_status_message_id", Mock(return_value=None))
    replacement = Mock(spec=discord.Message)
    replacement.id = 31
    replacement.edit = AsyncMock()
    value.channel.send.return_value = replacement
    save = Mock(side_effect=[RuntimeError("database unavailable"), None])
    monkeypatch.setattr("shelley.cogs.scp_sl.set_status_message_id", save)

    async def run():
        with pytest.raises(RuntimeError):
            await value.publish_once()
        await value.publish_once()

    asyncio.run(run())
    value.channel.send.assert_awaited_once()
    assert save.call_count == 2
    assert value.message is replacement


def test_discord_background_loop_survives_error_and_retries(monkeypatch):
    value = cog(monkeypatch)
    value.bot.wait_until_ready = AsyncMock()
    value.config.poll_seconds = 0.001
    error = discord.HTTPException(SimpleNamespace(status=500, reason="Error"), "retry")
    value.publish_once = AsyncMock(side_effect=[error, asyncio.CancelledError()])

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await value.publish_loop()

    asyncio.run(run())
    assert value.publish_once.await_count == 2


def test_unload_cancels_poll_and_publish_and_closes_owned_session(monkeypatch):
    monkeypatch.setenv("SHELLEY_SCP_SL_TOKEN", TOKEN)
    cfg = SimpleNamespace(scp_sl=config(), status_channel_id=20)
    monkeypatch.setattr("shelley.cogs.scp_sl.get_config", lambda: cfg)
    value = ScpSlCog(Mock())

    async def wait():
        await asyncio.Event().wait()

    value.poll_loop = wait
    value.publish_loop = wait

    async def run():
        await value.cog_load()
        session = value.client._session
        tasks = list(value._tasks)
        assert len(tasks) == 2
        await value.cog_unload()
        assert all(task.cancelled() for task in tasks)
        assert session.closed
        assert not value._tasks

    asyncio.run(run())
