from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from shelley.cogs.admin import (
    AdminCog,
    NotifyEditModal,
    NotifyMessageError,
    NotifySession,
    NotifySessionView,
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
            "Edit message",
            "Add files",
            "Clear files",
            "Cancel",
        }
        assert edit_labels == {"Publish", "Edit message", "Cancel"}
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
        await cog.edit_notify_message(cast(Any, channel), session, content)

        assert content == "Открой <#11>"
        assert message.edits == [{"content": "Открой <#11>"}]

        channel.message = Message(8)
        with pytest.raises(NotifyMessageError):
            await cog.edit_notify_message(cast(Any, channel), session, content)

    asyncio.run(scenario())
