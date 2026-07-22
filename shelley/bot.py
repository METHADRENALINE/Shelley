import logging

import discord
from discord import app_commands
from discord.ext import commands

from .config import BotConfig
from .settings import get_config

INITIAL_EXTENSIONS = (
    "shelley.cogs.admin",
    "shelley.cogs.information",
    "shelley.cogs.points",
    "shelley.cogs.star_forward",
    "shelley.cogs.status",
    "shelley.cogs.welcome",
)
GLOBAL_COMMAND_NAMES = frozenset({"privacy", "support", "terms"})

logger = logging.getLogger(__name__)


def configure_command_scopes(tree: app_commands.CommandTree, guild: discord.Object | None) -> None:
    if guild is not None:
        tree.copy_global_to(guild=guild)
        for name in GLOBAL_COMMAND_NAMES:
            tree.remove_command(name, guild=guild)

    for command in tuple(tree.get_commands()):
        if command.name not in GLOBAL_COMMAND_NAMES:
            command_type = getattr(command, "type", discord.AppCommandType.chat_input)
            tree.remove_command(command.name, type=command_type)


class ShelleyBot(commands.Bot):
    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.default()
        intents.messages = True
        intents.reactions = True
        intents.message_content = True
        intents.voice_states = True

        self.config = config
        if config.client_id:
            super().__init__(command_prefix="!", intents=intents, application_id=int(config.client_id))
        else:
            super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        for extension in INITIAL_EXTENSIONS:
            await self.load_extension(extension)

        cfg = get_config()
        dev_guild_id = cfg.dev_guild_id
        guild = discord.Object(id=int(dev_guild_id)) if dev_guild_id else None
        configure_command_scopes(self.tree, guild)

        try:
            global_synced = await self.tree.sync()
            logger.info("synced commands globally", extra={"commands": [c.name for c in global_synced]})
            if guild is not None:
                guild_synced = await self.tree.sync(guild=guild)
                logger.info("synced commands to guild", extra={"guild_id": int(dev_guild_id), "commands": [c.name for c in guild_synced]})
            else:
                logger.warning("dev_guild_id is not configured; guild commands were not synced")
        except discord.Forbidden as e:
            logger.warning("command sync failed because of missing access: %s", e)
        except Exception:
            logger.exception("command sync failed")

    async def on_ready(self) -> None:
        logger.info("logged in", extra={"bot_user": str(self.user), "bot_user_id": int(self.user.id) if self.user else 0})
