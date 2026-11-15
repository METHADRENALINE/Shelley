from __future__ import annotations

import asyncio
from functools import lru_cache
from io import BytesIO
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
from .models import CUSTOM_EMOJI, MEDAL_COLOR, MeowError, PointSource, Reward, number

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


def _title_lines(draw: ImageDraw.ImageDraw, title: str, width: int) -> tuple[list[str], Font]:
    for size in (56, 50, 44, 38):
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
    font = _font(38, bold=True)
    return [fitted_text(draw, title, font, width)], font


def _accent(rgb: int) -> tuple[int, int, int]:
    red, green, blue = (int(0.64 * ((rgb >> shift) & 255) + 0.36 * 255) for shift in (16, 8, 0))
    return red, green, blue


def _paint(
    reward: Reward,
    source: PointSource,
    accent: int,
    order: int | None,
    sprite: bytes | None,
) -> bytes:
    highlight = f"#{MEDAL_COLOR:06x}" if order is None else _accent(reward.color or accent)
    card = Image.new("RGBA", (1280, 296), (23, 26, 30, 255))
    draw = ImageDraw.Draw(card)
    draw.rectangle((0, 0, 8, card.height), fill=highlight)
    draw.rounded_rectangle((40, 40, 256, 256), radius=29, fill=(33, 38, 43))
    draw.rounded_rectangle((40, 40, 256, 256), radius=29, outline=(63, 67, 72), width=2)
    emoji = _emoji_art(reward.emoji, sprite)
    factor = 148 / max(emoji.size)
    emoji = emoji.resize(
        (max(1, round(emoji.width * factor)), max(1, round(emoji.height * factor))),
        Image.Resampling.LANCZOS,
    )
    card.alpha_composite(emoji, (148 - emoji.width // 2, 148 - emoji.height // 2))
    left = 298
    label = "\u041c\u0415\u0414\u0410\u041b\u042c \u0421\u0415\u0417\u041e\u041d\u0410" if order is None else str(order)
    draw.text((left, 54), label, font=_font(23, bold=True), fill=highlight, anchor="lt")
    lines, font = _title_lines(draw, reward.name, card.width - left - 50)
    for index, line in enumerate(lines):
        draw.text((left, 97 + index * 53), line, font=font, fill=(247, 248, 250), anchor="lt")
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
        self._render: dict[tuple[Reward, PointSource, int, int | None], bytes] = {}
        self._lock = asyncio.Lock()

    async def cards(
        self,
        rewards: tuple[Reward, ...],
        source: PointSource,
        accent: int,
        medal_key: str | None,
    ) -> list[bytes]:
        async with self._lock:
            results: list[bytes] = []
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as session:
                for index, reward in enumerate(rewards, 1):
                    order = None if reward.key == medal_key else index
                    key = (reward, source, accent, order)
                    image = self._render.get(key)
                    if image is None:
                        match = CUSTOM_EMOJI.fullmatch(reward.emoji)
                        sprite = await _fetch_emoji(session, match[1]) if match else None
                        image = await asyncio.to_thread(_paint, reward, source, accent, order, sprite)
                        self._render[key] = image
                        if len(self._render) > 32:
                            self._render.pop(next(iter(self._render)))
                    results.append(image)
            return results
