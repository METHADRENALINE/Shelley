from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import lru_cache
from io import BytesIO
from math import ceil
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from ..cogs.leaderboard_renderer import (
    draw_chat_icon,
    draw_microphone_icon,
    fitted_text,
    load_points_font,
    load_points_icon,
)
from .models import (
    CUSTOM_EMOJI,
    MEDAL_COLOR,
    MEDAL_SYMBOL,
    MeowError,
    PointSource,
    Reward,
    emoji_parts,
    number,
)

MAX_EMOJI_BYTES = 512 * 1024
EMOJI_FONTS = (
    ("C:/Windows/Fonts/seguiemj.ttf", 120),
    ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109),
    ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 120),
)
EMOJI_COLOR = (241, 243, 246, 255)
Font = ImageFont.ImageFont | ImageFont.FreeTypeFont


@lru_cache(maxsize=8)
def _font(size: int, *, bold: bool = False) -> Font:
    return load_points_font(size, bold=bold) or ImageFont.load_default()


@lru_cache(maxsize=1)
def _emoji_font() -> ImageFont.FreeTypeFont:
    for path, size in EMOJI_FONTS:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    raise MeowError("A color emoji font is required on the Shelley host.")


def _emoji_art(emoji: str, sprite: bytes | None) -> Image.Image:
    if sprite is not None:
        try:
            with Image.open(BytesIO(sprite)) as image:
                if max(image.size) > 512:
                    raise MeowError("That emoji image is too large.")
                return image.convert("RGBA")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise MeowError("Discord did not return a readable emoji image.") from exc
    font = _emoji_font()
    canvas = Image.new("RGBA", (360, 220))
    draw = ImageDraw.Draw(canvas)
    box = draw.textbbox((0, 0), emoji, font=font, embedded_color=True)
    draw.text((-box[0] + 10, -box[1] + 10), emoji, font=font, fill=EMOJI_COLOR, embedded_color=True)
    bounds = canvas.getbbox()
    if bounds is None:
        raise MeowError("That emoji could not be drawn.")
    return canvas.crop(bounds)


