from __future__ import annotations

import asyncio
import io
import json
import random
import time
from pathlib import Path
from typing import Any, Optional

import discord
from discord.ext import commands

try:
    from discord.ext import voice_recv
except ImportError:
    voice_recv = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None

from ..settings import config_path, load_json, save_json
from ..state import load_state


def int_id_set(values: Any) -> set[int]:
    if values is None:
        return set()
    if not isinstance(values, (list, tuple, set)):
        values = [values]

    result: set[int] = set()
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.add(parsed)
    return result


def points_cfg(cfg: dict) -> dict:
    raw = cfg.get("points", {})
    return raw if isinstance(raw, dict) else {}


def points_state_path(cfg: dict) -> str:
    return str(points_cfg(cfg).get("state_path", "data/points.json"))


def channel_is_counted(channel_id: int, include_ids: set[int], exclude_ids: set[int]) -> bool:
    if include_ids and channel_id not in include_ids:
        return False
    return channel_id not in exclude_ids


def message_counting_channel_id(message: discord.Message) -> Optional[int]:
    channel = message.channel
    if isinstance(channel, discord.TextChannel):
        return int(channel.id)
    if isinstance(channel, discord.Thread) and channel.parent_id is not None:
        return int(channel.parent_id)
    return None


def config_seconds(kind_cfg: dict, key: str, legacy_key: Optional[str], default: float) -> float:
    value = kind_cfg.get(key)
    if value is None and legacy_key:
        value = kind_cfg.get(legacy_key)
    if value is None:
        value = default

    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def random_points_amount(kind_cfg: dict) -> int:
    minimum = int(kind_cfg.get("award_min", 10))
    maximum = int(kind_cfg.get("award_max", 20))
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    return random.randint(minimum, maximum)


def points_user_record(state: dict, user_id: int) -> dict:
    users = state.get("users")
    if not isinstance(users, dict):
        users = {}
    state["users"] = users

    key = str(int(user_id))
    record = users.get(key)
    if not isinstance(record, dict):
        record = {}
        users[key] = record

    record["text_points"] = int(record.get("text_points", 0) or 0)
    record["voice_points"] = int(record.get("voice_points", 0) or 0)
    record["last_text_award_at"] = float(record.get("last_text_award_at", 0) or 0)
    record["last_voice_award_at"] = float(record.get("last_voice_award_at", 0) or 0)
    return record


def ranked_points_users(state: dict, field: str, limit: int) -> list[tuple[int, int]]:
    users = state.get("users", {})
    if not isinstance(users, dict):
        return []

    ranked: list[tuple[int, int]] = []
    for raw_user_id, record in users.items():
        if not isinstance(record, dict):
            continue
        try:
            user_id = int(raw_user_id)
            points = int(record.get(field, 0) or 0)
        except (TypeError, ValueError):
            continue
        if points > 0:
            ranked.append((user_id, points))

    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[: max(1, int(limit))]


def merge_points_state(existing: dict, update: dict) -> dict:
    merged = dict(existing) if isinstance(existing, dict) else {}

    for key, value in update.items():
        if key in ("users", "text_channel_cursors"):
            continue
        merged[key] = value

    existing_users = merged.get("users", {})
    if not isinstance(existing_users, dict):
        existing_users = {}
    update_users = update.get("users", {})
    if isinstance(update_users, dict):
        for user_id, record in update_users.items():
            if not isinstance(record, dict):
                continue
            old_record = existing_users.get(str(user_id), {})
            if not isinstance(old_record, dict):
                old_record = {}
            old_record.update(record)
            existing_users[str(user_id)] = old_record
    if existing_users:
        merged["users"] = existing_users

    existing_cursors = merged.get("text_channel_cursors", {})
    if not isinstance(existing_cursors, dict):
        existing_cursors = {}
    update_cursors = update.get("text_channel_cursors", {})
    if isinstance(update_cursors, dict):
        for channel_id, cursor in update_cursors.items():
            try:
                new_cursor = int(cursor)
                old_cursor = int(existing_cursors.get(str(channel_id), 0) or 0)
            except (TypeError, ValueError):
                continue
            existing_cursors[str(channel_id)] = max(old_cursor, new_cursor)
    if existing_cursors:
        merged["text_channel_cursors"] = existing_cursors

    return merged


