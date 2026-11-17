from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import uuid4

import regex
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

PointSource = Literal["text", "voice"]
SeasonState = Literal["preparing", "active", "awarding", "finished", "cancelled"]
MAX_RANKS = 9
MAX_SHOWCASE_TEXT = 3600
MAX_POINTS = 1_000_000_000
MAX_REWARD_NAME_LENGTH = 70
MAX_BANNER_URL_LENGTH = 2048
MAX_MEDAL_EMOJI_LENGTH = 512
MAX_ROLE_NAME_LENGTH = 100
MEDAL_COLOR = 0xFF6565
MEDAL_SYMBOL = "\U0001f3c5"
DEFAULT_ACCENT = 0x408871
FINAL_STAGE = 6
CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:([0-9]{17,20})>")
EMOJI_PART = regex.compile(rf"{CUSTOM_EMOJI.pattern}|\X")
EMOJI_ELEMENT = r"[\p{Emoji}&&\p{Extended_Pictographic}]\ufe0f?\p{Emoji_Modifier}?"
ROLE_EMOJI = regex.compile(
    rf"(?V1)(?:\p{{Regional_Indicator}}{{2}}|[#*0-9]\ufe0f?\u20e3|"
    rf"\U0001f3f4[\U000e0020-\U000e007e]+\U000e007f|"
    rf"{EMOJI_ELEMENT}(?:\u200d{EMOJI_ELEMENT})*)"
)
TEXT_STYLE_EMOJI = regex.compile(
    r"(?V1)[[\p{Emoji}&&\p{Extended_Pictographic}]--\p{Emoji_Presentation}]"
    r"(?!\ufe0f|\p{Emoji_Modifier})"
)


class MeowError(ValueError):
    pass


def emoji_presentation(value: str) -> str:
    normalized = value.replace("\ufe0e", "")
    if not ROLE_EMOJI.fullmatch(normalized):
        raise MeowError("Choose one standard emoji.")
    if normalized.endswith("\u20e3"):
        return normalized[0] + "\ufe0f\u20e3"
    return TEXT_STYLE_EMOJI.sub(lambda match: match[0] + "\ufe0f", normalized)


def validate_emoji(value: str) -> str:
    return value if CUSTOM_EMOJI.fullmatch(value) else emoji_presentation(value)


def emoji_parts(value: str) -> tuple[str, ...]:
    return tuple(match[0] for match in EMOJI_PART.finditer(value) if not match[0].isspace())


def emoji_sequence(value: str, *, allow_custom: bool) -> str:
    parse = validate_emoji if allow_custom else emoji_presentation
    try:
        parts = emoji_parts(value)
        if parts:
            return "".join(map(parse, parts))
    except MeowError:
        pass
    kind = "" if allow_custom else " standard"
    raise MeowError(f"Use only{kind} emojis in the medal field.")


def parse_color(value: str, default: int) -> int:
    cleaned = value.strip().removeprefix("#")
    if not cleaned:
        return default
    if not re.fullmatch(r"[a-fA-F0-9]{6}", cleaned):
        raise MeowError("Use a six digit hex color, for example #408871.")
    return int(cleaned, 16)


def number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


