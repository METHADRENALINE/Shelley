import re
import unicodedata
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PublicEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    timestamp: datetime
    kind: Literal["chat", "broadcast"]
    text: str = Field(min_length=1, max_length=1800)

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp requires a timezone")
        return value.astimezone(UTC)


class Exchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[PublicEvent] = Field(default_factory=list, max_length=25)
    acknowledged: list[str] = Field(default_factory=list, max_length=25)


def clean_text(text: str) -> str:
    text = re.sub(r"§[0-9a-fk-orx]", "", text, flags=re.IGNORECASE)
    return "".join(c for c in text if c == "\n" or not unicodedata.category(c).startswith("C")).strip()
