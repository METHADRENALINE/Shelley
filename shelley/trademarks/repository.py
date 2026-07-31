from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal, cast

from .models import (
    EventType,
    RequestStatus,
    RequestType,
    Trademark,
    TrademarkEvent,
    TrademarkRequest,
    TrademarkRequestListing,
)

TRADEMARK_COLUMNS = """
id, guild_id, display_name, normalized_name, owner_id, owner_name,
created_at, cycle_started_at, owner_since
"""
REQUEST_COLUMNS = """
id, guild_id, request_type, sender_id, sender_name, recipient_id,
recipient_name, offered_trademark_id, requested_trademark_id,
offered_trademark_ids, requested_trademark_ids,
source_channel_id, status, created_at, expires_at, resolved_at,
resolved_by_id, resolved_by_name
"""
EVENT_COLUMNS = """
id, guild_id, trademark_id, event_type, actor_id, actor_name,
from_user_id, from_user_name, to_user_id, to_user_name,
related_trademark_id, related_trademark_name, created_at
"""
REQUEST_TYPES = frozenset({"exchange", "gift"})
REQUEST_STATUSES = frozenset({"pending", "accepted", "declined", "cancelled", "expired", "invalidated"})
EVENT_TYPES = frozenset({"patent", "release", "admin_release", "gift", "exchange"})


def _row_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _trademark(row: Any) -> Trademark | None:
    data = _row_dict(row)
    if data is None:
        return None
    return Trademark(
        id=str(data["id"]),
        guild_id=int(data["guild_id"]),
        display_name=str(data["display_name"]),
        normalized_name=str(data["normalized_name"]),
        owner_id=int(data["owner_id"]) if data["owner_id"] is not None else None,
        owner_name=str(data["owner_name"]) if data["owner_name"] is not None else None,
        created_at=data["created_at"],
        cycle_started_at=data["cycle_started_at"],
        owner_since=data["owner_since"],
    )


def _required_trademark(row: Any) -> Trademark:
    result = _trademark(row)
    if result is None:
        raise RuntimeError("Expected a trademark row")
    return result


def _request(row: Any) -> TrademarkRequest | None:
    data = _row_dict(row)
    if data is None:
        return None
    request_type = str(data["request_type"])
    status = str(data["status"])
    if request_type not in REQUEST_TYPES:
        raise RuntimeError(f"Invalid trademark request type: {request_type}")
    if status not in REQUEST_STATUSES:
        raise RuntimeError(f"Invalid trademark request status: {status}")
    return TrademarkRequest(
        id=str(data["id"]),
        guild_id=int(data["guild_id"]),
        request_type=cast(RequestType, request_type),
        sender_id=int(data["sender_id"]),
        sender_name=str(data["sender_name"]),
        recipient_id=int(data["recipient_id"]),
        recipient_name=str(data["recipient_name"]),
        offered_trademark_ids=tuple(str(item) for item in data["offered_trademark_ids"]),
        requested_trademark_ids=tuple(str(item) for item in data["requested_trademark_ids"]),
        source_channel_id=int(data["source_channel_id"]),
        status=cast(RequestStatus, status),
        created_at=data["created_at"],
        expires_at=data["expires_at"],
        resolved_at=data["resolved_at"],
        resolved_by_id=(int(data["resolved_by_id"]) if data["resolved_by_id"] is not None else None),
        resolved_by_name=(str(data["resolved_by_name"]) if data["resolved_by_name"] is not None else None),
    )


def _required_request(row: Any) -> TrademarkRequest:
    result = _request(row)
    if result is None:
        raise RuntimeError("Expected a trademark request row")
    return result