def save_points_state(path: str, state: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    current = load_state(path)
    save_json(path, merge_points_state(current, state))


def voice_member_is_points_eligible(member: discord.Member, voice_state=None) -> bool:
    voice_state = voice_state if voice_state is not None else getattr(member, "voice", None)
    if getattr(member, "bot", False) or voice_state is None:
        return False
    return not bool(getattr(voice_state, "deaf", False) or getattr(voice_state, "self_deaf", False))


def load_points_font(size: int, *, bold: bool = False):
    if ImageFont is None:
        return None

    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def text_width(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0])


def fitted_text(draw, text: str, font, max_width: int) -> str:
    if text_width(draw, text, font) <= max_width:
        return text

    ellipsis = "..."
    if max_width <= text_width(draw, ellipsis, font):
        return ellipsis

    trimmed = text
    while trimmed and text_width(draw, trimmed + ellipsis, font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed.rstrip() + ellipsis) if trimmed else ellipsis


def draw_chat_icon(draw, x: int, y: int, size: int, fill: tuple[int, int, int, int]) -> None:
    box = (x, y, x + size, y + int(size * 0.72))
    draw.rounded_rectangle(box, radius=5, fill=fill)
    tail = [
        (x + int(size * 0.58), y + int(size * 0.72)),
        (x + int(size * 0.74), y + int(size * 0.72)),
        (x + int(size * 0.74), y + int(size * 0.92)),
    ]
    draw.polygon(tail, fill=fill)


