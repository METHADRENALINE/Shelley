from __future__ import annotations

from datetime import UTC, datetime

import discord

from ..renderers.status import status_online_block, status_payload_signature
from .config import ScpSlConfig
from .protocol import Snapshot


def escape(value: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


def render_status(config: ScpSlConfig, snapshot: Snapshot | None, now: datetime | None = None) -> list[discord.Embed]:
    now = datetime.now(UTC) if now is None else now
    embeds = []
    if config.logo_url:
        embeds.append(discord.Embed(color=config.color).set_image(url=config.logo_url))
    description = f"### IP\n{escape(config.display_address)}\n\u2800\n"
    names = snapshot.online_names if snapshot is not None else []
    description += status_online_block(len(names), "\u041e\u043d\u043b\u0430\u0439\u043d", True, names)
    if snapshot is not None and snapshot.online_names:
        if snapshot.voting(now):
            mode = "*\u0412\u0435\u0434\u0451\u0442\u0441\u044f \u0433\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435*"
        else:
            mode = escape(snapshot.mode_name_ru) if snapshot.mode_name_ru is not None else ""
        if mode:
            description += f"### **\u0420\u0435\u0436\u0438\u043c**\n{mode}\n\u2800\n"
    status = ":green_circle:" if snapshot is not None else ":red_circle:"
    description += f"### **\u0421\u0442\u0430\u0442\u0443\u0441** {status}\n"
    if snapshot is not None and snapshot.voting(now):
        description += (
            "\u2800\n### **\u0413\u043e\u043b\u043e\u0441\u043e\u0432\u0430\u043d\u0438\u0435 \u0437\u0430 \u0440\u0435\u0436\u0438\u043c**"
        )
    embeds.append(discord.Embed(color=config.color, description=description))
    return embeds


def payload_signature(embeds: list[discord.Embed], view: discord.ui.View | None, content: str | None = None) -> str:
    controls = repr(view.to_components()) if view is not None else None
    return status_payload_signature(content, embeds, controls)
