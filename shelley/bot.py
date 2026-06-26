import discord
from discord.ext import commands

from .settings import config_path, env_name, load_json

INITIAL_EXTENSIONS = (
    "shelley.cogs.admin",
    "shelley.cogs.points",
    "shelley.cogs.star_forward",
    "shelley.cogs.status",
    "shelley.cogs.welcome",
)


class ShelleyBot(commands.Bot):
    def __init__(self, application_id: int | None = None) -> None:
        intents = discord.Intents.default()
        intents.messages = True
        intents.reactions = True
        intents.message_content = True
        intents.voice_states = True

        options = {}
        if application_id:
            options["application_id"] = application_id

        super().__init__(command_prefix="!", intents=intents, **options)

    async def setup_hook(self) -> None:
        for extension in INITIAL_EXTENSIONS:
            await self.load_extension(extension)

        cfg = load_json(config_path())
        dev_guild_id = cfg.get("dev_guild_id")

        try:
            if dev_guild_id:
                guild = discord.Object(id=int(dev_guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"[{env_name()}] Synced to guild {dev_guild_id}: {[c.name for c in synced]}")
            else:
                synced = await self.tree.sync()
                print(f"[{env_name()}] Synced globally: {[c.name for c in synced]}")
        except discord.Forbidden as e:
            print(f"[{env_name()}] Command sync failed (Missing Access). {e}")
        except Exception as e:
            print(f"[{env_name()}] Command sync failed. {e!r}")

    async def on_ready(self) -> None:
        print(f"[{env_name()}] Logged in as {self.user} (id={self.user.id})")
