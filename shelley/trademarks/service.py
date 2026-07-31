from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Literal

from ..config import TrademarkGuildConfig
from ..db import Database
from .ids import generate_trademark_id, is_trademark_id, normalize_trademark_id
from .models import (
    MAX_EXCHANGE_SIDE_MARKS,
    ClaimResult,
    PatentAvailability,
    Trademark,
    TrademarkEvent,
    TrademarkRequest,
    TrademarkRequestListing,
    TransferResult,
)
from .normalization import normalize_trademark_name
from .repository import TrademarkRepository


class TrademarkOperationError(RuntimeError):
    pass


class TrademarkNotFound(TrademarkOperationError):
    pass


class TrademarkPermissionDenied(TrademarkOperationError):
    pass


class TrademarkRequestLimitReached(TrademarkOperationError):
    pass


class TrademarkDuplicateRequest(TrademarkOperationError):
    pass


class TrademarkInvalidRequest(TrademarkOperationError):
    pass


class TrademarkShowcaseFull(TrademarkOperationError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def _present_trademarks(
    trademark_ids: Iterable[str],
    trademarks: dict[str, Trademark | None],
) -> tuple[Trademark, ...]:
    result: list[Trademark] = []
    for trademark_id in trademark_ids:
        trademark = trademarks.get(trademark_id)
        if trademark is not None:
            result.append(trademark)
    return tuple(result)


class TrademarkService:
    def __init__(
        self,
        db: Database,
        repository: TrademarkRepository | None = None,
        *,
        now: Callable[[], datetime] = utc_now,
        trademark_id_factory: Callable[[], str] = generate_trademark_id,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.db = db
        self.repository = repository or TrademarkRepository()
        self.now = now
        self.trademark_id_factory = trademark_id_factory
        self.request_id_factory = request_id_factory or (lambda: str(uuid.uuid4()))

    def _window_state(
        self,
        row: dict,
        config: TrademarkGuildConfig,
        now: datetime,
    ) -> tuple[datetime | None, int, datetime | None]:
        started_at = row["window_started_at"]
        used = int(row["successful_count"])
        last_patent_at = row["last_patent_at"]
        if started_at is None or now >= started_at + timedelta(hours=config.patent_window_hours):
            return None, 0, None
        return started_at, used, last_patent_at

    def availability(
        self,
        guild_id: int,
        user_id: int,
        config: TrademarkGuildConfig,
    ) -> PatentAvailability:
        now = self.now()
        with self.db.connection() as conn:
            self.repository.lock_users(conn, guild_id, [user_id])
            owned = self.repository.owned_count(conn, guild_id, user_id)
            row = self.repository.ensure_window(conn, guild_id, user_id)
            started_at, used, last_patent_at = self._window_state(row, config, now)
        next_window_at = (
            started_at + timedelta(hours=config.patent_window_hours) if started_at is not None and used >= config.patent_limit else None
        )
        cooldown_until = None
        if last_patent_at is not None:
            candidate = last_patent_at + timedelta(seconds=config.patent_cooldown_seconds)
            if candidate > now:
                cooldown_until = candidate
        return PatentAvailability(
            owned=owned,
            inventory_limit=config.inventory_limit,
            used=used,
            patent_limit=config.patent_limit,
            next_window_at=next_window_at,
            cooldown_until=cooldown_until,
        )

    def claim(
        self,
        guild_id: int,
        user_id: int,
        user_name: str,
        raw_name: str,
        config: TrademarkGuildConfig,
    ) -> ClaimResult:
        normalized = normalize_trademark_name(raw_name, config)
        now = self.now()
        with self.db.connection() as conn:
            self.repository.lock_name(conn, guild_id, normalized.key)
            existing = self.repository.get_by_normalized_name(conn, guild_id, normalized.key)
            if existing is not None:
                self.repository.lock_marks(conn, guild_id, [existing.id])
            self.repository.lock_users(conn, guild_id, [user_id])
            if existing is not None:
                existing = self.repository.get_by_id(conn, guild_id, existing.id, for_update=True)
            if existing is not None and existing.owner_id is not None:
                return ClaimResult(status="occupied", trademark=existing)

            if self.repository.owned_count(conn, guild_id, user_id) >= config.inventory_limit:
                return ClaimResult(status="inventory_full", trademark=existing)

            window = self.repository.ensure_window(conn, guild_id, user_id)
            started_at, used, last_patent_at = self._window_state(window, config, now)
            if used >= config.patent_limit:
                if started_at is None:
                    raise RuntimeError("Patent window has no start time")
                return ClaimResult(
                    status="limit_reached",
                    trademark=existing,
                    next_available_at=started_at + timedelta(hours=config.patent_window_hours),
                )
            if last_patent_at is not None:
                cooldown_until = last_patent_at + timedelta(seconds=config.patent_cooldown_seconds)
                if cooldown_until > now:
                    return ClaimResult(
                        status="cooldown",
                        trademark=existing,
                        next_available_at=cooldown_until,
                    )

            if existing is None:
                trademark = None
                for _ in range(10):
                    trademark = self.repository.insert(
                        conn,
                        self.trademark_id_factory(),
                        guild_id,
                        normalized.display,
                        normalized.key,
                        normalized.search_key,
                        user_id,
                        user_name,
                        now,
                    )
                    if trademark is not None:
                        break
                if trademark is None:
                    raise RuntimeError("Could not generate a unique trademark ID")
            else:
                trademark = self.repository.claim_free(
                    conn,
                    existing.id,
                    normalized.display,
                    normalized.key,
                    normalized.search_key,
                    user_id,
                    user_name,
                    now,
                )

            if started_at is None:
                started_at = now
                used = 0
            self.repository.save_window(
                conn,
                guild_id,
                user_id,
                started_at,
                used + 1,
                now,
                now,
            )
            self.repository.insert_event(
                conn,
                guild_id,
                trademark.id,
                "patent",
                user_id,
                user_name,
                now,
                to_user_id=user_id,
                to_user_name=user_name,
            )
            return ClaimResult(status="claimed", trademark=trademark)

    def get(self, guild_id: int, trademark_id: str) -> Trademark | None:
        with self.db.connection() as conn:
            return self.repository.get_by_id(conn, guild_id, trademark_id)

    def get_many(self, guild_id: int, trademark_ids: Iterable[str]) -> tuple[Trademark, ...]:
        with self.db.connection() as conn:
            return tuple(self.repository.get_many(conn, guild_id, trademark_ids))

    def owned_page(self, guild_id: int, user_id: int, limit: int, offset: int) -> tuple[list[Trademark], int]:
        with self.db.connection() as conn:
            return self.repository.owned_page(conn, guild_id, user_id, limit, offset)

    def owned_page_excluding(
        self,
        guild_id: int,
        user_id: int,
        excluded_ids: Iterable[str],
        limit: int,
        offset: int,
    ) -> tuple[list[Trademark], int]:
        with self.db.connection() as conn:
            return self.repository.owned_page_excluding(
                conn,
                guild_id,
                user_id,
                excluded_ids,
                limit,
                offset,
            )

    def all_page(self, guild_id: int, limit: int, offset: int) -> tuple[list[Trademark], int]:
        with self.db.connection() as conn:
            return self.repository.all_page(conn, guild_id, limit, offset)

    def search(
        self,
        guild_id: int,
        query: str,
        config: TrademarkGuildConfig,
    ) -> list[Trademark]:
        cleaned = str(query).strip()
        cleaned = cleaned.removesuffix("™")
        if is_trademark_id(cleaned):
            identifier_query = normalize_trademark_id(cleaned)
            comparison_key = None
            search_query = None
        else:
            normalized = normalize_trademark_name(cleaned, config)
            identifier_query = None
            comparison_key = normalized.key
            search_query = normalized.search_key
        with self.db.connection() as conn:
            return self.repository.search(
                conn,
                guild_id,
                identifier_query,
                comparison_key,
                search_query,
                config.search_result_limit,
            )

    def showcase(self, guild_id: int, user_id: int) -> list[Trademark]:
        with self.db.connection() as conn:
            return self.repository.showcase(conn, guild_id, user_id)

    def add_to_showcase(
        self,
        guild_id: int,
        user_id: int,
        trademark_id: str,
        config: TrademarkGuildConfig,
    ) -> None:
        with self.db.connection() as conn:
            self.repository.lock_marks(conn, guild_id, [trademark_id])
            self.repository.lock_users(conn, guild_id, [user_id])
            trademark = self.repository.get_by_id(conn, guild_id, trademark_id, for_update=True)
            if trademark is None:
                raise TrademarkNotFound("Трейд марка не найдена")
            if trademark.owner_id != user_id:
                raise TrademarkPermissionDenied("Можно закрепить только свою трейд марку")
            current = self.repository.showcase(conn, guild_id, user_id)
            if any(item.id == trademark_id for item in current):
                return
            if len(current) >= config.showcase_limit:
                raise TrademarkShowcaseFull("Витрина заполнена")
            self.repository.add_showcase(conn, guild_id, user_id, trademark_id, len(current) + 1)

    def remove_from_showcase(self, guild_id: int, user_id: int, trademark_id: str) -> None:
        with self.db.connection() as conn:
            self.repository.lock_users(conn, guild_id, [user_id])
            self.repository.remove_showcase(conn, guild_id, user_id, trademark_id)

    def move_in_showcase(self, guild_id: int, user_id: int, trademark_id: str, position: int) -> None:
        with self.db.connection() as conn:
            self.repository.lock_users(conn, guild_id, [user_id])
            self.repository.move_showcase(conn, guild_id, user_id, trademark_id, position)

    def history_page(self, guild_id: int, trademark_id: str, limit: int, offset: int) -> tuple[list[TrademarkEvent], int]:
        with self.db.connection() as conn:
            return self.repository.history_page(conn, guild_id, trademark_id, limit, offset)

    def release(
        self,
        guild_id: int,
        trademark_id: str,
        actor_id: int,
        actor_name: str,
        *,
        administrator: bool = False,
    ) -> Trademark:
        now = self.now()
        with self.db.connection() as conn:
            self.repository.lock_marks(conn, guild_id, [trademark_id])
            trademark = self.repository.get_by_id(conn, guild_id, trademark_id)
            if trademark is None:
                raise TrademarkNotFound("Трейд марка не найдена")
            if trademark.owner_id is None:
                raise TrademarkInvalidRequest("Патент уже снят")
            self.repository.lock_users(conn, guild_id, [trademark.owner_id])
            trademark = self.repository.get_by_id(conn, guild_id, trademark_id, for_update=True)
            if trademark is None or trademark.owner_id is None:
                raise TrademarkInvalidRequest("Патент уже снят")
            if trademark.owner_id != actor_id and not administrator:
                raise TrademarkPermissionDenied("Нельзя снять патент другого пользователя")
            previous_owner_id = trademark.owner_id
            previous_owner_name = trademark.owner_name
            released = self.repository.release(conn, trademark.id, now)
            self.repository.remove_showcase_marks(conn, guild_id, [trademark.id])
            self.repository.invalidate_requests_for_marks(conn, guild_id, [trademark.id], now)
            self.repository.insert_event(
                conn,
                guild_id,
                trademark.id,
                "admin_release" if administrator and actor_id != previous_owner_id else "release",
                actor_id,
                actor_name,
                now,
                from_user_id=previous_owner_id,
                from_user_name=previous_owner_name,
            )
            return released

    def create_exchange(
        self,
        guild_id: int,
        sender_id: int,
        sender_name: str,
        recipient_id: int,
        recipient_name: str,
        offered_trademark_ids: Iterable[str],
        requested_trademark_ids: Iterable[str],
        source_channel_id: int,
        config: TrademarkGuildConfig,
    ) -> TrademarkRequest:
        return self._create_request(
            guild_id,
            "exchange",
            sender_id,
            sender_name,
            recipient_id,
            recipient_name,
            self._exchange_mark_ids(offered_trademark_ids),
            self._exchange_mark_ids(requested_trademark_ids),
            source_channel_id,
            config,
        )

    def create_gift(
        self,
        guild_id: int,
        sender_id: int,
        sender_name: str,
        recipient_id: int,
        recipient_name: str,
        offered_trademark_id: str,
        source_channel_id: int,
        config: TrademarkGuildConfig,
    ) -> TrademarkRequest:
        return self._create_request(
            guild_id,
            "gift",
            sender_id,
            sender_name,
            recipient_id,
            recipient_name,
            (str(offered_trademark_id),),
            (),
            source_channel_id,
            config,
        )

    def _create_request(
        self,
        guild_id: int,
        request_type: Literal["exchange", "gift"],
        sender_id: int,
        sender_name: str,
        recipient_id: int,
        recipient_name: str,
        offered_trademark_ids: tuple[str, ...],
        requested_trademark_ids: tuple[str, ...],
        source_channel_id: int,
        config: TrademarkGuildConfig,
    ) -> TrademarkRequest:
        if sender_id == recipient_id:
            raise TrademarkInvalidRequest("Нельзя отправить запрос самому себе")
        if request_type == "exchange":
            if not requested_trademark_ids:
                raise TrademarkInvalidRequest("Для обмена нужна хотя бы одна трейд марка с каждой стороны")
            if set(offered_trademark_ids) & set(requested_trademark_ids):
                raise TrademarkInvalidRequest("Одна трейд марка не может находиться с обеих сторон обмена")
        elif len(offered_trademark_ids) != 1 or requested_trademark_ids:
            raise TrademarkInvalidRequest("Подарок должен содержать одну трейд марку")
        now = self.now()
        expiry_hours = config.exchange_expiry_hours if request_type == "exchange" else config.gift_expiry_hours
        request_limit = config.active_exchange_limit if request_type == "exchange" else config.active_gift_limit
        with self.db.connection() as conn:
            mark_ids = (*offered_trademark_ids, *requested_trademark_ids)
            self.repository.lock_marks(conn, guild_id, mark_ids)
            self.repository.lock_users(conn, guild_id, [sender_id, recipient_id])
            self.repository.expire_requests(conn, guild_id, now, sender_id)
            if self.repository.pending_sent_count(conn, guild_id, sender_id, request_type) >= request_limit:
                raise TrademarkRequestLimitReached(f"Достигнут лимит активных запросов: {request_limit}")
            locked = {item_id: self.repository.get_by_id(conn, guild_id, item_id, for_update=True) for item_id in sorted(set(mark_ids))}
            offered = _present_trademarks(offered_trademark_ids, locked)
            requested = _present_trademarks(requested_trademark_ids, locked)
            if len(offered) != len(offered_trademark_ids) or any(item.owner_id != sender_id for item in offered):
                raise TrademarkInvalidRequest("Одна из предлагаемых трейд марок больше не принадлежит отправителю")
            if request_type == "exchange" and (
                len(requested) != len(requested_trademark_ids) or any(item.owner_id != recipient_id for item in requested)
            ):
                raise TrademarkInvalidRequest("Одна из запрошенных трейд марок больше не принадлежит получателю")
            if self.repository.pending_duplicate(
                conn,
                guild_id,
                request_type,
                sender_id,
                recipient_id,
                offered_trademark_ids,
                requested_trademark_ids,
            ):
                raise TrademarkDuplicateRequest("Такой активный запрос уже существует")
            return self.repository.insert_request(
                conn,
                self.request_id_factory(),
                guild_id,
                request_type,
                sender_id,
                sender_name,
                recipient_id,
                recipient_name,
                offered_trademark_ids,
                requested_trademark_ids,
                source_channel_id,
                now,
                now + timedelta(hours=expiry_hours),
            )

    def _exchange_mark_ids(self, trademark_ids: Iterable[str]) -> tuple[str, ...]:
        ids = tuple(sorted({str(item) for item in trademark_ids}))
        if not 1 <= len(ids) <= MAX_EXCHANGE_SIDE_MARKS:
            raise TrademarkInvalidRequest(f"С каждой стороны обмена должно быть от 1 до {MAX_EXCHANGE_SIDE_MARKS} трейд марок")
        return ids

    def request_counts(self, guild_id: int, user_id: int) -> tuple[int, int]:
        now = self.now()
        with self.db.connection() as conn:
            self.repository.expire_requests(conn, guild_id, now, user_id)
            incoming = self.repository.pending_incoming_count(conn, guild_id, user_id)
            outgoing_exchange = self.repository.pending_sent_count(conn, guild_id, user_id, "exchange")
            outgoing_gift = self.repository.pending_sent_count(conn, guild_id, user_id, "gift")
            return incoming, outgoing_exchange + outgoing_gift

    def request_page(
        self,
        guild_id: int,
        user_id: int,
        direction: Literal["incoming", "outgoing"],
        limit: int,
        offset: int,
    ) -> tuple[list[TrademarkRequestListing], int]:
        now = self.now()
        with self.db.connection() as conn:
            self.repository.expire_requests(conn, guild_id, now, user_id)
            return self.repository.request_page(conn, guild_id, user_id, direction, limit, offset)

    def request(self, guild_id: int, request_id: str) -> tuple[TrademarkRequest, tuple[Trademark, ...], tuple[Trademark, ...]]:
        with self.db.connection() as conn:
            request = self.repository.get_request(conn, guild_id, request_id)
            if request is None:
                raise TrademarkNotFound("Запрос не найден")
            offered = tuple(
                self.repository.get_many(
                    conn,
                    guild_id,
                    request.offered_trademark_ids,
                )
            )
            requested = tuple(
                self.repository.get_many(
                    conn,
                    guild_id,
                    request.requested_trademark_ids,
                )
            )
            if len(offered) != len(request.offered_trademark_ids) or len(requested) != len(request.requested_trademark_ids):
                raise TrademarkNotFound("Трейд марка запроса не найдена")
            return request, offered, requested

    def decline_request(
        self,
        guild_id: int,
        request_id: str,
        user_id: int,
        user_name: str,
    ) -> TrademarkRequest:
        return self._close_request(guild_id, request_id, user_id, user_name, "declined", recipient=True)

    def cancel_request(
        self,
        guild_id: int,
        request_id: str,
        user_id: int,
        user_name: str,
    ) -> TrademarkRequest:
        return self._close_request(guild_id, request_id, user_id, user_name, "cancelled", recipient=False)

    def _close_request(
        self,
        guild_id: int,
        request_id: str,
        user_id: int,
        user_name: str,
        status: str,
        *,
        recipient: bool,
    ) -> TrademarkRequest:
        now = self.now()
        expired = False
        with self.db.connection() as conn:
            request = self.repository.get_request(conn, guild_id, request_id, for_update=True)
            if request is None:
                raise TrademarkNotFound("Запрос не найден")
            expected_user = request.recipient_id if recipient else request.sender_id
            if expected_user != user_id:
                raise TrademarkPermissionDenied("Этот запрос принадлежит другому пользователю")
            if request.status != "pending":
                raise TrademarkInvalidRequest("Запрос уже обработан")
            if request.expires_at <= now:
                result = self.repository.resolve_request(
                    conn,
                    guild_id,
                    request_id,
                    "expired",
                    now,
                    user_id,
                    user_name,
                )
                expired = True
            else:
                result = self.repository.resolve_request(
                    conn,
                    guild_id,
                    request_id,
                    status,
                    now,
                    user_id,
                    user_name,
                )
        if expired:
            raise TrademarkInvalidRequest("Срок действия запроса истёк")
        return result

    def accept_request(
        self,
        guild_id: int,
        request_id: str,
        user_id: int,
        user_name: str,
        config: TrademarkGuildConfig,
    ) -> TransferResult:
        now = self.now()
        with self.db.connection() as conn:
            request = self.repository.get_request(conn, guild_id, request_id)
            if request is None:
                raise TrademarkNotFound("Запрос не найден")
            if request.recipient_id != user_id:
                raise TrademarkPermissionDenied("Этот запрос предназначен другому пользователю")
            mark_ids = (
                *request.offered_trademark_ids,
                *request.requested_trademark_ids,
            )
            self.repository.lock_marks(conn, guild_id, mark_ids)
            self.repository.lock_users(conn, guild_id, [request.sender_id, request.recipient_id])
            request = self.repository.get_request(conn, guild_id, request_id, for_update=True)
            if request is None:
                raise TrademarkNotFound("Запрос не найден")
            if request.recipient_id != user_id:
                raise TrademarkPermissionDenied("Этот запрос предназначен другому пользователю")
            if request.status != "pending":
                return TransferResult(status="already_processed", request=request)
            if request.expires_at <= now:
                request = self.repository.resolve_request(
                    conn,
                    guild_id,
                    request.id,
                    "expired",
                    now,
                    user_id,
                    user_name,
                )
                return TransferResult(status="expired", request=request)
            locked = {item_id: self.repository.get_by_id(conn, guild_id, item_id, for_update=True) for item_id in sorted(set(mark_ids))}
            offered = _present_trademarks(request.offered_trademark_ids, locked)
            requested = _present_trademarks(request.requested_trademark_ids, locked)
            if len(offered) != len(request.offered_trademark_ids) or any(item.owner_id != request.sender_id for item in offered):
                self.repository.resolve_request(
                    conn,
                    guild_id,
                    request.id,
                    "invalidated",
                    now,
                    user_id,
                    user_name,
                )
                return TransferResult(
                    status="invalid",
                    request=request,
                    offered=offered,
                    requested=requested,
                )

            if request.request_type == "gift":
                offered_item = offered[0]
                if self.repository.owned_count(conn, guild_id, request.recipient_id) + 1 > config.inventory_limit:
                    return TransferResult(
                        status="inventory_full",
                        request=request,
                        offered=offered,
                    )
                transferred = self.repository.transfer(
                    conn,
                    offered_item.id,
                    request.recipient_id,
                    request.recipient_name,
                    now,
                )
                self.repository.remove_showcase_marks(conn, guild_id, [offered_item.id])
                self.repository.insert_event(
                    conn,
                    guild_id,
                    offered_item.id,
                    "gift",
                    user_id,
                    user_name,
                    now,
                    from_user_id=request.sender_id,
                    from_user_name=request.sender_name,
                    to_user_id=request.recipient_id,
                    to_user_name=request.recipient_name,
                )
                self.repository.resolve_request(
                    conn,
                    guild_id,
                    request.id,
                    "accepted",
                    now,
                    user_id,
                    user_name,
                )
                self.repository.invalidate_requests_for_marks(
                    conn,
                    guild_id,
                    [offered_item.id],
                    now,
                    exclude_request_id=request.id,
                )
                return TransferResult(
                    status="completed",
                    request=request,
                    offered=(transferred,),
                )

            if len(requested) != len(request.requested_trademark_ids) or any(item.owner_id != request.recipient_id for item in requested):
                self.repository.resolve_request(
                    conn,
                    guild_id,
                    request.id,
                    "invalidated",
                    now,
                    user_id,
                    user_name,
                )
                return TransferResult(
                    status="invalid",
                    request=request,
                    offered=offered,
                    requested=requested,
                )

            sender_total = self.repository.owned_count(conn, guild_id, request.sender_id)
            recipient_total = self.repository.owned_count(conn, guild_id, request.recipient_id)
            sender_after = sender_total - len(offered) + len(requested)
            recipient_after = recipient_total - len(requested) + len(offered)
            if sender_after > config.inventory_limit or recipient_after > config.inventory_limit:
                return TransferResult(
                    status="inventory_full",
                    request=request,
                    offered=offered,
                    requested=requested,
                )

            received_by_recipient = tuple(
                self.repository.transfer(
                    conn,
                    item.id,
                    request.recipient_id,
                    request.recipient_name,
                    now,
                )
                for item in offered
            )
            received_by_sender = tuple(
                self.repository.transfer(
                    conn,
                    item.id,
                    request.sender_id,
                    request.sender_name,
                    now,
                )
                for item in requested
            )
            self.repository.remove_showcase_marks(conn, guild_id, mark_ids)
            requested_names = "™, ".join(item.display_name for item in requested)
            offered_names = "™, ".join(item.display_name for item in offered)
            for item in offered:
                self.repository.insert_event(
                    conn,
                    guild_id,
                    item.id,
                    "exchange",
                    user_id,
                    user_name,
                    now,
                    from_user_id=request.sender_id,
                    from_user_name=request.sender_name,
                    to_user_id=request.recipient_id,
                    to_user_name=request.recipient_name,
                    related_trademark_id=requested[0].id,
                    related_trademark_name=requested_names,
                )
            for item in requested:
                self.repository.insert_event(
                    conn,
                    guild_id,
                    item.id,
                    "exchange",
                    user_id,
                    user_name,
                    now,
                    from_user_id=request.recipient_id,
                    from_user_name=request.recipient_name,
                    to_user_id=request.sender_id,
                    to_user_name=request.sender_name,
                    related_trademark_id=offered[0].id,
                    related_trademark_name=offered_names,
                )
            self.repository.resolve_request(
                conn,
                guild_id,
                request.id,
                "accepted",
                now,
                user_id,
                user_name,
            )
            self.repository.invalidate_requests_for_marks(
                conn,
                guild_id,
                mark_ids,
                now,
                exclude_request_id=request.id,
            )
            return TransferResult(
                status="completed",
                request=request,
                offered=received_by_recipient,
                requested=received_by_sender,
            )
