from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from ..actions import run_remote_action
from ..security import require_administrator
from ..settings import get_config

logger = logging.getLogger(__name__)

DISCORD_FILES_PER_MESSAGE = 10
NOTIFY_SESSION_TTL_SECONDS = 1800


@dataclass
class NotifyAttachment:
    path: Path
    filename: str
    spoiler: bool = False
    description: str | None = None

    def to_file(self) -> discord.File:
        return discord.File(
            str(self.path),
            filename=self.filename,
            spoiler=self.spoiler,
            description=self.description,
        )


@dataclass
class NotifySession:
    guild_id: int
    user_id: int
    channel_id: int
    target_channel_id: int
    content: str
    temp_dir: Path
    created_at: float = field(default_factory=time.monotonic)
    attachments: list[NotifyAttachment] = field(default_factory=list)

    def is_expired(self) -> bool:
        return time.monotonic() - self.created_at > NOTIFY_SESSION_TTL_SECONDS

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)


def notify_message_content(text: str) -> str:
    value = str(text).strip()
    return value if value else "\u200b"


def notify_attachment_batches(
    attachments: list[NotifyAttachment],
) -> list[list[NotifyAttachment]]:
    return [
        attachments[index : index + DISCORD_FILES_PER_MESSAGE]
        for index in range(0, len(attachments), DISCORD_FILES_PER_MESSAGE)
    ]


def safe_attachment_filename(filename: str) -> str:
    name = Path(str(filename)).name.strip()
    return name or "attachment"


def notify_session_text(session: NotifySession) -> str:
    count = len(session.attachments)
    plural = "" if count == 1 else "s"
    return (
        "Notification draft is ready.\n"
        f"Attached file{plural}: {count}\n"
        "Use Add files for more uploads, then press Publish."
    )


