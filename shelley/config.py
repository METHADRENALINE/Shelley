from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

TOKEN_PLACEHOLDER = "replace-with-your-discord-bot-token"


class ConfigError(RuntimeError):
    pass


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = "postgresql:///shelley"
    connect_timeout: int = 5
    pool_min_size: int = 1
    pool_max_size: int = 5

    @field_validator("connect_timeout", "pool_min_size", "pool_max_size")
    @classmethod
    def validate_positive(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("database numeric values must be at least 1")
        return int(value)

    @model_validator(mode="after")
    def validate_pool(self) -> DatabaseConfig:
        if self.pool_max_size < self.pool_min_size:
            raise ValueError("pool_max_size must be greater than or equal to pool_min_size")
        return self

    def resolved_url(self) -> str:
        return os.getenv("SHELLEY_DATABASE_URL") or os.getenv("DATABASE_URL") or self.url


class StarForwardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    source_channel_ids: list[int] = Field(default_factory=list)
    target_channel_id: int = 0
    emoji: str = "⭐"
    threshold: int = 3

    @field_validator("source_channel_ids")
    @classmethod
    def validate_source_channel_ids(cls, value: list[int]) -> list[int]:
        return [int(item) for item in value if int(item) >= 0]

    @field_validator("target_channel_id")
    @classmethod
    def validate_target_channel_id(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError("target_channel_id must not be negative")
        return int(value)

    @field_validator("threshold")
    @classmethod
    def validate_threshold(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("threshold must be at least 1")
        return int(value)


class PointsTextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    channel_ids: list[int] = Field(default_factory=list)
    excluded_channel_ids: list[int] = Field(default_factory=list)
    interval_seconds: float = 120
    poll_seconds: float = 15
    poll_limit: int = 25
    poll_initial_lookback_seconds: float = 3600
    award_min: int = 10
    award_max: int = 20

    @field_validator("channel_ids", "excluded_channel_ids")
    @classmethod
    def validate_channel_ids(cls, value: list[int]) -> list[int]:
        return [int(item) for item in value if int(item) >= 0]

    @field_validator("interval_seconds", "poll_seconds", "poll_initial_lookback_seconds")
    @classmethod
    def validate_seconds(cls, value: float) -> float:
        if float(value) < 0:
            raise ValueError("seconds values must not be negative")
        return float(value)

    @field_validator("poll_limit")
    @classmethod
    def validate_poll_limit(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("poll_limit must be at least 1")
        return int(value)

    @model_validator(mode="after")
    def validate_awards(self) -> PointsTextConfig:
        if self.award_min < 0 or self.award_max < 0:
            raise ValueError("award values must not be negative")
        return self


class PointsVoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    channel_ids: list[int] = Field(default_factory=list)
    excluded_channel_ids: list[int] = Field(default_factory=list)
    interval_seconds: float = 120
    active_microphone_seconds: float = 3
    check_seconds: float = 1
    reconnect_grace_seconds: float = 60
    reconnect_retry_seconds: float = 120
    award_min: int = 10
    award_max: int = 20

    @field_validator("channel_ids", "excluded_channel_ids")
    @classmethod
    def validate_channel_ids(cls, value: list[int]) -> list[int]:
        return [int(item) for item in value if int(item) >= 0]

    @field_validator(
        "interval_seconds",
        "active_microphone_seconds",
        "check_seconds",
        "reconnect_grace_seconds",
        "reconnect_retry_seconds",
    )
    @classmethod
    def validate_seconds(cls, value: float) -> float:
        if float(value) < 0:
            raise ValueError("seconds values must not be negative")
        return float(value)

    @field_validator("reconnect_grace_seconds", "reconnect_retry_seconds")
    @classmethod
    def validate_reconnect_seconds(cls, value: float) -> float:
        if float(value) < 30:
            raise ValueError("voice reconnect values must be at least 30 seconds")
        return float(value)

    @model_validator(mode="after")
    def validate_awards(self) -> PointsVoiceConfig:
        if self.award_min < 0 or self.award_max < 0:
            raise ValueError("award values must not be negative")
        return self


class PointsLeaderboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    channel_id: int = 0
    update_seconds: float = 5
    limit: int = 10
    text_color: int = 0x5865F2
    voice_color: int = 0x57F287
    placeholder_text: str = ""

    @field_validator("channel_id")
    @classmethod
    def validate_channel_id(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError("channel_id must not be negative")
        return int(value)

    @field_validator("update_seconds")
    @classmethod
    def validate_update_seconds(cls, value: float) -> float:
        if float(value) < 0:
            raise ValueError("update_seconds must not be negative")
        return float(value)

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("limit must be at least 1")
        return int(value)


class PointsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    log_events: bool = False
    state_path: str = "data/points.json"
    text: PointsTextConfig = Field(default_factory=PointsTextConfig)
    voice: PointsVoiceConfig = Field(default_factory=PointsVoiceConfig)
    leaderboard: PointsLeaderboardConfig = Field(default_factory=PointsLeaderboardConfig)


class ServerComponentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    address: str
    tmux_session: str | None = None

    @field_validator("label", "address")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placeholder: str
    kind: str = "minecraft"
    address: str | None = None
    version_edition_override: str | None = None
    components: list[ServerComponentConfig] = Field(default_factory=list)

    @field_validator("placeholder", "kind")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class RemoteTargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    user: str
    key_path: str | None = None
    ssh_profile: str | None = None
    connect_timeout: int = 10
    command_timeout: float = 30
    status_command: str | None = None
    status_connect_timeout: int = 2
    status_command_timeout: float = 4
    start_command: str | None = None
    reboot_command: str | None = None
    status_placeholder: str | None = None
    starting_ttl_seconds: int = 600

    @field_validator("host", "user")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValueError("value must not be empty")
        return value

    @model_validator(mode="after")
    def validate_ssh_identity(self) -> RemoteTargetConfig:
        if not self.key_path and not self.ssh_profile:
            raise ValueError("key_path or ssh_profile must be configured")
        return self


class StatusMessageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    type: Literal["embed", "separator"] = "embed"
    renderer: str | None = None
    template_path: str | None = None
    status_placeholder: str | None = None
    control_target: str | None = None
    content: str = "\u2800\n\u2800"

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        value = str(value).strip().lower()
        if not value:
            raise ValueError("key must not be empty")
        return value

    @model_validator(mode="after")
    def validate_message(self) -> StatusMessageConfig:
        if self.type == "embed":
            if not self.renderer:
                raise ValueError("renderer is required for embed status messages")
            if not self.template_path:
                raise ValueError("template_path is required for embed status messages")
            if not self.status_placeholder:
                raise ValueError("status_placeholder is required for embed status messages")
        return self


class BotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: int = 0
    token: str | None = None
    dev_guild_id: int = 0
    notify_channel_id: int = 0
    welcome_channel_id: int = 0
    status_channel_id: int = 0
    welcome_message_path: str = "templates/welcome-msg.json"
    welcome_update_seconds: int = 5
    welcome_presence_check_seconds: int = 60
    star_forward: StarForwardConfig = Field(default_factory=StarForwardConfig)
    update_seconds: int = 30
    timeout_seconds: float = 3
    state_path: str = "data/state.json"
    recovery_log_path: str = "data/recovery-controls.jsonl"
    recovery_log_retention_days: int = 365
    recovery_control_cooldown_seconds: int = 60
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    points: PointsConfig = Field(default_factory=PointsConfig)
    servers: list[ServerConfig] = Field(default_factory=list)
    remote_targets: dict[str, RemoteTargetConfig] = Field(default_factory=dict)
    status_messages: list[StatusMessageConfig] = Field(default_factory=list)

    @field_validator("client_id", "dev_guild_id", "notify_channel_id", "welcome_channel_id", "status_channel_id")
    @classmethod
    def validate_snowflake(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError("Discord IDs must not be negative")
        return int(value)

    @field_validator(
        "welcome_update_seconds",
        "welcome_presence_check_seconds",
        "update_seconds",
        "recovery_log_retention_days",
        "recovery_control_cooldown_seconds",
    )
    @classmethod
    def validate_positive_int(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("value must be at least 1")
        return int(value)

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout_seconds(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        return float(value)

    def discord_token(self) -> str:
        token = (os.getenv("DISCORD_TOKEN") or str(self.token or "")).strip()
        if token == TOKEN_PLACEHOLDER:
            return ""
        return token

    def runtime_guild_id(self) -> int:
        return int(self.dev_guild_id or 0)

    def validate_runtime(self) -> None:
        errors: list[str] = []
        if not self.database.resolved_url().strip():
            errors.append("database.url or SHELLEY_DATABASE_URL must be configured")
        if self.notify_channel_id <= 0:
            errors.append("notify_channel_id must be configured")
        if self.status_channel_id <= 0:
            errors.append("status_channel_id must be configured")
        if self.welcome_channel_id <= 0:
            errors.append("welcome_channel_id must be configured")
        if self.star_forward.enabled:
            if not any(item > 0 for item in self.star_forward.source_channel_ids):
                errors.append("star_forward.source_channel_ids must contain at least one channel")
            if self.star_forward.target_channel_id <= 0:
                errors.append("star_forward.target_channel_id must be configured")
        if self.points.enabled:
            if self.points.text.enabled and not any(item > 0 for item in self.points.text.channel_ids):
                errors.append("points.text.channel_ids must contain at least one channel")
            if self.points.voice.enabled and not any(item > 0 for item in self.points.voice.channel_ids):
                errors.append("points.voice.channel_ids must contain at least one channel")
            if self.points.leaderboard.enabled and self.points.leaderboard.channel_id <= 0:
                errors.append("points.leaderboard.channel_id must be configured")
        if not self.servers:
            errors.append("servers must contain at least one server")
        if not self.status_messages:
            errors.append("status_messages must contain at least one status message")
        for index, server in enumerate(self.servers):
            if not server.address and not server.components:
                errors.append(f"servers[{index}] must configure address or components")
        for message in self.status_messages:
            if message.type == "embed" and message.template_path and not Path(message.template_path).exists():
                errors.append(f"{message.template_path} does not exist")
        if errors:
            raise ConfigError("Invalid runtime configuration:\n" + "\n".join(f"- {item}" for item in errors))


def parse_config(data: dict, path: str) -> BotConfig:
    try:
        return BotConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Invalid config file {path}: {e}") from e
