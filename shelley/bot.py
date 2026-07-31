import logging
from typing import Any

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
    "shelley.trademarks.cog",
)
GLOBAL_COMMAND_NAMES = frozenset({"privacy", "support", "terms"})
TRADEMARK_COMMAND_NAMES = frozenset({"tm", "Запатентовать трейд марку"})

logger = logging.getLogger(__name__)


def configure_command_scopes(
    tree: app_commands.CommandTree,
    guild: discord.Object | None,
    trademark_guilds: tuple[discord.Object, ...] = (),
) -> None:
    targets: dict[int, tuple[discord.Object, bool]] = {}
    if guild is not None:
        targets[int(guild.id)] = (guild, True)
    for trademark_guild in trademark_guilds:
        targets.setdefault(
            int(trademark_guild.id),
            (trademark_guild, guild is not None and trademark_guild.id == guild.id),
        )

    for target, primary in targets.values():
        tree.copy_global_to(guild=target)
        for name in GLOBAL_COMMAND_NAMES:
            tree.remove_command(name, guild=target)
        if not primary:
            for command in tuple(tree.get_commands(guild=target)):
                if command.name not in TRADEMARK_COMMAND_NAMES:
                    tree.remove_command(
                        command.name,
                        guild=target,
                        type=getattr(
                            command,
                            "type",
                            discord.AppCommandType.chat_input,
                        ),
                    )

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

        options: dict[str, Any] = {}
        if config.client_id:
            options["application_id"] = int(config.client_id)

        self.config = config
        super().__init__(command_prefix="!", intents=intents, **options)

    async def setup_hook(self) -> None:
        for extension in INITIAL_EXTENSIONS:
            await self.load_extension(extension)

        cfg = get_config()
        dev_guild_id = cfg.dev_guild_id
        guild = discord.Object(id=int(dev_guild_id)) if dev_guild_id else None
        trademark_guilds = tuple(discord.Object(id=int(guild_id)) for guild_id in cfg.trademarks.guilds if cfg.trademarks.enabled)
        configure_command_scopes(self.tree, guild, trademark_guilds)

        try:
            global_synced = await self.tree.sync()
            logger.info(
                "synced commands globally",
                extra={"commands": [c.name for c in global_synced]},
            )
            guild_targets = {int(item.id): item for item in trademark_guilds}
            if guild is not None:
                guild_targets[int(guild.id)] = guild
            for guild_target in guild_targets.values():
                guild_synced = await self.tree.sync(guild=guild_target)
                logger.info(
                    "synced commands to guild",
                    extra={
                        "guild_id": int(guild_target.id),
                        "commands": [c.name for c in guild_synced],
                    },
                )
            if not guild_targets:
                logger.warning("no command guilds are configured; guild commands were not synced")
        except discord.Forbidden as e:
            logger.warning("command sync failed because of missing access: %s", e)
        except Exception:
            logger.exception("command sync failed")

    async def on_ready(self) -> None:
        logger.info(
            "logged in",
            extra={
                "bot_user": str(self.user),
                "bot_user_id": int(self.user.id) if self.user else 0,
            },
        )