class NotifyModal(discord.ui.Modal):
    def __init__(self, cog: AdminCog, target_channel_id: int) -> None:
        super().__init__(title="Notify")
        self.cog = cog
        self.target_channel_id = target_channel_id
        self.message_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
        )
        self.file_upload: discord.ui.FileUpload = discord.ui.FileUpload(
            required=False,
            min_values=0,
            max_values=DISCORD_FILES_PER_MESSAGE,
        )
        self.add_item(self.message_input)
        self.add_item(
            discord.ui.Label(
                text="Files",
                description="Optional, up to 10 files here. Add more later if needed.",
                component=self.file_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        session = await self.cog.create_notify_session(
            interaction,
            notify_message_content(str(self.message_input.value)),
            self.target_channel_id,
        )
        if session is None:
            return
        await self.cog.store_notify_attachments(session, self.file_upload.values)
        await self.cog.send_notify_session_response(interaction, session)


class NotifyFilesModal(discord.ui.Modal):
    def __init__(self, cog: AdminCog, key: tuple[int, int, int]) -> None:
        super().__init__(title="Add notify files")
        self.cog = cog
        self.key = key
        self.file_upload: discord.ui.FileUpload = discord.ui.FileUpload(
            required=True,
            min_values=1,
            max_values=DISCORD_FILES_PER_MESSAGE,
        )
        self.add_item(
            discord.ui.Label(
                text="Files",
                description="Upload up to 10 more files.",
                component=self.file_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None or session.is_expired():
            self.cog.close_notify_session(self.key)
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        await self.cog.store_notify_attachments(session, self.file_upload.values)
        await self.cog.send_notify_session_response(interaction, session)


class NotifySessionView(discord.ui.View):
    def __init__(self, cog: AdminCog, key: tuple[int, int, int]) -> None:
        super().__init__(timeout=NOTIFY_SESSION_TTL_SECONDS)
        self.cog = cog
        self.key = key

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        session = self.cog.notify_sessions.get(self.key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return False
        if int(interaction.user.id) != session.user_id:
            await interaction.response.send_message("Only the author can use this notification draft.", ephemeral=True)
            return False
        return await require_administrator(interaction)

    async def on_timeout(self) -> None:
        self.cog.close_notify_session(self.key)

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.publish_notify_session(interaction, self.key)

    @discord.ui.button(label="Add files", style=discord.ButtonStyle.primary)
    async def add_files(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        await interaction.response.send_modal(NotifyFilesModal(self.cog, self.key))

    @discord.ui.button(label="Clear files", style=discord.ButtonStyle.secondary)
    async def clear_files(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = self.cog.notify_sessions.get(self.key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return
        self.cog.clear_notify_attachments(session)
        await interaction.response.edit_message(content=notify_session_text(session), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.cog.close_notify_session(self.key)
        await interaction.response.edit_message(content="Notification draft cancelled.", view=None)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.notify_sessions: dict[tuple[int, int, int], NotifySession] = {}

    @app_commands.command(name="notify", description="Send a notification to the configured channel.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def notify(self, interaction: discord.Interaction) -> None:
        if not await require_administrator(interaction):
            return

        try:
            cfg = get_config()
            notify_channel_id = int(cfg.notify_channel_id)
            await interaction.response.send_modal(NotifyModal(self, notify_channel_id))
        except discord.HTTPException:
            logger.exception("notify command failed because of Discord API error")
            if not interaction.response.is_done():
                await interaction.response.send_message("Discord rejected the notify command.", ephemeral=True)
        except Exception:
            logger.exception("notify command failed")
            if not interaction.response.is_done():
                await interaction.response.send_message("Notify command failed.", ephemeral=True)

    async def create_notify_session(
        self,
        interaction: discord.Interaction,
        content: str,
        target_channel_id: int,
    ) -> NotifySession | None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("Notify can only be used inside a server channel.", ephemeral=True)
            return None

        key = self.notify_key(int(interaction.guild_id), int(interaction.user.id), int(interaction.channel_id))
        self.close_notify_session(key)
        session = NotifySession(
            guild_id=int(interaction.guild_id),
            user_id=int(interaction.user.id),
            channel_id=int(interaction.channel_id),
            target_channel_id=int(target_channel_id),
            content=content,
            temp_dir=Path(tempfile.mkdtemp(prefix="shelley_notify_")),
        )
        self.notify_sessions[key] = session
        return session

    async def send_notify_session_response(
        self,
        interaction: discord.Interaction,
        session: NotifySession,
    ) -> None:
        key = self.notify_key(session.guild_id, session.user_id, session.channel_id)
        content = notify_session_text(session)
        view = NotifySessionView(self, key)
        if getattr(interaction, "message", None) is not None:
            try:
                await interaction.response.edit_message(content=content, view=view)
                return
            except discord.HTTPException:
                logger.exception("failed to edit notify draft response")
        if interaction.response.is_done():
            await interaction.followup.send(content, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(content, view=view, ephemeral=True)

    async def store_notify_attachments(
        self,
        session: NotifySession,
        attachments: list[discord.Attachment],
    ) -> None:
        for attachment in attachments:
            await self.store_notify_attachment(session, attachment)

    async def store_notify_attachment(
        self,
        session: NotifySession,
        attachment: discord.Attachment,
    ) -> None:
        filename = safe_attachment_filename(attachment.filename)
        stored_name = f"{len(session.attachments) + 1:04d}_{filename}"
        path = session.temp_dir / stored_name
        await attachment.save(path)
        session.attachments.append(
            NotifyAttachment(
                path=path,
                filename=filename,
                spoiler=bool(getattr(attachment, "is_spoiler", lambda: False)()),
                description=getattr(attachment, "description", None),
            )
        )

    async def publish_notify_session(
        self,
        interaction: discord.Interaction,
        key: tuple[int, int, int],
    ) -> None:
        session = self.notify_sessions.get(key)
        if session is None:
            await interaction.response.send_message("This notification draft has expired.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            channel = await self.bot.fetch_channel(session.target_channel_id)
            if not isinstance(channel, discord.TextChannel):
                await interaction.edit_original_response(content="notify_channel_id must point to a text channel.", view=None)
                return

            batches = notify_attachment_batches(session.attachments)
            if not batches:
                await channel.send(content=session.content)
            else:
                for index, batch in enumerate(batches):
                    files = [attachment.to_file() for attachment in batch]
                    await channel.send(
                        content=session.content if index == 0 else None,
                        files=files,
                    )

            self.close_notify_session(key)
            await interaction.edit_original_response(content="Notification sent.", view=None)
        except discord.HTTPException:
            logger.exception("notify publish failed because of Discord API error")
            await interaction.edit_original_response(content="Discord rejected the notification.", view=None)
        except Exception:
            logger.exception("notify publish failed")
            await interaction.edit_original_response(content="Notification failed.", view=None)

    def clear_notify_attachments(self, session: NotifySession) -> None:
        for attachment in session.attachments:
            try:
                attachment.path.unlink(missing_ok=True)
            except OSError:
                logger.exception("failed to remove notify attachment temp file")
        session.attachments.clear()

    def close_notify_session(self, key: tuple[int, int, int]) -> None:
        session = self.notify_sessions.pop(key, None)
        if session is not None:
            session.cleanup()

    @staticmethod
    def notify_key(guild_id: int, user_id: int, channel_id: int) -> tuple[int, int, int]:
        return int(guild_id), int(user_id), int(channel_id)

    @app_commands.command(name="reboot", description="Reboot a configured server machine.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(target="Target server, for example bm")
    async def reboot(self, interaction: discord.Interaction, target: str):
        await run_remote_action(interaction, target, "reboot_command", "reboot")

    @app_commands.command(name="start", description="Start a configured server.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(target="Target server, for example bm")
    async def start(self, interaction: discord.Interaction, target: str):
        await run_remote_action(interaction, target, "start_command", "start")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
