import copy
import json
from typing import List, Optional

import discord

from ..settings import load_json

def participant_word(count: int) -> str:
    value = abs(int(count))
    last_two = value % 100
    last_one = value % 10

    if 11 <= last_two <= 14:
        return "\u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432"
    if last_one == 1:
        return "\u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a"
    if 2 <= last_one <= 4:
        return "\u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u0430"
    return "\u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432"

def replace_status_variables(
    text: str,
    count: int,
    status: str,
    count_placeholder: str,
    version: Optional[str] = None,
) -> str:
    word_placeholder = (
        "[\u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432 / "
        "\u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u0430 / "
        "\u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a \u0432 "
        "\u0437\u0430\u0432\u0438\u0441\u0438\u043c\u043e\u0441\u0442\u0438 "
        "\u043e\u0442 \u043f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u043e\u0433\u043e "
        "\u0433\u043e\u0432\u043e\u0440\u0435\u043d\u0438\u044f \u043f\u043e "
        "\u0447\u0438\u0441\u043b\u0443 \u043a\u043e\u043b-\u0432\u0430 "
        "\u0438\u0433\u0440\u043e\u043a\u043e\u0432]"
    )
    return (
        text.replace(count_placeholder, str(int(count)))
        .replace(word_placeholder, participant_word(count))
        .replace("[\u0441\u0442\u0430\u0442\u0443\u0441]", status)
        .replace("[\u0432\u0435\u0440\u0441\u0438\u044f]", str(version or "Unknown"))
    )

def status_online_block(count: int, heading: str, trailing_spacer: bool) -> str:
    block = f"### **{heading}**\n{int(count)} {participant_word(count)}\n"
    if trailing_spacer:
        block += "\u2800\n"
    return block

def load_status_embed_template(path: str) -> List[dict]:
    data = load_json(path)
    raw_embeds = data.get("embeds") if isinstance(data, dict) else None
    if not isinstance(raw_embeds, list) or not raw_embeds:
        raise ValueError(f"{path} must contain a non-empty `embeds` list.")
    if len(raw_embeds) > 10:
        raise ValueError(f"{path} contains more than 10 embeds.")
    if not all(isinstance(embed, dict) for embed in raw_embeds):
        raise ValueError(f"{path} contains an invalid embed.")
    return copy.deepcopy(raw_embeds)

def sanitize_status_embed_dict(raw_embed: dict) -> dict:
    embed = copy.deepcopy(raw_embed)
    for key in ("image", "thumbnail"):
        media = embed.get(key)
        if isinstance(media, dict) and not str(media.get("url", "")).strip():
            embed.pop(key, None)
    return embed

def status_embeds_from_dicts(raw_embeds: List[dict]) -> List[discord.Embed]:
    return [
        discord.Embed.from_dict(sanitize_status_embed_dict(raw_embed))
        for raw_embed in raw_embeds
    ]

def render_smp_status_embeds(template_path: str, snapshot: dict) -> List[discord.Embed]:
    raw_embeds = load_status_embed_template(template_path)
    components = snapshot.get("components", [])
    if len(raw_embeds) < 2 + len(components):
        raise ValueError(f"{template_path} does not have enough backend embeds.")

    total_players = sum(int(component.get("players", 0)) for component in components)
    summary = raw_embeds[1]
    summary_description = replace_status_variables(
        str(summary.get("description", "")),
        total_players,
        str(snapshot["status"]),
        "[\u043a\u043e\u043b-\u0432\u043e \u0438\u0433\u0440\u043e\u043a\u043e\u0432 "
        "\u0441\u043e \u0432\u0441\u0435\u0445 \u0431\u0435\u043a\u0435\u043d\u0434 "
        "\u0441\u0435\u0440\u0432\u0435\u0440\u043e\u0432]",
        str(snapshot.get("version") or "Unknown"),
    )
    if any(component["status"] == ":green_circle:" for component in components):
        summary_description = summary_description.replace(
            "[\u043e\u0431\u0449\u0438\u0439 \u043e\u043d\u043b\u0430\u0439\u043d]",
            status_online_block(total_players, "\u041e\u0431\u0449\u0438\u0439 \u043e\u043d\u043b\u0430\u0439\u043d", True),
        )
    else:
        summary_description = summary_description.replace(
            "[\u043e\u0431\u0449\u0438\u0439 \u043e\u043d\u043b\u0430\u0439\u043d]",
            "",
        )
    summary["description"] = summary_description

    for index, component in enumerate(components, start=2):
        backend = raw_embeds[index]
        backend_description = replace_status_variables(
            str(backend.get("description", "")),
            int(component.get("players", 0)),
            str(component["status"]),
            "[\u043a\u043e\u043b-\u0432\u043e \u0438\u0433\u0440\u043e\u043a\u043e\u0432 "
            "\u0441 \u044d\u0442\u043e\u0433\u043e \u0431\u0435\u043a\u0435\u043d\u0434\u0430]",
        )
        if component["status"] == ":green_circle:":
            backend_description = backend_description.replace(
                "[\u043e\u043d\u043b\u0430\u0439\u043d]",
                status_online_block(
                    int(component.get("players", 0)),
                    "\u041e\u043d\u043b\u0430\u0439\u043d",
                    False,
                ),
            )
        else:
            backend_description = backend_description.replace("[\u043e\u043d\u043b\u0430\u0439\u043d]", "")
        backend["description"] = backend_description

    if components and all(component["status"] == ":red_circle:" for component in components):
        raw_embeds = raw_embeds[:2]
    else:
        raw_embeds = raw_embeds[: 2 + len(components)]

    return status_embeds_from_dicts(raw_embeds)

