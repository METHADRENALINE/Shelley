from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

import regex

from ..config import TrademarkGuildConfig
from .unicode_security import confusable_skeleton

CUSTOM_EMOJI_PATTERN = re.compile(r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{1,32}):(?P<id>\d{13,20})>")
CUSTOM_EMOJI_ALIAS_PATTERN = re.compile(r":(?P<name>[A-Za-z0-9_]{1,32}):")
MAX_DISCORD_NAME_PAYLOAD = 100
COMPARISON_KEY_VERSION = "uts39-17.0.0-v1"
DEFAULT_IGNORABLE_PATTERN = regex.compile(r"(?V1)\p{Default_Ignorable_Code_Point}")
EMOJI_SEQUENCE_PATTERN = regex.compile(
    r"(?V1)^(?:"
    r"[#*0-9]\ufe0f?\u20e3|"
    r"\p{Regional_Indicator}{2}|"
    r"\U0001f3f4\U000e0067\U000e0062"
    r"(?:"
    r"\U000e0065\U000e006e\U000e0067|"
    r"\U000e0073\U000e0063\U000e0074|"
    r"\U000e0077\U000e006c\U000e0073"
    r")\U000e007f|"
    r"\p{Extended_Pictographic}\ufe0f?(?:\p{Emoji_Modifier})?"
    r"(?:\u200d\p{Extended_Pictographic}\ufe0f?"
    r"(?:\p{Emoji_Modifier})?)*"
    r")$"
)
EMPTY_GLYPH_CHARACTERS = frozenset({"\u2800"})


class InvalidTrademarkName(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedTrademarkName:
    display: str
    key: str
    search_key: str
    visible_characters: int
    spaces: int


def resolve_custom_emoji_aliases(value: str, aliases: Mapping[str, str]) -> str:
    raw = str(value)

    def resolve_segment(segment: str) -> str:
        def replacement(match: re.Match[str]) -> str:
            candidate = aliases.get(match.group("name"))
            if candidate and CUSTOM_EMOJI_PATTERN.fullmatch(candidate):
                return candidate
            return match.group(0)

        return CUSTOM_EMOJI_ALIAS_PATTERN.sub(replacement, segment)

    parts: list[str] = []
    position = 0
    for match in CUSTOM_EMOJI_PATTERN.finditer(raw):
        parts.append(resolve_segment(raw[position : match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(resolve_segment(raw[position:]))
    return "".join(parts)


def _clean_spacing(value: str) -> str:
    output: list[str] = []
    pending_space = False
    for cluster in regex.findall(r"\X", unicodedata.normalize("NFC", value)):
        if cluster == " ":
            pending_space = bool(output)
            continue
        categories = [unicodedata.category(character) for character in cluster]
        if any(
            character.isspace() or category.startswith("Z") or character in EMPTY_GLYPH_CHARACTERS
            for character, category in zip(cluster, categories, strict=True)
        ):
            raise InvalidTrademarkName("Название содержит невидимый символ")
        if any(category in {"Cc", "Cs", "Co"} for category in categories):
            raise InvalidTrademarkName("Название содержит недопустимый символ")
        if categories[0].startswith("M"):
            raise InvalidTrademarkName("Название содержит невидимый символ")
        if (
            any(
                category == "Cf" or DEFAULT_IGNORABLE_PATTERN.fullmatch(character) is not None
                for character, category in zip(cluster, categories, strict=True)
            )
            and EMOJI_SEQUENCE_PATTERN.fullmatch(cluster) is None
        ):
            raise InvalidTrademarkName("Название содержит невидимый символ")
        if not any(not category.startswith(("C", "M", "Z")) for category in categories):
            raise InvalidTrademarkName("Название содержит невидимый символ")
        if pending_space:
            output.append(" ")
            pending_space = False
        output.append(cluster)
    return "".join(output).strip()


def _visible_character_count(value: str) -> int:
    count = 0
    position = 0
    for match in CUSTOM_EMOJI_PATTERN.finditer(value):
        count += len(regex.findall(r"\X", value[position : match.start()]))
        count += 1
        position = match.end()
    count += len(regex.findall(r"\X", value[position:]))
    return count


def _unicode_emoji_identity(cluster: str) -> str:
    return "-".join(f"{ord(character):x}" for character in cluster if character not in {"\ufe0e", "\ufe0f"})


def _text_parts(value: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    text: list[str] = []

    def flush_text() -> None:
        if not text:
            return
        parts.append(("text", confusable_skeleton("".join(text))))
        text.clear()

    for cluster in regex.findall(r"\X", value):
        if EMOJI_SEQUENCE_PATTERN.fullmatch(cluster) is None:
            text.append(cluster)
            continue
        flush_text()
        parts.append(("unicode-emoji", _unicode_emoji_identity(cluster)))
    flush_text()
    return parts


def _comparison_parts(value: str) -> list[tuple[str, str]]:
    position = 0
    parts: list[tuple[str, str]] = []
    for match in CUSTOM_EMOJI_PATTERN.finditer(value):
        parts.extend(_text_parts(value[position : match.start()]))
        parts.append(("custom-emoji", match.group("id")))
        position = match.end()
    parts.extend(_text_parts(value[position:]))
    return parts


def trademark_index_values(value: str) -> tuple[str, str]:
    parts = _comparison_parts(value)
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    search_parts: list[str] = []
    for kind, content in parts:
        if kind == "text":
            search_parts.append(content)
        elif kind == "custom-emoji":
            search_parts.append(f"\ufffcc{content}\ufffc")
        else:
            search_parts.append(f"\ufffcu{content}\ufffc")
    return f"{COMPARISON_KEY_VERSION}:{encoded}", "".join(search_parts)


def normalize_trademark_name(value: str, config: TrademarkGuildConfig) -> NormalizedTrademarkName:
    if "™" in str(value):
        raise InvalidTrademarkName("Символ ™ нельзя включать в название")
    display = _clean_spacing(str(value))
    if not display:
        raise InvalidTrademarkName("Название не может быть пустым")
    if len(display) > MAX_DISCORD_NAME_PAYLOAD:
        raise InvalidTrademarkName("Название слишком длинное для интерфейса Discord")
    spaces = display.count(" ")
    if spaces > config.max_spaces:
        raise InvalidTrademarkName(f"В названии может быть не больше {config.max_spaces} пробелов")
    visible_characters = _visible_character_count(display)
    if visible_characters < 1:
        raise InvalidTrademarkName("Название не может быть пустым")
    if visible_characters > config.max_name_characters:
        raise InvalidTrademarkName(f"Название может содержать не больше {config.max_name_characters} символов")
    comparison_key, search_key = trademark_index_values(display)
    return NormalizedTrademarkName(
        display=display,
        key=comparison_key,
        search_key=search_key,
        visible_characters=visible_characters,
        spaces=spaces,
    )


def parse_message_trademark(content: str, config: TrademarkGuildConfig) -> NormalizedTrademarkName:
    raw = str(content)
    if "\n" in raw or "\r" in raw:
        raise InvalidTrademarkName("Сообщение должно состоять из одной строки")
    stripped = raw.strip(" ")
    if stripped.count("™") != 1 or not stripped.endswith("™"):
        raise InvalidTrademarkName("Сообщение должно состоять из названия и одного символа ™ в конце")
    return normalize_trademark_name(stripped[:-1], config)
