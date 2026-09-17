from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from shelley.cogs.admin import (
    AdminCog,
    NotifyAttachment,
    NotifyEditModal,
    NotifyEmbed,
    NotifyEmbedModal,
    NotifyMessageError,
    NotifySession,
    NotifySessionView,
    create_notify_embed,
    parse_notify_embed_color,
    parse_notify_message_id,
    resolve_notify_channels,
)


class ChannelReference:
    def __init__(self, name: str, channel_id: int) -> None:
        self.name = name
        self.id = channel_id
        self.mention = f"<#{channel_id}>"


def test_notify_command_exposes_optional_message_id() -> None:
    parameters = AdminCog.notify.parameters

    assert len(parameters) == 1
    assert parameters[0].name == "message_id"
    assert parameters[0].type.name == "string"
    assert not parameters[0].required


def test_parse_notify_message_id_accepts_discord_snowflakes() -> None:
    assert parse_notify_message_id(None) is None
    assert parse_notify_message_id("") is None
    assert parse_notify_message_id(" 123456789012345678 ") == 123456789012345678

    for value in ("message", "-1", "0", str(1 << 64), "１２３"):
        with pytest.raises(ValueError):
            parse_notify_message_id(value)


def test_resolve_notify_channels_handles_unicode_and_discord_markup() -> None:
    channels = (
        ChannelReference("🌐╹сервера", 1),
        ChannelReference("general", 2),
        ChannelReference("general-chat", 3),
        ChannelReference("duplicate", 4),
        ChannelReference("duplicate", 5),
    )
    content = "Читайте #🌐╹сервера и #GENERAL, затем #general-chat.\nНе менять <#99>, \\#general, prefix#general и #duplicate."

    assert resolve_notify_channels(content, channels) == (
        "Читайте <#1> и <#2>, затем <#3>.\nНе менять <#99>, \\#general, prefix#general и #duplicate."
    )


