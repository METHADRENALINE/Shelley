from __future__ import annotations

import os
from pathlib import Path
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BridgeNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    receive_discord: bool = False
    label: str = Field(default="", max_length=40)


class BridgeRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: int = Field(ge=0, lt=2**64)
    channel_id: int = Field(ge=0, lt=2**64)
    minecraft_to_discord: bool = True
    discord_to_minecraft: bool = True
    public_announcements: bool = True
    allowed_bot_user_ids: set[int] = Field(default_factory=set)
    discord_format: str = "{text}"
    nodes: dict[str, BridgeNode] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_route(self) -> BridgeRoute:
        if any(user_id <= 0 or user_id >= 2**64 for user_id in self.allowed_bot_user_ids):
            raise ValueError("allowed_bot_user_ids must contain valid Discord IDs")
        if self.discord_to_minecraft and sum(node.receive_discord for node in self.nodes.values()) != 1:
            raise ValueError("exactly one node must receive Discord messages for each route")
        validate_names(self.nodes)
        for _, name, spec, conversion in Formatter().parse(self.discord_format):
            if name is not None and (name not in {"text", "server"} or spec or conversion):
                raise ValueError("discord_format only supports {text} and {server}")
        if "{text}" not in self.discord_format or len(self.discord_format) > 200:
            raise ValueError("discord_format must include {text} and be at most 200 characters")
        return self


def validate_names(values: dict) -> None:
    import re

    if any(not re.fullmatch(r"[a-z0-9_]{1,40}", name) for name in values):
        raise ValueError("route and node names must use lowercase letters, digits or underscores")


class ChatBridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    bind_host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    tls_certificate: str = ""
    tls_private_key: str = ""
    poll_seconds: float = Field(default=1, ge=0.2, le=60)
    message_ttl_seconds: int = Field(default=120, ge=10, le=3600)
    retention_hours: int = Field(default=24, ge=1, le=168)
    max_message_length: int = Field(default=1500, ge=1, le=1800)
    max_queue: int = Field(default=500, ge=1, le=5000)
    user_interval_seconds: float = Field(default=1, ge=0, le=60)
    routes: dict[str, BridgeRoute] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_routes(self) -> ChatBridgeConfig:
        validate_names(self.routes)
        channels = [route.channel_id for route in self.routes.values()]
        if len(channels) != len(set(channels)):
            raise ValueError("each bridge route needs a different Discord channel")
        if self.enabled and not self.routes:
            raise ValueError("enabled chat bridge needs at least one route")
        if self.enabled and any(route.guild_id == 0 or route.channel_id == 0 for route in self.routes.values()):
            raise ValueError("enabled chat bridge routes require nonzero guild and channel IDs")
        return self

    def validate_runtime(self) -> None:
        if not self.enabled:
            return
        if not Path(self.tls_certificate).is_file() or not Path(self.tls_private_key).is_file():
            raise ValueError("chat bridge TLS certificate and private key files are required")
        tokens = [os.environ.get(node.token_env, "") for route in self.routes.values() for node in route.nodes.values()]
        if any(len(token) < 32 for token in tokens) or len(set(tokens)) != len(tokens):
            raise ValueError(
                "chat bridge nodes require distinct secrets of at least 32 characters in their configured environment variables"
            )
