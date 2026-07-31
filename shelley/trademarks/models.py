from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

RequestType = Literal["exchange", "gift"]
RequestStatus = Literal[
    "pending",
    "accepted",
    "declined",
    "cancelled",
    "expired",
    "invalidated",
]
EventType = Literal["patent", "release", "admin_release", "gift", "exchange"]
MAX_EXCHANGE_SIDE_MARKS = 5


@dataclass(frozen=True, slots=True)
class Trademark:
    id: str
    guild_id: int
    display_name: str
    normalized_name: str
    owner_id: int | None
    owner_name: str | None
    created_at: datetime
    cycle_started_at: datetime | None
    owner_since: datetime | None

    @property
    def decorated_name(self) -> str:
        return f"{self.display_name}™"


@dataclass(frozen=True, slots=True)
class TrademarkEvent:
    id: int
    guild_id: int
    trademark_id: str
    event_type: EventType
    actor_id: int
    actor_name: str
    from_user_id: int | None
    from_user_name: str | None
    to_user_id: int | None
    to_user_name: str | None
    related_trademark_id: str | None
    related_trademark_name: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TrademarkRequest:
    id: str
    guild_id: int
    request_type: RequestType
    sender_id: int
    sender_name: str
    recipient_id: int
    recipient_name: str
    offered_trademark_ids: tuple[str, ...]
    requested_trademark_ids: tuple[str, ...]
    source_channel_id: int
    status: RequestStatus
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None
    resolved_by_id: int | None
    resolved_by_name: str | None

    @property
    def offered_trademark_id(self) -> str:
        return self.offered_trademark_ids[0]

    @property
    def requested_trademark_id(self) -> str | None:
        return self.requested_trademark_ids[0] if self.requested_trademark_ids else None


@dataclass(frozen=True, slots=True)
class TrademarkRequestListing:
    request: TrademarkRequest
    offered_names: tuple[str, ...]
    requested_names: tuple[str, ...]

    @property
    def offered_name(self) -> str:
        return self.offered_names[0]

    @property
    def requested_name(self) -> str | None:
        return self.requested_names[0] if self.requested_names else None


@dataclass(frozen=True, slots=True)
class PatentAvailability:
    owned: int
    inventory_limit: int
    used: int
    patent_limit: int
    next_window_at: datetime | None
    cooldown_until: datetime | None

    @property
    def available(self) -> int:
        return max(0, self.patent_limit - self.used)


@dataclass(frozen=True, slots=True)
class ClaimResult:
    status: Literal[
        "claimed",
        "occupied",
        "inventory_full",
        "limit_reached",
        "cooldown",
    ]
    trademark: Trademark | None = None
    next_available_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TransferResult:
    status: Literal[
        "completed",
        "expired",
        "invalid",
        "already_processed",
        "inventory_full",
    ]
    request: TrademarkRequest | None = None
    offered: tuple[Trademark, ...] = ()
    requested: tuple[Trademark, ...] = ()