def emoji_grid(
    value: str,
    sprites: Mapping[str, bytes] | None,
    *,
    width: int,
    height: int,
    icon_size: int,
) -> Image.Image:
    parts = emoji_parts(value)
    art = Image.new("RGBA", (width, height))
    if not parts:
        return art
    count = len(parts)
    cell_limit = icon_size + 4
    cell, columns = max(
        (min(cell_limit, width // columns, height // ceil(count / columns)), columns) for columns in range(1, min(count, width) + 1)
    )
    size = min(icon_size, max(1, cell - max(2, cell // 12)))
    if count == 1:
        size = icon_size
    icons: dict[str, Image.Image] = {}
    for part in set(parts):
        sprite = sprites.get(part) if sprites else None
        glyph = MEDAL_SYMBOL if sprite is None and CUSTOM_EMOJI.fullmatch(part) else part
        icon = _emoji_art(glyph, sprite)
        bounds = icon.getchannel("A").getbbox()
        if bounds is None:
            raise MeowError("An emoji image is empty.")
        icon = icon.crop(bounds)
        scale = size / max(icon.size)
        icons[part] = icon.resize(
            (max(1, round(icon.width * scale)), max(1, round(icon.height * scale))),
            Image.Resampling.LANCZOS,
        )
    top = (height - ceil(count / columns) * cell) // 2
    for start in range(0, count, columns):
        row = parts[start : start + columns]
        left = (width - len(row) * cell) // 2
        for index, part in enumerate(row):
            icon = icons[part]
            art.alpha_composite(
                icon,
                (
                    left + index * cell + (cell - icon.width) // 2,
                    top + start // columns * cell + (cell - icon.height) // 2,
                ),
            )
    return art


def _title_lines(draw: ImageDraw.ImageDraw, title: str, width: int, *, compact: bool = False) -> tuple[list[str], Font]:
    sizes = (44, 42, 38, 34) if compact else (56, 50, 44, 38)
    for size in sizes:
        font = _font(size, bold=True)
        lines: list[str] = []
        current = ""
        for word in title.split():
            combined = f"{current} {word}".strip()
            if current and draw.textlength(combined, font=font) > width:
                lines.append(current)
                current = word
            else:
                current = combined
        lines.append(current)
        if len(lines) <= 2 and all(draw.textlength(line, font=font) <= width for line in lines):
            return lines, font
    font = _font(sizes[-1], bold=True)
    return [fitted_text(draw, title, font, width)], font


def _accent(rgb: int) -> tuple[int, int, int]:
    red, green, blue = (int(0.64 * ((rgb >> shift) & 255) + 0.36 * 255) for shift in (16, 8, 0))
    return red, green, blue


def _paint(
    reward: Reward,
    source: PointSource,
    accent: int,
    order: int | None,
    sprites: Mapping[str, bytes] | None,
    winner_username: str | None = None,
) -> bytes:
    highlight = f"#{MEDAL_COLOR:06x}" if order is None else _accent(reward.color or accent)
    card = Image.new("RGBA", (1280, 296), (23, 26, 30, 255))
    draw = ImageDraw.Draw(card)
    draw.rectangle((0, 0, 8, card.height), fill=highlight)
    draw.rounded_rectangle((40, 40, 256, 256), radius=29, fill=(33, 38, 43))
    draw.rounded_rectangle((40, 40, 256, 256), radius=29, outline=(63, 67, 72), width=2)
    card.alpha_composite(emoji_grid(reward.emoji, sprites, width=180, height=180, icon_size=148), (58, 58))
    left = 298
    label = "\u041c\u0415\u0414\u0410\u041b\u042c \u0421\u0415\u0417\u041e\u041d\u0410" if order is None else str(order)
    draw.text((left, 54), label, font=_font(23, bold=True), fill=highlight, anchor="lt")
    width = card.width - left - 50
    lines, font = _title_lines(draw, reward.name, width, compact=winner_username is not None)
    for index, line in enumerate(lines):
        y = 94 + index * 44 if winner_username else 97 + index * 53
        draw.text((left, y), line, font=font, fill=(247, 248, 250), anchor="lt")
    if winner_username:
        winner_font = _font(24, bold=True)
        draw.text(
            (left, 153 if len(lines) == 1 else 183),
            fitted_text(draw, winner_username, winner_font, width),
            font=winner_font,
            fill=(181, 194, 200),
            anchor="lt",
        )
    draw.line((left, 212, 1230, 212), fill=(57, 62, 68), width=2)
    points_icon = load_points_icon(source, 30)
    if points_icon is not None:
        card.alpha_composite(points_icon, (left, 231))
    elif source == "voice":
        draw_microphone_icon(draw, left, 231, 28, EMOJI_COLOR)
    else:
        draw_chat_icon(draw, left, 232, 29, EMOJI_COLOR)
    draw.text(
        (left + 45, 234),
        number(reward.points),
        font=_font(32, bold=True),
        fill=(238, 241, 245),
        anchor="lt",
    )
    output = BytesIO()
    card.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def _fetch_emoji(session: aiohttp.ClientSession, emoji_id: str) -> bytes:
    try:
        async with session.get(
            f"https://cdn.discordapp.com/emojis/{emoji_id}.png?size=128",
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise MeowError("Shelley could not load that server emoji.")
            content = bytearray()
            async for chunk in response.content.iter_chunked(16 * 1024):
                content.extend(chunk)
                if len(content) > MAX_EMOJI_BYTES:
                    raise MeowError("That emoji image is too large.")
            return bytes(content)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise MeowError("The Discord emoji service is unavailable. Try again.") from exc


class CardRenderer:
    def __init__(self) -> None:
        self._render: dict[tuple[Reward, PointSource, int, int | None, str | None], bytes] = {}
        self._lock = asyncio.Lock()

    async def cards(
        self,
        rewards: tuple[Reward, ...],
        source: PointSource,
        accent: int,
        medal_key: str | None,
        winner_username: str | None = None,
    ) -> list[bytes]:
        async with self._lock:
            results: list[bytes] = []
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=6),
                connector=aiohttp.TCPConnector(limit=8),
            ) as session:
                for index, reward in enumerate(rewards, 1):
                    order = None if reward.key == medal_key else index
                    winner = winner_username if order is None else None
                    key = (reward, source, accent, order, winner)
                    image = self._render.get(key)
                    if image is None:
                        ids = {part: match[1] for part in emoji_parts(reward.emoji) if (match := CUSTOM_EMOJI.fullmatch(part))}
                        sprites = dict(
                            zip(
                                ids,
                                await asyncio.gather(*(_fetch_emoji(session, emoji_id) for emoji_id in ids.values())),
                                strict=True,
                            )
                        )
                        image = await asyncio.to_thread(_paint, reward, source, accent, order, sprites, winner)
                        self._render[key] = image
                        if len(self._render) > 32:
                            self._render.pop(next(iter(self._render)))
                    results.append(image)
            return results
