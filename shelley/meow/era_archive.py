from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from io import BytesIO

import aiohttp
import discord
from PIL import Image, ImageDraw

from ..cogs.leaderboard_renderer import fitted_text
from .art import _fetch_emoji, _font, emoji_grid
from .models import (
    CUSTOM_EMOJI,
    FINAL_STAGE,
    ArchivedMedal,
    MeowError,
    emoji_parts,
    era_title,
)

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


def final_act_without_icon(row: ArchivedMedal) -> bool:
    return row.stage == FINAL_STAGE and not row.season_emoji


def render_archive(
    rows: Sequence[ArchivedMedal],
    images: Mapping[str, bytes],
    *,
    era: int = 1,
    era_emoji: str | None = None,
) -> bytes:
    stages = [row.stage for row in rows]
    if not stages or stages != sorted(set(stages)) or not all(1 <= stage <= FINAL_STAGE for stage in stages):
        raise MeowError("An era board needs distinct stages in order.")
    board = Image.new("RGBA", (768, 120 + 88 * len(rows)), (19, 24, 27, 255))
    draw = ImageDraw.Draw(board)
    title_x = 28
    if era_emoji and (era_emoji in images or not CUSTOM_EMOJI.fullmatch(era_emoji)):
        icon = emoji_grid(era_emoji, images, width=48, height=48, icon_size=48)
        board.alpha_composite(icon, (44, 21))
        title_x = 108
    draw.text((title_x, 45), era_title(era), fill=WHITE, font=_font(48, bold=True), anchor="lm")
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
        if row.season_emoji and row.season_emoji in images:
            icon = emoji_grid(row.season_emoji, images, width=48, height=48, icon_size=48)
            board.alpha_composite(icon, (44, center - 24))
        text_x = 44 if final_act_without_icon(row) else 108
        draw.text(
            (text_x, top + 22),
            archive_label(row),
            fill=MUTED,
            font=label_font,
            anchor="lm",
        )
        draw.text(
            (text_x, top + 51),
            fitted_text(draw, row.username, name_font, 592 - text_x),
            fill=WHITE,
            font=name_font,
            anchor="lm",
        )
        medal = emoji_grid(row.medal_emoji, images, width=120, height=64, icon_size=44)
        board.alpha_composite(medal, (616, center - 32))
    output = BytesIO()
    board.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def archive_file(rows: Sequence[ArchivedMedal], *, era: int = 1, era_emoji: str | None = None) -> discord.File:
    icons = {
        value: match[1]
        for row in rows
        for value in (*emoji_parts(row.medal_emoji), row.season_emoji)
        if value and (match := CUSTOM_EMOJI.fullmatch(value))
    }
    if era_emoji and (match := CUSTOM_EMOJI.fullmatch(era_emoji)):
        icons[era_emoji] = match[1]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), connector=aiohttp.TCPConnector(limit=8)) as session:
        downloaded = await asyncio.gather(
            *(_fetch_emoji(session, emoji_id) for emoji_id in icons.values()),
            return_exceptions=True,
        )
    images = {value: result for value, result in zip(icons, downloaded, strict=True) if isinstance(result, bytes)}
    data = await asyncio.to_thread(render_archive, rows, images, era=era, era_emoji=era_emoji)
    description = era_title(era) + "\n" + "\n".join(f"{archive_label(row)}: {row.username}" for row in rows)
    return discord.File(BytesIO(data), filename=archive_filename(era), description=description)