def render_bm_status_embeds(template_path: str, snapshot: dict) -> List[discord.Embed]:
    raw_embeds = load_status_embed_template(template_path)
    if len(raw_embeds) < 2:
        raise ValueError(f"{template_path} does not have a status embed.")

    status = str(snapshot["status"])
    status_index = next(
        (
            index
            for index, raw_embed in enumerate(raw_embeds)
            if "[\u0441\u0442\u0430\u0442\u0443\u0441]" in str(raw_embed.get("description", ""))
        ),
        None,
    )
    if status_index is None:
        raise ValueError(f"{template_path} does not contain a [status] placeholder.")

    status_embed = raw_embeds[status_index]
    status_description = replace_status_variables(
        str(status_embed.get("description", "")),
        int(snapshot.get("players", 0)),
        status,
        "[\u043a\u043e\u043b-\u0432\u043e \u0438\u0433\u0440\u043e\u043a\u043e\u0432]",
        str(snapshot.get("version") or "Unknown"),
    )
    if status == ":green_circle:":
        status_description = status_description.replace(
            "[\u043e\u043d\u043b\u0430\u0439\u043d]",
            status_online_block(
                int(snapshot.get("players", 0)),
                "\u041e\u043d\u043b\u0430\u0439\u043d",
                True,
            ),
        )
    else:
        status_description = status_description.replace("[\u043e\u043d\u043b\u0430\u0439\u043d]", "")
    status_embed["description"] = status_description

    if status in (":red_circle:", ":yellow_circle:"):
        color = int(raw_embeds[-1].get("color", raw_embeds[0].get("color", 0)))
        raw_embeds.append(
            {
                "description": bm_control_text(status),
                "color": color,
            }
        )

    return status_embeds_from_dicts(raw_embeds)

def status_payload_signature(
    content: Optional[str],
    embeds: List[discord.Embed],
    control_status: Optional[str],
) -> str:
    payload = {
        "content": content or "",
        "embeds": [embed.to_dict() for embed in embeds],
        "control_status": control_status or "",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def bm_control_text(status: Optional[str]) -> str:
    start_text = (
        "**\u2022  \u0421\u0442\u0430\u0440\u0442**\n"
        "\u0417\u0430\u043f\u0443\u0441\u043a\u0430\u0435\u0442 \u0441\u0435\u0440\u0432\u0435\u0440. \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 \u044d\u0442\u0443 \u043a\u043d\u043e\u043f\u043a\u0443 \u0438\u0441\u043a\u043b\u044e\u0447\u0438\u0442\u0435\u043b\u044c\u043d\u043e \u043f\u0440\u0438 \u0430\u0432\u0430\u0440\u0438\u0439\u043d\u043e\u043c, \u043d\u0435\u0437\u0430\u043f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c \u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0438."
    )
    reboot_text = (
        "**\u2022  \u0420\u0435\u0441\u0442\u0430\u0440\u0442 \u0441\u0438\u0441\u0442\u0435\u043c\u044b**\n"
        "\u041f\u043e\u043b\u043d\u043e\u0441\u0442\u044c\u044e \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0441\u043a\u0430\u0435\u0442 \u041e\u0421 \u0441\u0435\u0440\u0432\u0435\u0440\u0430. \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 \u0442\u043e\u043b\u044c\u043a\u043e \u0432 \u0442\u043e\u043c \u0441\u043b\u0443\u0447\u0430\u0435, \u0435\u0441\u043b\u0438 \u043a\u043d\u043e\u043f\u043a\u0430 \u00ab\u0421\u0442\u0430\u0440\u0442\u00bb \u043d\u0435 \u0434\u0430\u043b\u0430 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0430 \u0432 \u0442\u0435\u0447\u0435\u043d\u0438\u0438 2 \u043c\u0438\u043d\u0443\u0442. \u0421\u043f\u0443\u0441\u0442\u044f 90 \u0441\u0435\u043a\u0443\u043d\u0434, \u043f\u043e\u0441\u043b\u0435 \u043d\u0430\u0436\u0430\u0442\u0438\u044f \u00ab\u0420\u0435\u0441\u0442\u0430\u0440\u0442 \u0441\u0438\u0441\u0442\u0435\u043c\u044b\u00bb, \u043f\u0440\u043e\u0431\u0443\u0439 \u00ab\u0421\u0442\u0430\u0440\u0442\u00bb."
    )
    warning_text = "**!** *\u042d\u0442\u043e\u0442 \u0444\u0443\u043d\u043a\u0446\u0438\u043e\u043d\u0430\u043b \u043f\u0440\u0435\u0434\u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d \u0438\u0441\u043a\u043b\u044e\u0447\u0438\u0442\u0435\u043b\u044c\u043d\u043e \u0434\u043b\u044f \u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u043e\u0433\u043e \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f \u0440\u0430\u0431\u043e\u0442\u044b \u0441\u0435\u0440\u0432\u0435\u0440\u0430.*"

    if status == ":red_circle:":
        return f"{start_text}\n{reboot_text}\n\u2800\n{warning_text}"
    if status == ":yellow_circle:":
        return f"{reboot_text}\n\u2800\n{warning_text}"
    return "\u200b"
