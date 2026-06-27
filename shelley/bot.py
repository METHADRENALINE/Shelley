import logging

import discord
from discord.ext import commands

from .config import BotConfig
from .settings import env_name, get_config

INITIAL_EXTENSIONS = (
    "shelley.cogs.admin",
    "shelley.cogs.points",
    "shelley.cogs.star_forward",
    "shelley.cogs.status",
    "shelley.cogs.welcome",
)

logger = logging.getLogger(__name__)


class ShelleyBot(commands.Bot):
    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.default()
        intents.messages = True
        intents.reactions = True
        intents.message_content = True
        intents.voice_states = True

        options = {}
        if config.client_id:
            options["application_id"] = int(config.client_id)

        self.config = config
        super().__init__(command_prefix="!", intents=intents, **options)

    async def setup_hook(self) -> None:
        for extension in INITIAL_EXTENSIONS:
            await self.load_extension(extension)

        cfg = get_config()
        dev_guild_id = cfg.dev_guild_id

        try:
            if dev_guild_id:
                guild = discord.Object(id=int(dev_guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("synced commands to guild", extra={"guild_id": int(dev_guild_id), "commands": [c.name for c in synced]})
            else:
                synced = await self.tree.sync()
                logger.info("synced commands globally", extra={"commands": [c.name for c in synced]})
        except discord.Forbidden as e:
            logger.warning("command sync failed because of missing access: %s", e)
        except Exception as e:
            logger.exception("command sync failed")

    async def on_ready(self) -> None:
        logger.info("logged in", extra={"bot_user": str(self.user), "bot_user_id": int(self.user.id) if self.user else 0})
