import asyncio

import discord
from discord.ext import commands

from ..settings import config_path, env_name, load_json
from ..state import load_state, save_json


class StarForwardCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()

    @staticmethod
    def _star_forward_key(channel_id: int, message_id: int) -> str:
        return f"{int(channel_id)}:{int(message_id)}"

    async def _count_reactors(self, message: discord.Message, emoji: str) -> int:
        for reaction in message.reactions:
            if str(reaction.emoji) != emoji:
                continue

            user_ids: set[int] = set()
            async for user in reaction.users(limit=None):
                if user.bot:
                    continue
                user_ids.add(user.id)

            return len(user_ids)

        return 0

    async def _delete_star_forward_from_state(
        self,
        state_path: str,
        source_channel_id: int,
        source_message_id: int,
        target_channel_id: int,
    ) -> None:
        key = self._star_forward_key(source_channel_id, source_message_id)

        async with self._lock:
            state = load_state(state_path)

            forwards = state.get("star_forwards", {})
            if not isinstance(forwards, dict):
                forwards = {}

            forwarded_message_id = forwards.pop(key, None)
            state["star_forwards"] = forwards

            if forwarded_message_id:
                try:
                    target_channel = await self.bot.fetch_channel(int(target_channel_id))
                    if isinstance(target_channel, discord.TextChannel):
                        forwarded_message = await target_channel.fetch_message(int(forwarded_message_id))
                        await forwarded_message.delete()
                        print(f"[star_forward] Deleted forward {forwarded_message_id} for source {key}")
                except discord.NotFound:
                    pass
                except discord.Forbidden as e:
                    print(f"[star_forward] Forbidden deleting forward for source {key}: {e}")
                except discord.HTTPException as e:
                    print(f"[star_forward] HTTPException deleting forward for source {key}: {e}")

            save_json(state_path, state)

    async def _handle_star_forward_update(self, payload) -> None:
        cfg = load_json(config_path())
        star_cfg = cfg.get("star_forward")

        if not star_cfg or not star_cfg.get("enabled", True):
            return

        source_channel_ids = {int(x) for x in star_cfg.get("source_channel_ids", [])}
        target_channel_id = int(star_cfg["target_channel_id"])
        emoji = str(star_cfg.get("emoji", "⭐"))
        threshold = int(star_cfg.get("threshold", 3))
        state_path = cfg.get("state_path", f"data/state-{env_name()}.json")

        source_channel_id = int(payload.channel_id)
        source_message_id = int(payload.message_id)

        if source_channel_id not in source_channel_ids:
            return

        payload_emoji = getattr(payload, "emoji", None)
        if payload_emoji is not None and str(payload_emoji) != emoji:
            return

        if self.bot.user is not None and getattr(payload, "user_id", None) == self.bot.user.id:
            return

        try:
            source_channel = await self.bot.fetch_channel(source_channel_id)
            target_channel = await self.bot.fetch_channel(target_channel_id)

            if not isinstance(source_channel, discord.TextChannel):
                print(f"[star_forward] Source channel {source_channel_id} is not a TextChannel")
                return

            if not isinstance(target_channel, discord.TextChannel):
                print(f"[star_forward] Target channel {target_channel_id} is not a TextChannel")
                return

            source_message = await source_channel.fetch_message(source_message_id)
            reactors_count = await self._count_reactors(source_message, emoji)

        except discord.NotFound:
            await self._delete_star_forward_from_state(
                state_path=state_path,
                source_channel_id=source_channel_id,
                source_message_id=source_message_id,
                target_channel_id=target_channel_id,
            )
            return
        except discord.Forbidden as e:
            print(f"[star_forward] Forbidden fetching source/target/message: {e}")
            return
        except discord.HTTPException as e:
            print(f"[star_forward] HTTPException fetching source/target/message: {e}")
            return

        key = self._star_forward_key(source_channel_id, source_message_id)

        async with self._lock:
            state = load_state(state_path)

            forwards = state.get("star_forwards", {})
            if not isinstance(forwards, dict):
                forwards = {}

            state["star_forwards"] = forwards
            forwarded_message_id = forwards.get(key)

            if reactors_count >= threshold:
                if forwarded_message_id:
                    try:
                        await target_channel.fetch_message(int(forwarded_message_id))
                        return
                    except discord.NotFound:
                        forwards.pop(key, None)

                is_forwardable = getattr(source_message, "is_forwardable", None)
                if callable(is_forwardable) and not is_forwardable():
                    print(f"[star_forward] Source message {key} is not forwardable")
                    save_json(state_path, state)
                    return

                forward_method = getattr(source_message, "forward", None)
                if not callable(forward_method):
                    print("[star_forward] discord.py>=2.5 is required for Message.forward()")
                    save_json(state_path, state)
                    return

                try:
                    forwarded = await source_message.forward(target_channel)
                    forwards[key] = int(forwarded.id)
                    save_json(state_path, state)
                    print(f"[star_forward] Forwarded source {key} as {forwarded.id}")
                except discord.Forbidden as e:
                    print(f"[star_forward] Forbidden forwarding source {key}: {e}")
                except discord.HTTPException as e:
                    print(f"[star_forward] HTTPException forwarding source {key}: {e}")

                return

            if forwarded_message_id:
                try:
                    forwarded_message = await target_channel.fetch_message(int(forwarded_message_id))
                    await forwarded_message.delete()
                    print(
                        f"[star_forward] Removed forward {forwarded_message_id}; "
                        f"source {key} has {reactors_count} reactors"
                    )
                except discord.NotFound:
                    pass
                except discord.Forbidden as e:
                    print(f"[star_forward] Forbidden deleting forward {forwarded_message_id}: {e}")
                except discord.HTTPException as e:
                    print(f"[star_forward] HTTPException deleting forward {forwarded_message_id}: {e}")

                forwards.pop(key, None)
                save_json(state_path, state)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload) -> None:
        try:
            await self._handle_star_forward_update(payload)
        except Exception as e:
            print(f"[star_forward] on_raw_reaction_add failed: {e!r}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload) -> None:
        try:
            await self._handle_star_forward_update(payload)
        except Exception as e:
            print(f"[star_forward] on_raw_reaction_remove failed: {e!r}")

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload) -> None:
        try:
            await self._handle_star_forward_update(payload)
        except Exception as e:
            print(f"[star_forward] on_raw_reaction_clear failed: {e!r}")

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload) -> None:
        try:
            await self._handle_star_forward_update(payload)
        except Exception as e:
            print(f"[star_forward] on_raw_reaction_clear_emoji failed: {e!r}")



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StarForwardCog(bot))
