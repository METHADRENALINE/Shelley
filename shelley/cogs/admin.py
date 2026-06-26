from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from ..actions import run_remote_action
from ..discord_helpers import silent_cleanup, silent_defer
from ..security import require_administrator
from ..settings import config_path, load_json


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="notify", description="Send a notification to the configured channel.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def notify(
        self,
        interaction: discord.Interaction,
        text: str,
        file1: discord.Attachment | None = None,
        file2: discord.Attachment | None = None,
        file3: discord.Attachment | None = None,
    ):
        if not await require_administrator(interaction):
            return

        await silent_defer(interaction)

        try:
            cfg = load_json(config_path())
            notify_channel_id = int(cfg["notify_channel_id"])
            ch = await self.bot.fetch_channel(notify_channel_id)
            if not isinstance(ch, discord.TextChannel):
                return

            files: List[discord.File] = []
            for att in (file1, file2, file3):
                if att is not None:
                    files.append(await att.to_file())

            if files:
                await ch.send(content=text, files=files)
            else:
                await ch.send(content=text)

        except Exception as e:
            print(f"[notify] error: {e!r}")

        finally:
            await silent_cleanup(interaction)

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
