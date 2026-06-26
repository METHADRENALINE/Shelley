import asyncio
import time
from typing import List, Optional

import discord
from discord.ext import commands

from ..discord_helpers import edit_welcome_message_safely, ensure_welcome_message, get_or_create_welcome_message
from ..renderers.welcome import load_welcome_message_payload
from ..settings import config_path, env_name, file_sha256, load_json
from ..state import get_welcome_message_file_hash, set_welcome_message_file_hash


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self.welcome_loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    async def welcome_loop(self) -> None:
        await self.bot.wait_until_ready()
        cfg = load_json(config_path())

        welcome_channel_id = cfg.get("welcome_channel_id")
        if not welcome_channel_id:
            print(f"[{env_name()}] welcome_channel_id is not configured; welcome loop disabled")
            return

        update_seconds = int(cfg.get("welcome_update_seconds", cfg.get("update_seconds", 60)))
        message_path = cfg.get("welcome_message_path", "welcome-msg.json")
        state_path = cfg.get("state_path", f"data/state-{env_name()}.json")

        ch = await self.bot.fetch_channel(int(welcome_channel_id))
        if not isinstance(ch, discord.TextChannel):
            raise RuntimeError("welcome_channel_id must point to a TextChannel.")

        message: Optional[discord.Message] = None
        last_loaded_hash: Optional[str] = None
        last_synced_hash = get_welcome_message_file_hash(state_path)
        last_payload: Optional[tuple[Optional[str], List[discord.Embed]]] = None
        last_error: Optional[str] = None
        presence_check_seconds = max(
            update_seconds,
            int(cfg.get("welcome_presence_check_seconds", 60)),
        )
        next_presence_check_at = 0.0

        while True:
            try:
                current_hash = file_sha256(message_path)

                if current_hash != last_loaded_hash or last_payload is None:
                    content, embeds = load_welcome_message_payload(message_path)
                    last_payload = (content, embeds)
                    last_loaded_hash = current_hash
                else:
                    content, embeds = last_payload

                now = time.monotonic()
                should_check_presence = message is None or now >= next_presence_check_at
                message_was_created = False

                if should_check_presence:
                    message, message_was_created = await ensure_welcome_message(
                        ch,
                        state_path,
                        content,
                        embeds,
                        message,
                    )
                    next_presence_check_at = now + presence_check_seconds

                if message_was_created:
                    last_synced_hash = None

                if current_hash != last_synced_hash:
                    try:
                        if not message_was_created:
                            await edit_welcome_message_safely(message, content, embeds)
                    except discord.NotFound:
                        message, message_was_created = await get_or_create_welcome_message(
                            ch,
                            state_path,
                            content,
                            embeds,
                        )
                        if not message_was_created:
                            await edit_welcome_message_safely(message, content, embeds)
                        next_presence_check_at = time.monotonic() + presence_check_seconds

                    set_welcome_message_file_hash(state_path, current_hash)
                    last_synced_hash = current_hash
                    print(f"[{env_name()}] Synced welcome message id={message.id} in channel={ch.name}")

                if last_error is not None:
                    print(f"[{env_name()}] welcome loop recovered")
                    last_error = None

            except discord.NotFound:
                message = None
                last_synced_hash = None
                next_presence_check_at = 0.0
                print(f"[{env_name()}] Welcome message was deleted; recreating on next iteration")
            except discord.Forbidden as e:
                print(f"[{env_name()}] Forbidden syncing welcome message: {e}")
            except discord.HTTPException as e:
                print(f"[{env_name()}] HTTPException syncing welcome message: {e}")
            except Exception as e:
                error = repr(e)
                if error != last_error:
                    print(f"[{env_name()}] welcome loop iteration failed: {error}")
                last_error = error

            await asyncio.sleep(update_seconds)



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))