def _event(row: Any) -> TrademarkEvent:
    data = dict(row)
    event_type = str(data["event_type"])
    if event_type not in EVENT_TYPES:
        raise RuntimeError(f"Invalid trademark event type: {event_type}")
    return TrademarkEvent(
        id=int(data["id"]),
        guild_id=int(data["guild_id"]),
        trademark_id=str(data["trademark_id"]),
        event_type=cast(EventType, event_type),
        actor_id=int(data["actor_id"]),
        actor_name=str(data["actor_name"]),
        from_user_id=(int(data["from_user_id"]) if data["from_user_id"] is not None else None),
        from_user_name=(str(data["from_user_name"]) if data["from_user_name"] is not None else None),
        to_user_id=(int(data["to_user_id"]) if data["to_user_id"] is not None else None),
        to_user_name=(str(data["to_user_name"]) if data["to_user_name"] is not None else None),
        related_trademark_id=(str(data["related_trademark_id"]) if data["related_trademark_id"] is not None else None),
        related_trademark_name=(str(data["related_trademark_name"]) if data["related_trademark_name"] is not None else None),
        created_at=data["created_at"],
    )


def advisory_key(namespace: str, guild_id: int, value: int | str) -> int:
    digest = hashlib.blake2b(f"{namespace}:{int(guild_id)}:{value}".encode(), digest_size=8).digest()
    unsigned = int.from_bytes(digest, "big")
    return unsigned if unsigned < 2**63 else unsigned - 2**64


