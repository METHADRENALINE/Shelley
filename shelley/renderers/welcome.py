from typing import List, Optional

import discord

from ..settings import load_json

def load_welcome_message_payload(path: str) -> tuple[Optional[str], List[discord.Embed]]:
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValueError("welcome-msg.json must contain a JSON object.")

    content = data.get("content")
    if content is not None and not isinstance(content, str):
        content = str(content)

    raw_embeds = data.get("embeds", [])
    if raw_embeds is None:
        raw_embeds = []
    if not isinstance(raw_embeds, list):
        raise ValueError("welcome-msg.json field `embeds` must be a list.")
    if len(raw_embeds) > 10:
        raise ValueError("Discord allows up to 10 embeds per message.")

    attachments = data.get("attachments", [])
    if attachments:
        raise ValueError("welcome-msg.json attachments are not supported; use embed image URLs instead.")

    embeds: List[discord.Embed] = []
    for index, raw_embed in enumerate(raw_embeds):
        if not isinstance(raw_embed, dict):
            raise ValueError(f"welcome-msg.json embed #{index + 1} must be an object.")
        embeds.append(discord.Embed.from_dict(raw_embed))

    if (content is None or content == "") and not embeds:
        raise ValueError("welcome-msg.json must contain `content` or at least one embed.")

    return content, embeds
