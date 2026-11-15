from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from io import BytesIO

import aiohttp
import discord
import regex
from PIL import Image, ImageDraw

from ..cogs.leaderboard_renderer import fitted_text
from .art import _emoji_art, _fetch_emoji, _font
from .models import CUSTOM_EMOJI, FINAL_STAGE, ArchivedMedal, MeowError, era_title

FINAL_ACT = "\u0424\u0438\u043d\u0430\u043b\u044c\u043d\u044b\u0439 \u0430\u043a\u0442"
SEASON = "\u0421\u0435\u0437\u043e\u043d"
WHITE = (244, 246, 249)
MUTED = (181, 194, 200)
SURFACE = (35, 42, 46)


def archive_filename(era: int) -> str:
    return "meow-era-i.png" if era == 1 else f"meow-era-{era}.png"


def archive_embed(era: int = 1) -> discord.Embed:
    return discord.Embed(color=0xFFFFFF).set_image(url=f"attachment://{archive_filename(era)}")


def archive_label(row: ArchivedMedal) -> str:
    return f"{SEASON} {row.stage}" if row.stage < FINAL_STAGE else FINAL_ACT


def _icon(emoji: str, size: int, sprite: bytes | None = None) -> Image.Image:
    image = _emoji_art(emoji, sprite)
    bounds = image.getchannel("A").getbbox()
    if bounds is None:
        raise MeowError("An archive icon is empty.")
    image = image.crop(bounds)
    scale = size / max(image.size)
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )


def render_archive(rows: Sequence[ArchivedMedal], images: Mapping[str, bytes], *, era: int = 1) -> bytes:
    stages = [row.stage for row in rows]
    if not stages or stages != sorted(set(stages)) or not all(1 <= stage <= FINAL_STAGE for stage in stages):
        raise MeowError("An era board needs distinct stages in order.")
    board = Image.new("RGBA", (768, 120 + 88 * len(rows)), (19, 24, 27, 255))
    draw = ImageDraw.Draw(board)
    draw.text((28, 45), era_title(era), fill=WHITE, font=_font(48, bold=True), anchor="lm")
    draw.line((28, 80, 740, 80), fill=(91, 108, 111), width=1)
    label_font = _font(21)
    name_font = _font(26, bold=True)
    for index, row in enumerate(rows):
        top = 100 + index * 88
        center = top + 39
        draw.rounded_rectangle(
            (28, top, 740, top + 78),
            radius=12,
            fill=SURFACE,
        )
        if row.season_emoji and (sprite := images.get(row.season_emoji)) is not None:
            icon = _icon(row.season_emoji, 48, sprite)
            board.alpha_composite(icon, (68 - icon.width // 2, center - icon.height // 2))
        draw.text(
            (108, top + 22),
            archive_label(row),
            fill=MUTED,
            font=label_font,
            anchor="lm",
        )
        draw.text(
            (108, top + 51),
            fitted_text(draw, row.username, name_font, 484),
            fill=WHITE,
            font=name_font,
            anchor="lm",
        )
        if CUSTOM_EMOJI.fullmatch(row.medal_emoji):
            medal_icons = [_icon(row.medal_emoji, 44, images[row.medal_emoji])] if row.medal_emoji in images else [_icon("\U0001f3c5", 44)]
        else:
            medal_icons = [_icon(value, 44) for value in regex.findall(r"\X", row.medal_emoji)]
        total = sum(icon.width for icon in medal_icons) + max(0, len(medal_icons) - 1) * 4
        left = 676 - total // 2
        for medal in medal_icons:
            board.alpha_composite(medal, (left, center - medal.height // 2))
            left += medal.width + 4
    output = BytesIO()
    board.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def archive_file(rows: Sequence[ArchivedMedal], *, era: int = 1) -> discord.File:
    icons = {
        value: match[1]
        for row in rows
        for value in (row.season_emoji, row.medal_emoji)
        if value and (match := CUSTOM_EMOJI.fullmatch(value))
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        downloaded = await asyncio.gather(
            *(_fetch_emoji(session, emoji_id) for emoji_id in icons.values()),
            return_exceptions=True,
        )
    images = {value: result for value, result in zip(icons, downloaded, strict=True) if isinstance(result, bytes)}
    data = await asyncio.to_thread(render_archive, rows, images, era=era)
    description = era_title(era) + "\n" + "\n".join(f"{archive_label(row)}: {row.username} {row.medal_emoji}" for row in rows)
    return discord.File(BytesIO(data), filename=archive_filename(era), description=description)
