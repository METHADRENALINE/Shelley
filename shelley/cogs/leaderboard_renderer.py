from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

Image: Any = None
ImageDraw: Any = None
ImageFont: Any = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    pass


logger = logging.getLogger(__name__)


def load_points_font(size: int, *, bold: bool = False):
    if ImageFont is None:
        return None
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
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


def compact_points_text(points: int) -> str:
    value = abs(int(points))
    sign = "-" if int(points) < 0 else ""
    if value < 1_000_000:
        return str(int(points))
    units = (
        (1_000_000_000_000_000, "Q"),
        (1_000_000_000_000, "T"),
        (1_000_000_000, "B"),
        (1_000_000, "M"),
    )
    for divisor, suffix in units:
        if value >= divisor:
            number = value / divisor
            formatted = f"{number:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{formatted}{suffix}"
    return str(int(points))


def fit_points(draw, points: int, font, max_width: int, scale: int):
    raw = str(int(points))
    compact = compact_points_text(int(points))
    fonts = [
        font,
        load_points_font(23 * scale, bold=True),
        load_points_font(21 * scale, bold=True),
        load_points_font(19 * scale, bold=True),
    ]
    for candidate in (raw, compact):
        for candidate_font in fonts:
            if candidate_font is not None and text_width(draw, candidate, candidate_font) <= max_width:
                return candidate, candidate_font
    fallback_font = fonts[-1] or font
    return fitted_text(draw, compact, fallback_font, max_width), fallback_font


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
    draw.rounded_rectangle((cx - mic_w // 2, top, cx + mic_w // 2, top + mic_h), radius=max(5, mic_w // 2), fill=fill)
    arc_box = (x + int(size * 0.18), y + int(size * 0.30), x + int(size * 0.82), y + int(size * 0.88))
    draw.arc(arc_box, 0, 180, fill=fill, width=stem_w)
    draw.line((cx, y + int(size * 0.78), cx, y + size), fill=fill, width=stem_w)
    draw.line((cx - int(size * 0.22), y + size, cx + int(size * 0.22), y + size), fill=fill, width=stem_w)


def points_asset_path(filename: str) -> Path | None:
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
    except OSError:
        logger.exception("cannot load leaderboard icon", extra={"path": str(path)})
        return None


def render_points_leaderboard_png(
    rows: list[tuple[int, str, int]], *, icon_kind: str, accent_color: int, placeholder_text: str = ""
) -> bytes:
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is required for image leaderboards.")
    output_scale = 2
    scale = 4
    width = 720 * scale
    row_h = 64 * scale
    gap = 10 * scale
    is_placeholder = not rows
    visible_rows = rows or [(1, str(placeholder_text), 0)]
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
    icon_fill = ((accent_color >> 16) & 255, (accent_color >> 8) & 255, accent_color & 255, 255)
    if icon_fill == (0, 0, 0, 255):
        icon_fill = (248, 249, 252, 255)
    icon_size = 34 * scale
    icon_image = load_points_icon(icon_kind, icon_size)
    icon_x = 523 * scale
    icon_y_offset = 15 * scale
    icon_dot_x = 504 * scale
    points_left = 590 * scale
    points_right = 690 * scale
    points_width = points_right - points_left
    for index, (rank, name, points) in enumerate(visible_rows):
        top = index * (row_h + gap)
        draw.rounded_rectangle((0, top, width, top + row_h), radius=14 * scale, fill=row_color)
        rank_color = rank_colors.get(rank, text_color)
        rank_text = f"#{rank}"
        rank_x = 30 * scale
        rank_y = top + 15 * scale
        draw.text((rank_x, rank_y), rank_text, fill=rank_color, font=rank_font)
        dot_x = rank_x + text_width(draw, rank_text, rank_font) + 8 * scale
        draw.text((dot_x, top + 18 * scale), "•", fill=muted_dot, font=dot_font)
        name_x = dot_x + 18 * scale
        max_name_width = icon_dot_x - 18 * scale - name_x
        clean_name = str(name).replace("\n", " ").strip()
        if not clean_name and not is_placeholder:
            clean_name = str(rank)
        draw.text((name_x, top + 16 * scale), fitted_text(draw, clean_name, name_font, max_name_width), fill=text_color, font=name_font)
        draw.text((icon_dot_x, top + 16 * scale), "•", fill=muted_dot, font=dot_font)
        icon_y = top + icon_y_offset
        if icon_image is not None:
            image.alpha_composite(icon_image, (icon_x, icon_y))
        elif icon_kind == "voice":
            draw_microphone_icon(draw, icon_x, icon_y, 28 * scale, (248, 249, 252, 255))
        else:
            draw_chat_icon(draw, icon_x, icon_y, 30 * scale, (248, 249, 252, 255))
        points_text, row_points_font = fit_points(draw, int(points), points_font, points_width, scale)
        draw.text(
            (points_right - text_width(draw, points_text, row_points_font), top + 18 * scale),
            points_text,
            fill=text_color,
            font=row_points_font,
        )
    if scale != output_scale:
        image = image.resize((width * output_scale // scale, height * output_scale // scale), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