def draw_microphone_icon(draw, x: int, y: int, size: int, fill: tuple[int, int, int, int]) -> None:
    stem_w = max(4, size // 8)
    mic_w = int(size * 0.45)
    mic_h = int(size * 0.62)
    cx = x + size // 2
    top = y + 1
    draw.rounded_rectangle(
        (cx - mic_w // 2, top, cx + mic_w // 2, top + mic_h),
        radius=max(5, mic_w // 2),
        fill=fill,
    )
    arc_box = (x + int(size * 0.18), y + int(size * 0.30), x + int(size * 0.82), y + int(size * 0.88))
    draw.arc(arc_box, 0, 180, fill=fill, width=stem_w)
    draw.line((cx, y + int(size * 0.78), cx, y + size), fill=fill, width=stem_w)
    draw.line((cx - int(size * 0.22), y + size, cx + int(size * 0.22), y + size), fill=fill, width=stem_w)


def points_asset_path(filename: str) -> Optional[Path]:
    base = Path(__file__).resolve()
    candidates = [
        Path.cwd() / "assets" / filename,
        base.parents[2] / "assets" / filename,
        base.parent / "assets" / filename,
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def load_points_icon(icon_kind: str, size: int):
    if Image is None:
        return None

    filename = "voice-points.png" if icon_kind == "voice" else "text-point.png"
    path = points_asset_path(filename)
    if path is None:
        return None

    try:
        icon = Image.open(path).convert("RGBA")
        alpha_bbox = icon.getchannel("A").getbbox()
        if alpha_bbox:
            icon = icon.crop(alpha_bbox)
        icon.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.alpha_composite(icon, ((size - icon.width) // 2, (size - icon.height) // 2))
        return canvas
    except Exception as e:
        print(f"[points] cannot load leaderboard icon {path}: {e!r}")
        return None


def render_points_leaderboard_png(
    rows: list[tuple[int, str, int]],
    *,
    icon_kind: str,
    accent_color: int,
) -> bytes:
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is required for image leaderboards.")

    scale = 2
    width = 720 * scale
    row_h = 64 * scale
    gap = 10 * scale
    visible_rows = rows or [(1, "Пока здесь пусто.", 0)]
    height = len(visible_rows) * row_h + max(0, len(visible_rows) - 1) * gap
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    rank_font = load_points_font(30 * scale, bold=True)
    name_font = load_points_font(28 * scale, bold=True)
    points_font = load_points_font(25 * scale, bold=True)
    dot_font = load_points_font(22 * scale, bold=True)

    row_color = (74, 78, 84, 255)
    text_color = (248, 249, 252, 255)
    muted_dot = (132, 135, 140, 255)
    rank_colors = {
        1: (255, 219, 31, 255),
        2: (222, 224, 228, 255),
        3: (225, 145, 54, 255),
    }

    icon_fill = (
        (accent_color >> 16) & 255,
        (accent_color >> 8) & 255,
        accent_color & 255,
        255,
    )
    if icon_fill == (0, 0, 0, 255):
        icon_fill = (248, 249, 252, 255)

    icon_size = 34 * scale
    icon_image = load_points_icon(icon_kind, icon_size)

    for index, (rank, name, points) in enumerate(visible_rows):
        top = index * (row_h + gap)
        draw.rounded_rectangle((0, top, width, top + row_h), radius=8 * scale, fill=row_color)

        rank_color = rank_colors.get(rank, text_color)
        rank_text = f"#{rank}"
        rank_x = 30 * scale
        rank_y = top + 15 * scale
        draw.text((rank_x, rank_y), rank_text, fill=rank_color, font=rank_font)
        dot_x = rank_x + text_width(draw, rank_text, rank_font) + 8 * scale
        draw.text((dot_x, top + 18 * scale), "•", fill=muted_dot, font=dot_font)

        name_x = dot_x + 18 * scale
        max_name_width = 485 * scale - name_x
        clean_name = str(name).replace("\n", " ").strip() or str(rank)
        draw.text((name_x, top + 16 * scale), fitted_text(draw, clean_name, name_font, max_name_width), fill=text_color, font=name_font)

        draw.text((504 * scale, top + 16 * scale), "•", fill=muted_dot, font=dot_font)
        icon_x = 523 * scale
        icon_y = top + 15 * scale
        if icon_image is not None:
            image.alpha_composite(icon_image, (icon_x, icon_y))
        elif icon_kind == "voice":
            draw_microphone_icon(draw, icon_x, icon_y, 28 * scale, (248, 249, 252, 255))
        else:
            draw_chat_icon(draw, icon_x, icon_y, 30 * scale, (248, 249, 252, 255))

        points_text = str(int(points))
        draw.text((670 * scale - text_width(draw, points_text, points_font), top + 18 * scale), points_text, fill=text_color, font=points_font)

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


if voice_recv is not None:

    class PointsVoiceSink(voice_recv.AudioSink):
        def __init__(self, cog: "PointsCog") -> None:
            super().__init__()
            self.cog = cog

        def wants_opus(self) -> bool:
            return True

        def write(self, _user, _data) -> None:
            return None

        def cleanup(self) -> None:
            return None

        @voice_recv.AudioSink.listener()
        def on_voice_member_speaking_start(self, member) -> None:
            self.cog.note_voice_speaking_threadsafe(member, True)

        @voice_recv.AudioSink.listener()
        def on_voice_member_speaking_stop(self, member) -> None:
            self.cog.note_voice_speaking_threadsafe(member, False)

        @voice_recv.AudioSink.listener()
        def on_voice_member_speaking_state(self, member, _ssrc, state) -> None:
            self.cog.note_voice_speaking_state_threadsafe(member, state)

else:
    PointsVoiceSink = None


class PointsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._text_poll_task: asyncio.Task | None = None
        self._leaderboard_task: asyncio.Task | None = None
        self._voice_task: asyncio.Task | None = None
        self._leaderboard_message: discord.Message | None = None
        self._leaderboard_signature: str | None = None
        self._leaderboard_dirty = True
        self._voice_started_at: dict[int, float] = {}
        self._voice_seconds: dict[int, float] = {}
        self._voice_warning_printed = False
        self._log_events = False
        self._config_logged = False

    async def cog_load(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._text_poll_task = asyncio.create_task(self.text_poll_loop())
        self._leaderboard_task = asyncio.create_task(self.leaderboard_loop())
        self._voice_task = asyncio.create_task(self.voice_loop())

    async def cog_unload(self) -> None:
        for task in (self._text_poll_task, self._leaderboard_task, self._voice_task):
            if task:
                task.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.log_config_once(load_json(config_path()))

    def log_config_once(self, cfg: dict) -> None:
        if self._config_logged:
            return

        pcfg = points_cfg(cfg)
        text_cfg = pcfg.get("text", {}) if isinstance(pcfg.get("text", {}), dict) else {}
        voice_cfg = pcfg.get("voice", {}) if isinstance(pcfg.get("voice", {}), dict) else {}
        leaderboard_cfg = pcfg.get("leaderboard", {}) if isinstance(pcfg.get("leaderboard", {}), dict) else {}
        print(
            "[points] config "
            f"enabled={bool(pcfg.get('enabled', False))} "
            f"text_channels={sorted(int_id_set(text_cfg.get('channel_ids', [])))} "
            f"text_excluded={sorted(int_id_set(text_cfg.get('excluded_channel_ids', [])))} "
            f"text_interval={config_seconds(text_cfg, 'interval_seconds', 'cooldown_seconds', 120):g}s "
            f"voice_channels={sorted(int_id_set(voice_cfg.get('channel_ids', [])))} "
            f"voice_active={config_seconds(voice_cfg, 'active_microphone_seconds', 'minimum_speaking_seconds', 3):g}s "
            f"voice_interval={config_seconds(voice_cfg, 'interval_seconds', 'cooldown_seconds', 120):g}s "
            f"leaderboard_channel={int(leaderboard_cfg.get('channel_id', 0) or 0)}"
        )
        self._config_logged = True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            cfg = load_json(config_path())
            self._log_events = bool(points_cfg(cfg).get("log_events", False))
            await self.award_text_points_for_message(message, cfg, source="gateway")
        except Exception as e:
            print(f"[points] message award failed: {e!r}")

    async def award_text_points_for_message(self, message: discord.Message, cfg: dict, *, source: str) -> bool:
        if message.guild is None or message.author.bot:
            return False

        counting_channel_id = message_counting_channel_id(message)
        if counting_channel_id is None:
            return False

        pcfg = points_cfg(cfg)
        text_cfg = pcfg.get("text", {})
        if not pcfg.get("enabled", False) or not isinstance(text_cfg, dict) or not text_cfg.get("enabled", True):
            return False

        include_ids = int_id_set(text_cfg.get("channel_ids", []))
        exclude_ids = int_id_set(text_cfg.get("excluded_channel_ids", []))
        if not channel_is_counted(counting_channel_id, include_ids, exclude_ids):
            return False

        now = time.time()
        cooldown = max(1.0, config_seconds(text_cfg, "interval_seconds", "cooldown_seconds", 120))
        amount = random_points_amount(text_cfg)
        state_path = points_state_path(cfg)

        if self._log_events:
            print(f"[points] text seen user={message.author.id} channel={counting_channel_id} source={source}")

        async with self._lock:
            state = load_state(state_path)
            record = points_user_record(state, int(message.author.id))
            remaining = cooldown - (now - float(record.get("last_text_award_at", 0) or 0))
            if remaining > 0:
                if self._log_events:
                    print(f"[points] text cooldown user={message.author.id} remaining={remaining:.1f}s source={source}")
                return False

            total = int(record.get("text_points", 0) or 0) + amount
            record["text_points"] = total
            record["last_text_award_at"] = now
            record["last_text_channel_id"] = counting_channel_id
            record["last_text_message_id"] = int(message.id)
            record["last_name"] = str(message.author)
            record["last_display_name"] = str(getattr(message.author, "display_name", message.author))
            save_points_state(state_path, state)

        self._leaderboard_dirty = True
        print(f"[points] text +{amount} user={message.author.id} channel={counting_channel_id} total={total} source={source}")
        return True

    async def text_poll_loop(self) -> None:
        await self.bot.wait_until_ready()
        print("[points] text poll loop started")

        while not self.bot.is_closed():
            try:
                cfg = load_json(config_path())
                pcfg = points_cfg(cfg)
                text_cfg = pcfg.get("text", {})
                if pcfg.get("enabled", False) and isinstance(text_cfg, dict) and text_cfg.get("enabled", True):
                    self._log_events = bool(pcfg.get("log_events", False))
                    await self.poll_text_channels(cfg)
                    sleep_seconds = max(2.0, float(text_cfg.get("poll_seconds", 15)))
                else:
                    sleep_seconds = 30.0
            except Exception as e:
                print(f"[points] text poll loop failed: {e!r}")
                sleep_seconds = 10.0

            await asyncio.sleep(sleep_seconds)

    async def poll_text_channels(self, cfg: dict) -> None:
        pcfg = points_cfg(cfg)
        text_cfg = pcfg.get("text", {})
        include_ids = int_id_set(text_cfg.get("channel_ids", []))
        exclude_ids = int_id_set(text_cfg.get("excluded_channel_ids", []))
        channel_ids = sorted(channel_id for channel_id in include_ids if channel_id not in exclude_ids)
        if not channel_ids:
            return

        state_path = points_state_path(cfg)
        poll_limit = max(1, min(100, int(text_cfg.get("poll_limit", 25))))
        lookback_seconds = max(1.0, float(text_cfg.get("poll_initial_lookback_seconds", 300)))
        now = discord.utils.utcnow()

        for channel_id in channel_ids:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception as e:
                    print(f"[points] cannot fetch text channel {channel_id}: {e!r}")
                    continue

            if not isinstance(channel, discord.TextChannel):
                continue

            async with self._lock:
                state = load_state(state_path)
                cursors = state.get("text_channel_cursors", {})
                if not isinstance(cursors, dict):
                    cursors = {}
                    state["text_channel_cursors"] = cursors
                last_id = int(cursors.get(str(channel_id), 0) or 0)

            after = discord.Object(id=last_id) if last_id else None
            messages: list[discord.Message] = []
            try:
                async for message in channel.history(limit=poll_limit, after=after, oldest_first=bool(after)):
                    if not last_id and (now - message.created_at).total_seconds() > lookback_seconds:
                        continue
                    messages.append(message)
            except discord.Forbidden:
                print(f"[points] cannot poll text channel {channel_id}: missing Read Message History")
                continue
            except discord.HTTPException as e:
                print(f"[points] cannot poll text channel {channel_id}: {e!r}")
                continue

            if not messages:
                if self._log_events:
                    print(f"[points] text poll channel={channel_id} messages=0")
                continue

            if self._log_events:
                print(f"[points] text poll channel={channel_id} messages={len(messages)}")

            messages.sort(key=lambda item: int(item.id))
            max_seen_id = max(int(message.id) for message in messages)
            for message in messages:
                await self.award_text_points_for_message(message, cfg, source="poll")

            async with self._lock:
                state = load_state(state_path)
                cursors = state.get("text_channel_cursors", {})
                if not isinstance(cursors, dict):
                    cursors = {}
                if max_seen_id > int(cursors.get(str(channel_id), 0) or 0):
                    cursors[str(channel_id)] = max_seen_id
                    state["text_channel_cursors"] = cursors
                    save_points_state(state_path, state)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, _before: discord.VoiceState, after: discord.VoiceState) -> None:
        if member.bot:
            return

        try:
            cfg = load_json(config_path())
            pcfg = points_cfg(cfg)
            voice_cfg = pcfg.get("voice", {})
            if not pcfg.get("enabled", False) or not isinstance(voice_cfg, dict) or not voice_cfg.get("enabled", True):
                self._set_voice_speaking_state(int(member.id), False)
                return

            include_ids = int_id_set(voice_cfg.get("channel_ids", []))
            exclude_ids = int_id_set(voice_cfg.get("excluded_channel_ids", []))
            after_channel_id = int(after.channel.id) if after.channel is not None else 0
            if (
                not after_channel_id
                or not channel_is_counted(after_channel_id, include_ids, exclude_ids)
                or not voice_member_is_points_eligible(member, after)
            ):
                self._set_voice_speaking_state(int(member.id), False)
        except Exception as e:
            print(f"[points] voice state update failed: {e!r}")

    def note_voice_speaking_threadsafe(self, member: discord.Member, speaking: bool) -> None:
        user_id = getattr(member, "id", None)
        if user_id is None:
            return
        if not voice_member_is_points_eligible(member):
            speaking = False
        if getattr(member, "bot", False):
            return

        loop = self._loop
        if loop is None or not loop.is_running():
            return

        loop.call_soon_threadsafe(self._set_voice_speaking_state, int(user_id), bool(speaking))

    def note_voice_speaking_state_threadsafe(self, member: discord.Member, state) -> None:
        value = getattr(state, "value", state)
        try:
            speaking = int(value) != 0
        except (TypeError, ValueError):
            speaking = bool(value)
        self.note_voice_speaking_threadsafe(member, speaking)

    def _set_voice_speaking_state(self, user_id: int, speaking: bool) -> None:
        user_id = int(user_id)
        now = time.monotonic()

        if speaking:
            if user_id not in self._voice_started_at:
                self._voice_started_at[user_id] = now
                if self._log_events:
                    print(f"[points] voice speaking start user={user_id}")
            return

        started_at = self._voice_started_at.pop(user_id, None)
        if started_at is None:
            return

        active_seconds = max(0.0, now - float(started_at))
        self._voice_seconds[user_id] = float(self._voice_seconds.get(user_id, 0.0)) + active_seconds
        if self._log_events:
            print(f"[points] voice speaking stop user={user_id} active_seconds={active_seconds:.2f}")

    async def voice_loop(self) -> None:
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                cfg = load_json(config_path())
                pcfg = points_cfg(cfg)
                self._log_events = bool(pcfg.get("log_events", False))
                voice_cfg = pcfg.get("voice", {})
                if pcfg.get("enabled", False) and isinstance(voice_cfg, dict) and voice_cfg.get("enabled", True):
                    await self.ensure_voice_monitor(cfg)
                    await self.award_voice_points_due(cfg)
                    sleep_seconds = max(0.5, float(voice_cfg.get("check_seconds", 1)))
                else:
                    self.clear_voice_tracking()
                    sleep_seconds = 30.0
            except Exception as e:
                print(f"[points] voice loop failed: {e!r}")
                sleep_seconds = 10.0

            await asyncio.sleep(sleep_seconds)

    def clear_voice_tracking(self) -> None:
        for user_id in list(self._voice_started_at):
            self._set_voice_speaking_state(user_id, False)
        self._voice_started_at.clear()
        self._voice_seconds.clear()

    async def ensure_voice_monitor(self, cfg: dict) -> None:
        pcfg = points_cfg(cfg)
        voice_cfg = pcfg.get("voice", {})
        if voice_recv is None or PointsVoiceSink is None:
            if not self._voice_warning_printed:
                print("[points] discord-ext-voice-recv is not installed; voice points are disabled")
                self._voice_warning_printed = True
            return

        channel_ids = [
            channel_id
            for channel_id in int_id_set(voice_cfg.get("channel_ids", []))
            if channel_id not in int_id_set(voice_cfg.get("excluded_channel_ids", []))
        ]
        if not channel_ids:
            return

        target_channel = None
        for channel_id in channel_ids:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception as e:
                    print(f"[points] cannot fetch voice channel {channel_id}: {e!r}")
                    continue

            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                target_channel = channel
                break

        if target_channel is None:
            return

        voice_client = discord.utils.get(self.bot.voice_clients, guild=target_channel.guild)
        if voice_client is not None and getattr(getattr(voice_client, "channel", None), "id", None) != target_channel.id:
            await voice_client.move_to(target_channel)

        if voice_client is None or not voice_client.is_connected():
            voice_client = await target_channel.connect(
                cls=voice_recv.VoiceRecvClient,
                self_mute=True,
                self_deaf=False,
            )

        is_listening = getattr(voice_client, "is_listening", None)
        if callable(is_listening) and is_listening():
            return

        listen = getattr(voice_client, "listen", None)
        if not callable(listen):
            print("[points] current voice client does not support receive listening")
            return

        listen(PointsVoiceSink(self))
        print(f"[points] Listening for voice activity in {target_channel.name} ({target_channel.id})")

    def eligible_voice_members(self, cfg: dict) -> dict[int, discord.Member]:
        pcfg = points_cfg(cfg)
        voice_cfg = pcfg.get("voice", {})
        include_ids = int_id_set(voice_cfg.get("channel_ids", []))
        exclude_ids = int_id_set(voice_cfg.get("excluded_channel_ids", []))
        members: dict[int, discord.Member] = {}

        for channel_id in include_ids:
            if channel_id in exclude_ids:
                continue
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                continue

            eligible_members = [
                member
                for member in channel.members
                if isinstance(member, discord.Member) and voice_member_is_points_eligible(member)
            ]
            if len(eligible_members) < 2:
                continue

            for member in eligible_members:
                members[int(member.id)] = member

        return members

    def counted_voice_member_ids(self, cfg: dict) -> set[int]:
        return set(self.eligible_voice_members(cfg))

    async def award_voice_points_due(self, cfg: dict) -> None:
        pcfg = points_cfg(cfg)
        voice_cfg = pcfg.get("voice", {})
        eligible_members = self.eligible_voice_members(cfg)
        counted_member_ids = set(eligible_members)

        for user_id in list(self._voice_started_at):
            if user_id not in counted_member_ids:
                self._set_voice_speaking_state(user_id, False)

        for user_id in list(self._voice_seconds):
            if user_id not in counted_member_ids:
                self._voice_seconds.pop(user_id, None)

        if not counted_member_ids:
            self._voice_seconds.clear()
            return

        now_wall = time.time()
        now_mono = time.monotonic()
        cooldown = max(1.0, config_seconds(voice_cfg, "interval_seconds", "cooldown_seconds", 120))
        minimum_speaking = max(
            0.1,
            config_seconds(voice_cfg, "active_microphone_seconds", "minimum_speaking_seconds", 3),
        )
        state_path = points_state_path(cfg)
        awarded = False
        changed = False

        async with self._lock:
            state = load_state(state_path)
            for user_id in sorted(counted_member_ids):
                accumulated = float(self._voice_seconds.get(user_id, 0.0))
                if user_id in self._voice_started_at:
                    accumulated += max(0.0, now_mono - float(self._voice_started_at[user_id]))

                if accumulated < minimum_speaking:
                    continue

                record = points_user_record(state, user_id)
                if now_wall - float(record.get("last_voice_award_at", 0) or 0) < cooldown:
                    continue

                amount = random_points_amount(voice_cfg)
                total = int(record.get("voice_points", 0) or 0) + amount
                record["voice_points"] = total
                record["last_voice_award_at"] = now_wall
                member = eligible_members.get(user_id)
                if member is not None:
                    record["last_name"] = str(member)
                    record["last_display_name"] = str(getattr(member, "display_name", member))
                self._voice_seconds[user_id] = 0.0
                if user_id in self._voice_started_at:
                    self._voice_started_at[user_id] = now_mono
                awarded = True
                changed = True
                print(f"[points] voice +{amount} user={user_id} total={total}")

            if changed:
                save_points_state(state_path, state)

        if awarded:
            self._leaderboard_dirty = True

    async def resolve_display_name(self, guild: discord.Guild | None, user_id: int, record: dict) -> str:
        if guild is not None:
            member = guild.get_member(int(user_id))
            if member is not None:
                return str(member.display_name)

        user = self.bot.get_user(int(user_id))
        if user is not None:
            return str(getattr(user, "global_name", None) or user.name)

        return str(record.get("last_display_name") or record.get("last_name") or user_id)

    async def build_leaderboard_embed(
        self,
        title: str,
        field: str,
        state: dict,
        guild: discord.Guild | None,
        limit: int,
        color: int,
    ) -> discord.Embed:
        users = state.get("users", {})
        if not isinstance(users, dict):
            users = {}

        lines: list[str] = []
        for rank, (user_id, points) in enumerate(ranked_points_users(state, field, limit), start=1):
            record = users.get(str(user_id), {})
            if not isinstance(record, dict):
                record = {}
            name = await self.resolve_display_name(guild, user_id, record)
            name = discord.utils.escape_markdown(name).replace("\n", " ")[:42]
            lines.append(f"`#{rank}`  **{name}**  -  `{points}`")

        if not lines:
            lines.append("Пока здесь пусто.")

        return discord.Embed(title=title, description="\n".join(lines), color=int(color))

    async def build_leaderboard_embeds(self, state: dict, guild: discord.Guild | None, pcfg: dict) -> list[discord.Embed]:
        leaderboard_cfg = pcfg.get("leaderboard", {})
        if not isinstance(leaderboard_cfg, dict):
            leaderboard_cfg = {}

        limit = int(leaderboard_cfg.get("limit", 10))
        text_color = int(leaderboard_cfg.get("text_color", 0x5865F2))
        voice_color = int(leaderboard_cfg.get("voice_color", 0x57F287))

        return [
            await self.build_leaderboard_embed("Текст поинты", "text_points", state, guild, limit, text_color),
            await self.build_leaderboard_embed("Войс поинты", "voice_points", state, guild, limit, voice_color),
        ]

    async def leaderboard_rows(
        self,
        field: str,
        state: dict,
        guild: discord.Guild | None,
        limit: int,
    ) -> list[tuple[int, str, int]]:
        users = state.get("users", {})
        if not isinstance(users, dict):
            users = {}

        rows: list[tuple[int, str, int]] = []
        for rank, (user_id, points) in enumerate(ranked_points_users(state, field, limit), start=1):
            record = users.get(str(user_id), {})
            if not isinstance(record, dict):
                record = {}
            name = await self.resolve_display_name(guild, user_id, record)
            rows.append((rank, name, points))
        return rows

    async def build_leaderboard_message(
        self,
        state: dict,
        guild: discord.Guild | None,
        pcfg: dict,
    ) -> tuple[list[discord.Embed], list[discord.File], str]:
        leaderboard_cfg = pcfg.get("leaderboard", {})
        if not isinstance(leaderboard_cfg, dict):
            leaderboard_cfg = {}

        limit = int(leaderboard_cfg.get("limit", 10))
        text_color = int(leaderboard_cfg.get("text_color", 0x5865F2))
        voice_color = int(leaderboard_cfg.get("voice_color", 0x57F287))
        text_title = "Текст поинты"
        voice_title = "Войс поинты"

        if Image is None:
            embeds = [
                await self.build_leaderboard_embed(text_title, "text_points", state, guild, limit, text_color),
                await self.build_leaderboard_embed(voice_title, "voice_points", state, guild, limit, voice_color),
            ]
            signature = json.dumps([embed.to_dict() for embed in embeds], ensure_ascii=False, sort_keys=True)
            return embeds, [], signature

        text_rows = await self.leaderboard_rows("text_points", state, guild, limit)
        voice_rows = await self.leaderboard_rows("voice_points", state, guild, limit)
        assets = [
            (text_title, "text", "points-text.png", text_color, text_rows),
            (voice_title, "voice", "points-voice.png", voice_color, voice_rows),
        ]

        embeds: list[discord.Embed] = []
        files: list[discord.File] = []
        signature_payload: list[dict] = []

        for title, icon_kind, filename, color, rows in assets:
            png = render_points_leaderboard_png(rows, icon_kind=icon_kind, accent_color=color)
            file = discord.File(io.BytesIO(png), filename=filename)
            embed = discord.Embed(title=title, color=int(color))
            embed.set_image(url=f"attachment://{filename}")
            files.append(file)
            embeds.append(embed)
            signature_payload.append(
                {
                    "title": title,
                    "icon": icon_kind,
                    "color": int(color),
                    "rows": rows,
                    "image_version": 2,
                }
            )

        signature = json.dumps(signature_payload, ensure_ascii=False, sort_keys=True)
        return embeds, files, signature

    async def leaderboard_loop(self) -> None:
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                cfg = load_json(config_path())
                pcfg = points_cfg(cfg)
                leaderboard_cfg = pcfg.get("leaderboard", {})
                if pcfg.get("enabled", False) and isinstance(leaderboard_cfg, dict) and leaderboard_cfg.get("enabled", True):
                    await self.sync_leaderboard(cfg)
                    sleep_seconds = max(2.0, float(leaderboard_cfg.get("update_seconds", 5)))
                else:
                    sleep_seconds = 30.0
            except Exception as e:
                print(f"[points] leaderboard loop failed: {e!r}")
                sleep_seconds = 10.0

            await asyncio.sleep(sleep_seconds)

    async def sync_leaderboard(self, cfg: dict) -> None:
        pcfg = points_cfg(cfg)
        leaderboard_cfg = pcfg.get("leaderboard", {})
        channel_id = int(leaderboard_cfg.get("channel_id", 0) or 0)
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            print(f"[points] leaderboard channel {channel_id} is not a text channel")
            return

        state_path = points_state_path(cfg)
        async with self._lock:
            state = load_state(state_path)
            message_id = int(state.get("leaderboard_message_id", 0) or 0)

        embeds, files, signature = await self.build_leaderboard_message(state, channel.guild, pcfg)
        if not self._leaderboard_dirty and signature == self._leaderboard_signature:
            return

        message = self._leaderboard_message
        if message is None or message.channel.id != channel.id:
            message = None
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    message = None

        if message is None:
            message = await channel.send(
                embeds=embeds,
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            async with self._lock:
                state = load_state(state_path)
                state["leaderboard_message_id"] = int(message.id)
                save_points_state(state_path, state)
            print(f"[points] Created leaderboard message id={message.id} in channel={channel.name}")
        else:
            await message.edit(
                embeds=embeds,
                attachments=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        self._leaderboard_message = message
        self._leaderboard_signature = signature
        self._leaderboard_dirty = False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PointsCog(bot))
