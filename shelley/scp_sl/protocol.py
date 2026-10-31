from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

ModeId = Literal["plain", "perks", "paravoz"]
SessionId = Annotated[
    str,
    Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
]
ModeName = Annotated[str, Field(min_length=1, max_length=80)]
PlayerName = Annotated[str, Field(min_length=1, max_length=100)]


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Mode(ProtocolModel):
    id: ModeId
    name_ru: ModeName

    @field_validator("name_ru")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if not value.strip() or any(not character.isprintable() for character in value):
            raise ValueError("mode names must be printable single-line text")
        return value


class Poll(ProtocolModel):
    id: SessionId | None
    deadline: AwareDatetime | None
    open: bool

    @model_validator(mode="after")
    def validate_open_poll(self) -> Poll:
        if self.open and (self.id is None or self.deadline is None):
            raise ValueError("an open poll requires an ID and deadline")
        return self


class Snapshot(ProtocolModel):
    schema_version: int = Field(ge=1, le=1)
    boot_id: SessionId
    heartbeat_sequence: int = Field(ge=0, le=2**63 - 1)
    heartbeat_at: AwareDatetime
    online_names: list[PlayerName] = Field(max_length=128)
    phase: Literal["empty", "voting", "countdown", "round", "ended"]
    mode: ModeId | None
    mode_name_ru: ModeName | None
    available_modes: list[Mode] = Field(max_length=3)
    poll: Poll

    @field_validator("online_names")
    @classmethod
    def validate_names(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or any(not character.isprintable() for character in value) for value in values):
            raise ValueError("player names must be printable single-line text")
        return values

    @field_validator("mode_name_ru")
    @classmethod
    def validate_mode_label(cls, value: str | None) -> str | None:
        return None if value is None else Mode.validate_label(value)

    @model_validator(mode="after")
    def validate_modes(self) -> Snapshot:
        ids = [mode.id for mode in self.available_modes]
        if len(ids) != len(set(ids)):
            raise ValueError("mode IDs must be unique")
        if (self.mode is None) != (self.mode_name_ru is None):
            raise ValueError("selected mode requires its localized name")
        if self.poll.open and (len(self.online_names) < 2 or not ids or self.phase != "voting"):
            raise ValueError("an open poll requires at least two players, modes and the voting phase")
        if "paravoz" in ids and len(self.online_names) < 2:
            raise ValueError("paravoz requires at least two players")
        return self

    def voting(self, now: datetime) -> bool:
        return bool(len(self.online_names) >= 2 and self.poll.open and self.poll.deadline is not None and self.poll.deadline > now)


class Vote(ProtocolModel):
    boot_id: SessionId
    poll_id: SessionId
    discord_user_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    mode: ModeId

    @field_validator("discord_user_id")
    @classmethod
    def validate_snowflake(cls, value: str) -> str:
        if int(value) >= 2**64:
            raise ValueError("invalid Discord snowflake")
        return value


class VoteResult(ProtocolModel):
    result: Literal["accepted", "duplicate", "closed"]