def test_notify_view_allows_text_edits_and_preserves_existing_files(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        cog = cast(Any, SimpleNamespace(notify_sessions={}))
        new_key = (1, 2, 3)
        edit_key = (1, 2, 4)
        cog.notify_sessions[new_key] = NotifySession(
            guild_id=1,
            user_id=2,
            channel_id=3,
            target_channel_id=5,
            content="New message",
            temp_dir=tmp_path / "new",
        )
        cog.notify_sessions[edit_key] = NotifySession(
            guild_id=1,
            user_id=2,
            channel_id=4,
            target_channel_id=5,
            content="Existing message",
            temp_dir=tmp_path / "edit",
            message_id=123456789012345678,
        )

        new_labels = {str(getattr(item, "label", "")) for item in NotifySessionView(cog, new_key).children}
        edit_labels = {str(getattr(item, "label", "")) for item in NotifySessionView(cog, edit_key).children}

        assert new_labels == {
            "Publish",
            "Preview",
            "Edit message",
            "Add embed",
            "Edit last embed",
            "Remove last embed",
            "Add files",
            "Clear files",
            "Cancel",
        }
        assert edit_labels == {
            "Publish",
            "Preview",
            "Edit message",
            "Add embed",
            "Edit last embed",
            "Remove last embed",
            "Cancel",
        }
        modal = NotifyEditModal(cog, new_key, "Line one\nLine two")
        assert modal.message_input.default == "Line one\nLine two"
        assert modal.message_input.style.name == "paragraph"

    asyncio.run(scenario())


def test_edit_notify_message_resolves_references_and_only_edits_content(
    tmp_path: Path,
) -> None:
    class Message:
        def __init__(self, author_id: int) -> None:
            self.author = SimpleNamespace(id=author_id)
            self.edits: list[dict[str, object]] = []

        async def edit(self, **kwargs: object) -> None:
            self.edits.append(kwargs)

    class Channel:
        def __init__(self, message: Message) -> None:
            self.message = message
            self.guild = SimpleNamespace(
                channels=(ChannelReference("🌐╹сервера", 11),),
                threads=(),
                emojis=(),
            )

        async def fetch_message(self, message_id: int) -> Message:
            assert message_id == 123456789012345678
            return self.message

    async def scenario() -> None:
        bot = SimpleNamespace(user=SimpleNamespace(id=7), emojis=())
        cog = AdminCog(cast(Any, bot))
        message = Message(7)
        channel = Channel(message)
        session = NotifySession(
            guild_id=1,
            user_id=2,
            channel_id=3,
            target_channel_id=4,
            content="Открой #🌐╹сервера",
            temp_dir=tmp_path,
            message_id=123456789012345678,
        )

        content = cog.resolve_notify_content(session, cast(Any, channel))
        embed = create_notify_embed("Title", "Description", "#5865F2").to_embed()
        await cog.edit_notify_message(cast(Any, channel), session, content, [embed])

        assert content == "Открой <#11>"
        assert message.edits == [{"content": "Открой <#11>", "embeds": [embed]}]

        channel.message = Message(8)
        with pytest.raises(NotifyMessageError):
            await cog.edit_notify_message(cast(Any, channel), session, content, [])

    asyncio.run(scenario())


def test_notify_embeds_validate_colors_and_preserve_order(tmp_path: Path) -> None:
    assert parse_notify_embed_color("") is None
    assert parse_notify_embed_color("#5865F2") == 0x5865F2
    assert parse_notify_embed_color("00ff7f") == 0x00FF7F
    for value in ("#12345", "#GG0000", "blue"):
        with pytest.raises(ValueError):
            parse_notify_embed_color(value)
    with pytest.raises(ValueError):
        create_notify_embed("", "", "")

    cog = AdminCog(cast(Any, SimpleNamespace()))
    embeds = [create_notify_embed(f"Embed {index}", f"Body {index}", "").to_embed() for index in range(23)]
    attachments = [NotifyAttachment(tmp_path / f"{index}.png", f"{index}.png") for index in range(12)]
    batches = cog.notify_batches("Message", embeds, attachments)

    assert [len(batch_embeds) for _, batch_embeds, _ in batches] == [10, 10, 3]
    assert [len(batch_files) for _, _, batch_files in batches] == [10, 2, 0]
    assert [embed.title for _, batch_embeds, _ in batches for embed in batch_embeds] == [f"Embed {index}" for index in range(23)]
    assert [content for content, _, _ in batches] == ["Message", None, None]

    long_embeds = [create_notify_embed(f"Long {index}", "A" * 4000, "").to_embed() for index in range(3)]
    long_batches = cog.notify_batches("Message", long_embeds, [])
    assert [len(batch_embeds) for _, batch_embeds, _ in long_batches] == [1, 1, 1]


def test_notify_embed_modal_edits_the_last_embed(tmp_path: Path) -> None:
    key = (1, 2, 3)
    session = NotifySession(
        guild_id=1,
        user_id=2,
        channel_id=3,
        target_channel_id=4,
        content="Message",
        temp_dir=tmp_path,
        embeds=[NotifyEmbed({"title": "First", "description": "Body", "color": 0x5865F2})],
    )
    cog = cast(Any, SimpleNamespace(notify_sessions={key: session}))
    modal = NotifyEmbedModal(cog, key, 0)

    assert modal.title_input.default == "First"
    assert modal.description_input.default == "Body"
    assert modal.color_input.default == "#5865F2"


def test_notify_preview_is_ephemeral_and_keeps_all_embed_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Response:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def send_message(self, **kwargs: object) -> None:
            self.calls.append(dict(kwargs))

        def is_done(self) -> bool:
            return bool(self.calls)

    class Followup:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def send(self, **kwargs: object) -> None:
            self.calls.append(dict(kwargs))

    async def scenario() -> None:
        key = (1, 2, 3)
        session = NotifySession(
            guild_id=1,
            user_id=2,
            channel_id=3,
            target_channel_id=4,
            content="Message",
            temp_dir=tmp_path,
            embeds=[create_notify_embed(f"Embed {index}", "Body", "") for index in range(12)],
        )
        cog = AdminCog(cast(Any, SimpleNamespace()))
        cog.notify_sessions[key] = session

        async def fetch_channel(_channel_id: int) -> object:
            return object()

        monkeypatch.setattr(cog, "fetch_notify_channel", fetch_channel)
        monkeypatch.setattr(cog, "resolve_notify_content", lambda _session, _channel: "Message")
        monkeypatch.setattr(cog, "resolve_notify_embeds", lambda _session, _channel: [embed.to_embed() for embed in session.embeds])
        interaction = SimpleNamespace(response=Response(), followup=Followup())

        await cog.preview_notify_session(cast(Any, interaction), key)

        assert len(interaction.response.calls) == 1
        assert len(interaction.followup.calls) == 1
        assert interaction.response.calls[0]["ephemeral"] is True
        assert interaction.followup.calls[0]["ephemeral"] is True
        assert [len(call["embeds"]) for call in (*interaction.response.calls, *interaction.followup.calls)] == [10, 2]
        assert interaction.response.calls[0]["content"] == "Message"
        assert interaction.followup.calls[0]["content"] == ""

    asyncio.run(scenario())
