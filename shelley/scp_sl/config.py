from __future__ import annotations

import os
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScpSlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = False
    display_address: str = Field(default="", max_length=253, pattern=r"^[A-Za-z0-9.-]*$")
    logo_url: str = Field(default="", max_length=2048)
    color: int = Field(default=0xFFFFFF, ge=0, le=0xFFFFFF)
    separator_content: str = Field(default="\u2800\n\u2800", max_length=2000)
    token_env: str = Field(default="SHELLEY_SCP_SL_TOKEN", max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$")
    api_url: str = Field(default="http://127.0.0.1:8790", max_length=2048)
    poll_seconds: float = Field(default=1, ge=1, le=5)
    timeout_seconds: float = Field(default=2, gt=0, le=2)
    stale_seconds: float = Field(default=5, gt=0, le=5)

    @field_validator("api_url", "logo_url")
    @classmethod
    def validate_url(cls, value: str, info) -> str:
        if not value and info.field_name == "logo_url":
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (info.field_name == "api_url" and parsed.query)
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError("an HTTP(S) URL without credentials or fragments is required")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("invalid URL port")
        return value.rstrip("/") if info.field_name == "api_url" else value

    def validate_runtime(self) -> None:
        if not self.enabled:
            return
        if not self.display_address:
            raise ValueError("scp_sl.display_address must be configured without a port")
        token = os.environ.get(self.token_env, "")
        if not 32 <= len(token) <= 256 or any(not 33 <= ord(character) <= 126 for character in token):
            raise ValueError("scp_sl requires a 32-256 character bearer token in its configured environment variable")