class TrademarkRepository:
    def lock_marks(self, conn: Any, guild_id: int, trademark_ids: Iterable[str]) -> None:
        for trademark_id in sorted({str(item) for item in trademark_ids}):
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (advisory_key("trademark-mark", guild_id, trademark_id),),
            )

    def lock_users(self, conn: Any, guild_id: int, user_ids: Iterable[int]) -> None:
        for user_id in sorted({int(item) for item in user_ids}):
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (advisory_key("trademark-user", guild_id, user_id),),
            )

    def lock_name(self, conn: Any, guild_id: int, normalized_name: str) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (advisory_key("trademark-name", guild_id, normalized_name),),
        )

    def get_by_id(self, conn: Any, guild_id: int, trademark_id: str, *, for_update: bool = False) -> Trademark | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = conn.execute(
            f"SELECT {TRADEMARK_COLUMNS} FROM shelley_trademarks WHERE guild_id = %s AND id = %s{suffix}",
            (int(guild_id), str(trademark_id)),
        ).fetchone()
        return _trademark(row)

    def get_by_normalized_name(
        self,
        conn: Any,
        guild_id: int,
        normalized_name: str,
        *,
        for_update: bool = False,
    ) -> Trademark | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = conn.execute(
            f"SELECT {TRADEMARK_COLUMNS} FROM shelley_trademarks WHERE guild_id = %s AND normalized_name = %s{suffix}",
            (int(guild_id), str(normalized_name)),
        ).fetchone()
        return _trademark(row)

    def get_many(self, conn: Any, guild_id: int, trademark_ids: Iterable[str]) -> list[Trademark]:
        ids = tuple(dict.fromkeys(str(item) for item in trademark_ids))
        if not ids:
            return []
        rows = conn.execute(
            f"""
            SELECT {TRADEMARK_COLUMNS}
            FROM shelley_trademarks
            WHERE guild_id = %s AND id = ANY(%s)
            """,
            (int(guild_id), list(ids)),
        ).fetchall()
        trademarks = [_required_trademark(row) for row in rows]
        items = {item.id: item for item in trademarks}
        return [items[item_id] for item_id in ids if item_id in items]

    def owned_page_excluding(
        self,
        conn: Any,
        guild_id: int,
        user_id: int,
        excluded_ids: Iterable[str],
        limit: int,
        offset: int,
    ) -> tuple[list[Trademark], int]:
        excluded = list(dict.fromkeys(str(item) for item in excluded_ids))
        rows = conn.execute(
            f"""
            SELECT {TRADEMARK_COLUMNS}, count(*) OVER() AS total
            FROM shelley_trademarks
            WHERE guild_id = %s
              AND owner_id = %s
              AND NOT (id = ANY(%s))
            ORDER BY created_at, id
            LIMIT %s OFFSET %s
            """,
            (
                int(guild_id),
                int(user_id),
                excluded,
                int(limit),
                int(offset),
            ),
        ).fetchall()
        total = int(rows[0]["total"]) if rows else 0
        return [_required_trademark(row) for row in rows], total

    def insert(
        self,
        conn: Any,
        trademark_id: str,
        guild_id: int,
        display_name: str,
        normalized_name: str,
        search_name: str,
        owner_id: int,
        owner_name: str,
        now: datetime,
    ) -> Trademark | None:
        row = conn.execute(
            f"""
            INSERT INTO shelley_trademarks (
                id, guild_id, display_name, normalized_name, search_name,
                owner_id, owner_name, created_at, cycle_started_at,
                owner_since, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING {TRADEMARK_COLUMNS}
            """,
            (
                trademark_id,
                int(guild_id),
                display_name,
                normalized_name,
                search_name,
                int(owner_id),
                owner_name,
                now,
                now,
                now,
                now,
            ),
        ).fetchone()
        return _trademark(row)

    def claim_free(
        self,
        conn: Any,
        trademark_id: str,
        display_name: str,
        normalized_name: str,
        search_name: str,
        owner_id: int,
        owner_name: str,
        now: datetime,
    ) -> Trademark:
        row = conn.execute(
            f"""
            UPDATE shelley_trademarks
            SET display_name = %s,
                normalized_name = %s,
                search_name = %s,
                owner_id = %s,
                owner_name = %s,
                cycle_started_at = %s,
                owner_since = %s,
                updated_at = %s
            WHERE id = %s AND owner_id IS NULL
            RETURNING {TRADEMARK_COLUMNS}
            """,
            (
                display_name,
                normalized_name,
                search_name,
                int(owner_id),
                owner_name,
                now,
                now,
                now,
                trademark_id,
            ),
        ).fetchone()
        result = _trademark(row)
        if result is None:
            raise RuntimeError("Free trademark could not be claimed")
        return result

    def transfer(
        self,
        conn: Any,
        trademark_id: str,
        owner_id: int,
        owner_name: str,
        now: datetime,
    ) -> Trademark:
        row = conn.execute(
            f"""
            UPDATE shelley_trademarks
            SET owner_id = %s, owner_name = %s, owner_since = %s, updated_at = %s
            WHERE id = %s
            RETURNING {TRADEMARK_COLUMNS}
            """,
            (int(owner_id), owner_name, now, now, trademark_id),
        ).fetchone()
        result = _trademark(row)
        if result is None:
            raise RuntimeError("Trademark transfer did not return a row")
        return result

    def release(self, conn: Any, trademark_id: str, now: datetime) -> Trademark:
        row = conn.execute(
            f"""
            UPDATE shelley_trademarks
            SET owner_id = NULL,
                owner_name = NULL,
                cycle_started_at = NULL,
                owner_since = NULL,
                updated_at = %s
            WHERE id = %s
            RETURNING {TRADEMARK_COLUMNS}
            """,
            (now, trademark_id),
        ).fetchone()
        result = _trademark(row)
        if result is None:
            raise RuntimeError("Trademark release did not return a row")
        return result

    def owned_count(self, conn: Any, guild_id: int, user_id: int) -> int:
        row = conn.execute(
            """
            SELECT count(*) AS total
            FROM shelley_trademarks
            WHERE guild_id = %s AND owner_id = %s
            """,
            (int(guild_id), int(user_id)),
        ).fetchone()
        return int(row["total"])

    def owned_page(
        self,
        conn: Any,
        guild_id: int,
        user_id: int,
        limit: int,
        offset: int,
    ) -> tuple[list[Trademark], int]:
        rows = conn.execute(
            f"""
            SELECT {TRADEMARK_COLUMNS}, count(*) OVER() AS total
            FROM shelley_trademarks
            WHERE guild_id = %s AND owner_id = %s
            ORDER BY created_at, id
            LIMIT %s OFFSET %s
            """,
            (int(guild_id), int(user_id), int(limit), int(offset)),
        ).fetchall()
        total = int(rows[0]["total"]) if rows else 0
        return [_required_trademark(row) for row in rows], total

    def all_page(self, conn: Any, guild_id: int, limit: int, offset: int) -> tuple[list[Trademark], int]:
        rows = conn.execute(
            f"""
            SELECT {TRADEMARK_COLUMNS}, count(*) OVER() AS total
            FROM shelley_trademarks
            WHERE guild_id = %s
            ORDER BY created_at, id
            LIMIT %s OFFSET %s
            """,
            (int(guild_id), int(limit), int(offset)),
        ).fetchall()
        total = int(rows[0]["total"]) if rows else 0
        return [_required_trademark(row) for row in rows], total

    def search(
        self,
        conn: Any,
        guild_id: int,
        identifier_query: str | None,
        comparison_key: str | None,
        search_query: str | None,
        limit: int,
    ) -> list[Trademark]:
        rows = conn.execute(
            f"""
            SELECT {TRADEMARK_COLUMNS}
            FROM shelley_trademarks
            WHERE guild_id = %s
              AND (
                  upper(id) = upper(%s)
                  OR normalized_name = %s
                  OR (
                      CAST(%s AS TEXT) IS NOT NULL
                      AND position(%s in COALESCE(search_name, normalized_name)) > 0
                  )
              )
            ORDER BY
              CASE
                  WHEN upper(id) = upper(%s) OR normalized_name = %s THEN 0
                  ELSE 1
              END,
              created_at,
              id
            LIMIT %s
            """,
            (
                int(guild_id),
                identifier_query,
                comparison_key,
                search_query,
                search_query,
                identifier_query,
                comparison_key,
                int(limit),
            ),
        ).fetchall()
        return [_required_trademark(row) for row in rows]

    def insert_event(
        self,
        conn: Any,
        guild_id: int,
        trademark_id: str,
        event_type: str,
        actor_id: int,
        actor_name: str,
        now: datetime,
        *,
        from_user_id: int | None = None,
        from_user_name: str | None = None,
        to_user_id: int | None = None,
        to_user_name: str | None = None,
        related_trademark_id: str | None = None,
        related_trademark_name: str | None = None,
    ) -> TrademarkEvent:
        row = conn.execute(
            f"""
            INSERT INTO shelley_trademark_events (
                guild_id, trademark_id, event_type, actor_id, actor_name,
                from_user_id, from_user_name, to_user_id, to_user_name,
                related_trademark_id, related_trademark_name, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {EVENT_COLUMNS}
            """,
            (
                int(guild_id),
                trademark_id,
                event_type,
                int(actor_id),
                actor_name,
                int(from_user_id) if from_user_id is not None else None,
                from_user_name,
                int(to_user_id) if to_user_id is not None else None,
                to_user_name,
                related_trademark_id,
                related_trademark_name,
                now,
            ),
        ).fetchone()
        return _event(row)

    def history_page(
        self,
        conn: Any,
        guild_id: int,
        trademark_id: str,
        limit: int,
        offset: int,
    ) -> tuple[list[TrademarkEvent], int]:
        rows = conn.execute(
            f"""
            SELECT {EVENT_COLUMNS}, count(*) OVER() AS total
            FROM shelley_trademark_events
            WHERE guild_id = %s AND trademark_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (int(guild_id), trademark_id, int(limit), int(offset)),
        ).fetchall()
        total = int(rows[0]["total"]) if rows else 0
        return [_event(row) for row in rows], total

    def ensure_window(self, conn: Any, guild_id: int, user_id: int) -> dict[str, Any]:
        conn.execute(
            """
            INSERT INTO shelley_trademark_patent_windows (guild_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id, user_id) DO NOTHING
            """,
            (int(guild_id), int(user_id)),
        )
        row = conn.execute(
            """
            SELECT guild_id, user_id, window_started_at, successful_count,
                   last_patent_at
            FROM shelley_trademark_patent_windows
            WHERE guild_id = %s AND user_id = %s
            FOR UPDATE
            """,
            (int(guild_id), int(user_id)),
        ).fetchone()
        return dict(row)

    def save_window(
        self,
        conn: Any,
        guild_id: int,
        user_id: int,
        window_started_at: datetime,
        successful_count: int,
        last_patent_at: datetime,
        now: datetime,
    ) -> None:
        conn.execute(
            """
            UPDATE shelley_trademark_patent_windows
            SET window_started_at = %s,
                successful_count = %s,
                last_patent_at = %s,
                updated_at = %s
            WHERE guild_id = %s AND user_id = %s
            """,
            (
                window_started_at,
                int(successful_count),
                last_patent_at,
                now,
                int(guild_id),
                int(user_id),
            ),
        )

    def showcase(self, conn: Any, guild_id: int, user_id: int) -> list[Trademark]:
        rows = conn.execute(
            f"""
            SELECT {", ".join(f"t.{column.strip()}" for column in TRADEMARK_COLUMNS.split(","))}
            FROM shelley_trademark_showcase s
            JOIN shelley_trademarks t ON t.id = s.trademark_id
            WHERE s.guild_id = %s AND s.user_id = %s AND t.owner_id = %s
            ORDER BY s.position
            """,
            (int(guild_id), int(user_id), int(user_id)),
        ).fetchall()
        return [_required_trademark(row) for row in rows]

    def add_showcase(
        self,
        conn: Any,
        guild_id: int,
        user_id: int,
        trademark_id: str,
        position: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO shelley_trademark_showcase (
                guild_id, user_id, trademark_id, position
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (guild_id, user_id, trademark_id) DO NOTHING
            """,
            (int(guild_id), int(user_id), trademark_id, int(position)),
        )

    def remove_showcase(self, conn: Any, guild_id: int, user_id: int, trademark_id: str) -> None:
        conn.execute(
            """
            DELETE FROM shelley_trademark_showcase
            WHERE guild_id = %s AND user_id = %s AND trademark_id = %s
            """,
            (int(guild_id), int(user_id), trademark_id),
        )
        self.compact_showcase(conn, guild_id, user_id)

    def remove_showcase_marks(self, conn: Any, guild_id: int, trademark_ids: Iterable[str]) -> None:
        ids = sorted(set(trademark_ids))
        if not ids:
            return
        rows = conn.execute(
            """
            DELETE FROM shelley_trademark_showcase
            WHERE guild_id = %s AND trademark_id = ANY(%s)
            RETURNING user_id
            """,
            (int(guild_id), ids),
        ).fetchall()
        for user_id in sorted({int(row["user_id"]) for row in rows}):
            self.compact_showcase(conn, guild_id, user_id)

    def compact_showcase(self, conn: Any, guild_id: int, user_id: int) -> None:
        conn.execute(
            """
            UPDATE shelley_trademark_showcase
            SET position = position + 1000
            WHERE guild_id = %s AND user_id = %s
            """,
            (int(guild_id), int(user_id)),
        )
        conn.execute(
            """
            WITH ordered AS (
                SELECT trademark_id,
                       row_number() OVER (ORDER BY position, created_at, trademark_id) AS new_position
                FROM shelley_trademark_showcase
                WHERE guild_id = %s AND user_id = %s
            )
            UPDATE shelley_trademark_showcase AS target
            SET position = ordered.new_position
            FROM ordered
            WHERE target.guild_id = %s
              AND target.user_id = %s
              AND target.trademark_id = ordered.trademark_id
            """,
            (int(guild_id), int(user_id), int(guild_id), int(user_id)),
        )

    def move_showcase(
        self,
        conn: Any,
        guild_id: int,
        user_id: int,
        trademark_id: str,
        position: int,
    ) -> None:
        rows = conn.execute(
            """
            SELECT trademark_id
            FROM shelley_trademark_showcase
            WHERE guild_id = %s AND user_id = %s
            ORDER BY position
            """,
            (int(guild_id), int(user_id)),
        ).fetchall()
        ids = [str(row["trademark_id"]) for row in rows]
        if trademark_id not in ids:
            raise ValueError("Trademark is not in the showcase")
        ids.remove(trademark_id)
        ids.insert(max(0, min(int(position) - 1, len(ids))), trademark_id)
        conn.execute(
            """
            UPDATE shelley_trademark_showcase
            SET position = position + 1000
            WHERE guild_id = %s AND user_id = %s
            """,
            (int(guild_id), int(user_id)),
        )
        for index, item in enumerate(ids, start=1):
            conn.execute(
                """
                UPDATE shelley_trademark_showcase
                SET position = %s
                WHERE guild_id = %s AND user_id = %s AND trademark_id = %s
                """,
                (index, int(guild_id), int(user_id), item),
            )

    def expire_requests(self, conn: Any, guild_id: int, now: datetime, user_id: int | None = None) -> int:
        user_clause = ""
        params: list[Any] = [now, int(guild_id), now]
        if user_id is not None:
            user_clause = " AND (sender_id = %s OR recipient_id = %s)"
            params.extend((int(user_id), int(user_id)))
        cursor = conn.execute(
            f"""
            UPDATE shelley_trademark_requests
            SET status = 'expired', resolved_at = %s
            WHERE guild_id = %s
              AND status = 'pending'
              AND expires_at <= %s
              {user_clause}
            """,
            tuple(params),
        )
        return int(cursor.rowcount or 0)

    def pending_sent_count(self, conn: Any, guild_id: int, sender_id: int, request_type: str) -> int:
        row = conn.execute(
            """
            SELECT count(*) AS total
            FROM shelley_trademark_requests
            WHERE guild_id = %s
              AND sender_id = %s
              AND request_type = %s
              AND status = 'pending'
            """,
            (int(guild_id), int(sender_id), request_type),
        ).fetchone()
        return int(row["total"])

    def pending_duplicate(
        self,
        conn: Any,
        guild_id: int,
        request_type: str,
        sender_id: int,
        recipient_id: int,
        offered_trademark_ids: tuple[str, ...],
        requested_trademark_ids: tuple[str, ...],
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM shelley_trademark_requests
            WHERE guild_id = %s
              AND request_type = %s
              AND sender_id = %s
              AND recipient_id = %s
              AND offered_trademark_ids = %s
              AND requested_trademark_ids = %s
              AND status = 'pending'
            """,
            (
                int(guild_id),
                request_type,
                int(sender_id),
                int(recipient_id),
                list(offered_trademark_ids),
                list(requested_trademark_ids),
            ),
        ).fetchone()
        return row is not None

    def pending_incoming_count(self, conn: Any, guild_id: int, recipient_id: int) -> int:
        row = conn.execute(
            """
            SELECT count(*) AS total
            FROM shelley_trademark_requests
            WHERE guild_id = %s AND recipient_id = %s AND status = 'pending'
            """,
            (int(guild_id), int(recipient_id)),
        ).fetchone()
        return int(row["total"])

    def insert_request(
        self,
        conn: Any,
        request_id: str,
        guild_id: int,
        request_type: str,
        sender_id: int,
        sender_name: str,
        recipient_id: int,
        recipient_name: str,
        offered_trademark_ids: tuple[str, ...],
        requested_trademark_ids: tuple[str, ...],
        source_channel_id: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> TrademarkRequest:
        row = conn.execute(
            f"""
            INSERT INTO shelley_trademark_requests (
                id, guild_id, request_type, sender_id, sender_name,
                recipient_id, recipient_name, offered_trademark_id,
                requested_trademark_id, offered_trademark_ids,
                requested_trademark_ids, source_channel_id, created_at, expires_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            RETURNING {REQUEST_COLUMNS}
            """,
            (
                request_id,
                int(guild_id),
                request_type,
                int(sender_id),
                sender_name,
                int(recipient_id),
                recipient_name,
                offered_trademark_ids[0],
                requested_trademark_ids[0] if requested_trademark_ids else None,
                list(offered_trademark_ids),
                list(requested_trademark_ids),
                int(source_channel_id),
                created_at,
                expires_at,
            ),
        ).fetchone()
        result = _request(row)
        if result is None:
            raise RuntimeError("Trademark request insert did not return a row")
        return result

    def get_request(self, conn: Any, guild_id: int, request_id: str, *, for_update: bool = False) -> TrademarkRequest | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = conn.execute(
            f"SELECT {REQUEST_COLUMNS} FROM shelley_trademark_requests WHERE guild_id = %s AND id = %s{suffix}",
            (int(guild_id), request_id),
        ).fetchone()
        return _request(row)

    def request_page(
        self,
        conn: Any,
        guild_id: int,
        user_id: int,
        direction: Literal["incoming", "outgoing"],
        limit: int,
        offset: int,
    ) -> tuple[list[TrademarkRequestListing], int]:
        user_column = "recipient_id" if direction == "incoming" else "sender_id"
        rows = conn.execute(
            f"""
            SELECT r.*,
                   ARRAY(
                       SELECT offered.display_name
                       FROM unnest(r.offered_trademark_ids)
                            WITH ORDINALITY AS ids(trademark_id, position)
                       JOIN shelley_trademarks offered
                         ON offered.id = ids.trademark_id
                       ORDER BY ids.position
                   ) AS offered_names,
                   ARRAY(
                       SELECT requested.display_name
                       FROM unnest(r.requested_trademark_ids)
                            WITH ORDINALITY AS ids(trademark_id, position)
                       JOIN shelley_trademarks requested
                         ON requested.id = ids.trademark_id
                       ORDER BY ids.position
                   ) AS requested_names,
                   count(*) OVER() AS total
            FROM shelley_trademark_requests r
            WHERE r.guild_id = %s
              AND r.{user_column} = %s
              AND r.status = 'pending'
            ORDER BY r.created_at DESC, r.id
            LIMIT %s OFFSET %s
            """,
            (int(guild_id), int(user_id), int(limit), int(offset)),
        ).fetchall()
        total = int(rows[0]["total"]) if rows else 0
        listings = [
            TrademarkRequestListing(
                request=_required_request(row),
                offered_names=tuple(str(item) for item in row["offered_names"]),
                requested_names=tuple(str(item) for item in row["requested_names"]),
            )
            for row in rows
        ]
        return listings, total

    def resolve_request(
        self,
        conn: Any,
        guild_id: int,
        request_id: str,
        status: str,
        now: datetime,
        user_id: int,
        user_name: str,
    ) -> TrademarkRequest:
        row = conn.execute(
            f"""
            UPDATE shelley_trademark_requests
            SET status = %s,
                resolved_at = %s,
                resolved_by_id = %s,
                resolved_by_name = %s
            WHERE guild_id = %s AND id = %s
            RETURNING {REQUEST_COLUMNS}
            """,
            (
                status,
                now,
                int(user_id),
                user_name,
                int(guild_id),
                request_id,
            ),
        ).fetchone()
        result = _request(row)
        if result is None:
            raise RuntimeError("Trademark request update did not return a row")
        return result

    def invalidate_requests_for_marks(
        self,
        conn: Any,
        guild_id: int,
        trademark_ids: Iterable[str],
        now: datetime,
        *,
        exclude_request_id: str | None = None,
    ) -> int:
        ids = sorted(set(trademark_ids))
        if not ids:
            return 0
        exclude_clause = ""
        params: list[Any] = [now, int(guild_id), ids, ids]
        if exclude_request_id is not None:
            exclude_clause = " AND id <> %s"
            params.append(exclude_request_id)
        cursor = conn.execute(
            f"""
            UPDATE shelley_trademark_requests
            SET status = 'invalidated', resolved_at = %s
            WHERE guild_id = %s
              AND status = 'pending'
              AND (
                  offered_trademark_ids && %s
                  OR requested_trademark_ids && %s
              )
              {exclude_clause}
            """,
            tuple(params),
        )
        return int(cursor.rowcount or 0)