class Reward(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    key: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    name: str = Field(min_length=1, max_length=MAX_REWARD_NAME_LENGTH)
    emoji: str = Field(min_length=1, max_length=64)
    role_emoji: str | None = Field(default=None, max_length=64)
    points: int = Field(ge=1, le=MAX_POINTS)
    description: str = Field(min_length=1, max_length=1000)
    color: int = Field(default=0, ge=0, le=0xFFFFFF)

    @field_validator("name")
    @classmethod
    def single_line(cls, value: str) -> str:
        if len(value.splitlines()) != 1 or "\0" in value:
            raise ValueError("Use a single line for the reward name.")
        if len(value.encode("utf-16-le")) > 200:
            raise ValueError("That reward name is too long.")
        return value

    @field_validator("emoji")
    @classmethod
    def one_emoji(cls, value: str) -> str:
        return validate_emoji(value)

    @field_validator("role_emoji")
    @classmethod
    def one_role_emoji(cls, value: str | None) -> str | None:
        return emoji_presentation(value) if value is not None else None

    def role_symbol(self, *, medal: bool) -> str:
        if self.role_emoji is not None:
            return self.role_emoji
        if medal:
            return "".join(MEDAL_SYMBOL if CUSTOM_EMOJI.fullmatch(part) else part for part in emoji_parts(self.emoji))
        if CUSTOM_EMOJI.fullmatch(self.emoji):
            return "\U0001f431"
        return self.emoji

    @field_validator("description")
    @classmethod
    def valid_description(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("Descriptions cannot contain null characters.")
        return value


class Medal(Reward):
    emoji: str = Field(min_length=1, max_length=MAX_MEDAL_EMOJI_LENGTH)
    role_emoji: str | None = Field(default=None, max_length=MAX_ROLE_NAME_LENGTH)

    @field_validator("emoji")
    @classmethod
    def one_emoji(cls, value: str) -> str:
        normalized = emoji_sequence(value, allow_custom=True)
        if len(normalized) > MAX_MEDAL_EMOJI_LENGTH:
            raise ValueError("The medal emoji field is too long.")
        return normalized

    @field_validator("role_emoji")
    @classmethod
    def one_role_emoji(cls, value: str | None) -> str | None:
        return emoji_sequence(value, allow_custom=False) if value is not None else None

    @model_validator(mode="after")
    def validate_role_length(self) -> Medal:
        role_name = self.role_symbol(medal=True)
        if len(role_name.encode("utf-16-le")) // 2 > MAX_ROLE_NAME_LENGTH:
            raise ValueError("The medal role name must fit within 100 characters.")
        return self


class EraSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    era: int = Field(ge=1, le=3999)
    stage: int = Field(ge=1, le=FINAL_STAGE)

    @property
    def label(self) -> str:
        stage = f"Season {self.stage}" if self.stage < FINAL_STAGE else "Final act"
        return f"{era_title(self.era)} \u00b7 {stage}"


def era_title(era: int) -> str:
    parts: list[str] = []
    for value, symbol in (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    ):
        count, era = divmod(era, value)
        parts.append(symbol * count)
    return "".join(parts) + " Era"


def next_era_slot(completed: set[EraSlot]) -> EraSlot:
    era = max((slot.era for slot in completed), default=1)
    for stage in range(1, FINAL_STAGE + 1):
        slot = EraSlot(era=era, stage=stage)
        if slot not in completed:
            return slot
    if era >= 3999:
        raise MeowError("No automatic era is available.")
    return EraSlot(era=era + 1, stage=1)


class SeasonDesign(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(default="", max_length=80)
    emoji: str | None = Field(default=None, max_length=64)
    era_slot: EraSlot | None = None
    introduction: str = Field(default="", max_length=800)
    banner_url: str | None = Field(default=None, max_length=MAX_BANNER_URL_LENGTH)
    source: PointSource = "text"
    accent: int = Field(default=DEFAULT_ACCENT, ge=0, le=0xFFFFFF)
    ranks: tuple[Reward, ...] = Field(default=(), max_length=MAX_RANKS)
    medal: Medal | None = None

    @field_validator("medal", mode="before")
    @classmethod
    def reward_to_medal(cls, value: object) -> object:
        if isinstance(value, Reward) and not isinstance(value, Medal):
            return value.model_dump()
        return value

    @field_validator("emoji")
    @classmethod
    def server_emoji(cls, value: str | None) -> str | None:
        if value is not None and not CUSTOM_EMOJI.fullmatch(value):
            raise ValueError("The season icon must be a server emoji.")
        return value

    @field_validator("banner_url")
    @classmethod
    def valid_banner_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        error = "Use a direct HTTPS image URL for the season banner."
        try:
            url = HttpUrl(value)
        except ValidationError:
            raise ValueError(error) from None
        if url.scheme != "https" or url.username is not None or url.password is not None or re.search(r"[\s\x00-\x1f\x7f]", value):
            raise ValueError(error)
        return str(url)

    @model_validator(mode="after")
    def validate_rewards(self) -> SeasonDesign:
        keys = {reward.key for reward in self.rewards}
        if len(keys) != len(self.rewards):
            raise ValueError("Each reward must have its own ID.")
        if len({reward.name.casefold() for reward in self.rewards}) != len(self.rewards):
            raise ValueError("Reward names must be different.")
        if len({rank.points for rank in self.ranks}) != len(self.ranks):
            raise ValueError("Rank thresholds must be different.")
        if self.medal is not None and any(rank.points >= self.medal.points for rank in self.ranks):
            raise ValueError("The medal must require more points than any rank.")
        if "\0" in self.name or "\0" in self.introduction:
            raise ValueError("Season text cannot contain null characters.")
        total = sum(len(text.encode("utf-16-le")) // 2 for text in (self.introduction, *(reward.description for reward in self.rewards)))
        if total > MAX_SHOWCASE_TEXT:
            raise ValueError("Shorten the descriptions to fit one Discord showcase.")
        return self

    @property
    def ordered_ranks(self) -> tuple[Reward, ...]:
        return tuple(sorted(self.ranks, key=lambda rank: rank.points))

    @property
    def rewards(self) -> tuple[Reward, ...]:
        return (*self.ordered_ranks, self.medal) if self.medal else self.ordered_ranks

    def updated(self, **changes: object) -> SeasonDesign:
        return SeasonDesign.model_validate({**self.model_dump(), **changes})

    def put_reward(self, reward: Reward, *, medal: bool) -> SeasonDesign:
        if medal:
            return self.updated(medal=reward)
        ranks = tuple(rank for rank in self.ranks if rank.key != reward.key)
        return self.updated(ranks=(*ranks, reward))

    def remove_rank(self, key: str) -> SeasonDesign:
        return self.updated(ranks=tuple(rank for rank in self.ranks if rank.key != key))

    def ready(self) -> None:
        if not self.name or len(self.name.splitlines()) != 1:
            raise MeowError("Give the season a single line name.")
        if not self.ranks:
            raise MeowError("Add at least one rank.")
        if self.medal is None:
            raise MeowError("Set up the final medal.")

    def role_keys(self) -> tuple[frozenset[str], str | None]:
        return frozenset(rank.key for rank in self.ranks), self.medal.key if self.medal else None

    def rank_at(self, points: int) -> Reward | None:
        return next(
            (rank for rank in reversed(self.ordered_ranks) if points >= rank.points),
            None,
        )


@dataclass(frozen=True)
class Draft:
    design: SeasonDesign
    revision: int
    based_on: int | None


@dataclass(frozen=True)
class Season:
    id: int
    guild_id: int
    design: SeasonDesign
    state: SeasonState
    roles: dict[str, int]
    revision: int
    draft_revision: int
    role_revision: int
    started_at: datetime | None
    closed_at: datetime | None
    winner_id: int | None
    test_user_id: int | None
    cleaned: bool
    winner_username: str | None = None


@dataclass(frozen=True)
class Showcase:
    channel_id: int | None
    message_id: int | None
    season_id: int | None
    revision: int
    retired_message_id: int | None = None


@dataclass(frozen=True)
class TrialShowcase:
    channel_id: int | None
    message_id: int | None


@dataclass(frozen=True)
class RankChange:
    user_id: int
    reward_key: str | None


@dataclass(frozen=True)
class ArchivedMedal:
    stage: int
    winner_id: int
    username: str
    role_id: int
    medal_emoji: str
    season_emoji: str | None


@dataclass(frozen=True)
class EraBoard:
    channel_id: int | None
    message_id: int | None
    signature: str | None
    emoji: str | None = None


@dataclass(frozen=True)
class Dashboard:
    draft: Draft
    live: Season | None
    preparing: Season | None
    latest: Season | None
    next_slot: EraSlot | None = None

    @property
    def shown(self) -> Season | None:
        return self.live or self.latest

    @property
    def automatic_slot(self) -> EraSlot | None:
        return (self.live.design.era_slot if self.live else None) or self.next_slot

    @property
    def draft_slot(self) -> EraSlot | None:
        return self.draft.design.era_slot or self.automatic_slot


def can_update_live(draft: Draft, season: Season | None) -> bool:
    return (
        season is not None
        and season.state == "active"
        and draft.based_on == season.id
        and draft.design.role_keys() == season.design.role_keys()
    )
