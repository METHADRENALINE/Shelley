from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..db import Database, get_database
from ..settings import get_config


PointKind = Literal["text", "voice", "all"]


@dataclass(frozen=True)
class PointsRow:
    user_id: int
    text_points: int
    voice_points: int
    last_text_award_at: float
    last_voice_award_at: float
    last_name: str | None = None
    last_display_name: str | None = None

    def points_for(self, field: str) -> int:
        if field == "voice_points":
            return self.voice_points
        return self.text_points


@dataclass(frozen=True)
class PointsAward:
    amount: int
    total: int


def _field_for_kind(kind: PointKind) -> str:
    if kind == "text":
        return "text_points"
    if kind == "voice":
        return "voice_points"
    raise ValueError("kind must be text or voice")


def _award_field_for_kind(kind: PointKind) -> str:
    if kind == "text":
        return "last_text_award_at"
    if kind == "voice":
        return "last_voice_award_at"
    raise ValueError("kind must be text or voice")


class PointsStore:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_database(get_config())

    def _ensure_user(
        self,
        conn,
        guild_id: int,
        user_id: int,
        name: str | None = None,
        display_name: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO shelley_points_users (guild_id, user_id, last_name, last_display_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET
                last_name = COALESCE(EXCLUDED.last_name, shelley_points_users.last_name),
                last_display_name = COALESCE(EXCLUDED.last_display_name, shelley_points_users.last_display_name),
                updated_at = now()
            """,
            (int(guild_id), int(user_id), name, display_name),
        )

    def get_user(self, guild_id: int, user_id: int) -> PointsRow | None:
        row = self.db.fetchone(
            """
            SELECT user_id, text_points, voice_points, last_text_award_at, last_voice_award_at, last_name, last_display_name
            FROM shelley_points_users
            WHERE guild_id = %s AND user_id = %s
            """,
            (int(guild_id), int(user_id)),
        )
        return self._row_to_points(row) if row else None

    def award(
        self,
        guild_id: int,
        user_id: int,
        kind: Literal["text", "voice"],
        amount: int,
        now: float,
        cooldown: float,
        name: str | None = None,
        display_name: str | None = None,
        text_channel_id: int | None = None,
        text_message_id: int | None = None,
    ) -> PointsAward | None:
        field = _field_for_kind(kind)
        award_field = _award_field_for_kind(kind)
        with self.db.connection() as conn:
            self._ensure_user(conn, guild_id, user_id, name, display_name)
            row = conn.execute(
                f"""
                SELECT {field}, {award_field}
                FROM shelley_points_users
                WHERE guild_id = %s AND user_id = %s
                FOR UPDATE
                """,
                (int(guild_id), int(user_id)),
            ).fetchone()
            if row is None:
                return None
            last_award_at = float(row[award_field] or 0)
            if float(now) - last_award_at < float(cooldown):
                return None
            total = int(row[field] or 0) + int(amount)
            if kind == "text":
                conn.execute(
                    """
                    UPDATE shelley_points_users
                    SET text_points = %s,
                        last_text_award_at = %s,
                        last_text_channel_id = %s,
                        last_text_message_id = %s,
                        last_name = COALESCE(%s, last_name),
                        last_display_name = COALESCE(%s, last_display_name),
                        updated_at = now()
                    WHERE guild_id = %s AND user_id = %s
                    """,
                    (
                        total,
                        float(now),
                        text_channel_id,
                        text_message_id,
                        name,
                        display_name,
                        int(guild_id),
                        int(user_id),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE shelley_points_users
                    SET voice_points = %s,
                        last_voice_award_at = %s,
                        last_name = COALESCE(%s, last_name),
                        last_display_name = COALESCE(%s, last_display_name),
                        updated_at = now()
                    WHERE guild_id = %s AND user_id = %s
                    """,
                    (total, float(now), name, display_name, int(guild_id), int(user_id)),
                )
            return PointsAward(amount=int(amount), total=total)

    def top(self, guild_id: int, field: Literal["text_points", "voice_points"], limit: int) -> list[PointsRow]:
        if field not in ("text_points", "voice_points"):
            raise ValueError("field must be text_points or voice_points")
        rows = self.db.fetchall(
            f"""
            SELECT user_id, text_points, voice_points, last_text_award_at, last_voice_award_at, last_name, last_display_name
            FROM shelley_points_users
            WHERE guild_id = %s AND {field} > 0
            ORDER BY {field} DESC, user_id ASC
            LIMIT %s
            """,
            (int(guild_id), max(1, int(limit))),
        )
        return [self._row_to_points(row) for row in rows]

    def get_text_cursor(self, guild_id: int, channel_id: int) -> int:
        row = self.db.fetchone(
            """
            SELECT message_id
            FROM shelley_text_channel_cursors
            WHERE guild_id = %s AND channel_id = %s
            """,
            (int(guild_id), int(channel_id)),
        )
        return int((row or {}).get("message_id", 0) or 0)

    def set_text_cursor(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self.db.execute(
            """
            INSERT INTO shelley_text_channel_cursors (guild_id, channel_id, message_id, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (guild_id, channel_id)
            DO UPDATE SET message_id = GREATEST(shelley_text_channel_cursors.message_id, EXCLUDED.message_id), updated_at = now()
            """,
            (int(guild_id), int(channel_id), int(message_id)),
        )

    def reset_points(self, guild_id: int, kind: PointKind) -> int:
        if kind == "all":
            return self.db.execute(
                """
                UPDATE shelley_points_users
                SET text_points = 0,
                    voice_points = 0,
                    last_text_award_at = 0,
                    last_voice_award_at = 0,
                    updated_at = now()
                WHERE guild_id = %s
                """,
                (int(guild_id),),
            )
        field = _field_for_kind(kind)
        award_field = _award_field_for_kind(kind)
        return self.db.execute(
            f"""
            UPDATE shelley_points_users
            SET {field} = 0,
                {award_field} = 0,
                updated_at = now()
            WHERE guild_id = %s
            """,
            (int(guild_id),),
        )

    def add_points(
        self,
        guild_id: int,
        user_id: int,
        kind: Literal["text", "voice"],
        amount: int,
        name: str | None = None,
        display_name: str | None = None,
    ) -> int:
        field = _field_for_kind(kind)
        with self.db.connection() as conn:
            self._ensure_user(conn, guild_id, user_id, name, display_name)
            row = conn.execute(
                f"""
                UPDATE shelley_points_users
                SET {field} = {field} + %s,
                    last_name = COALESCE(%s, last_name),
                    last_display_name = COALESCE(%s, last_display_name),
                    updated_at = now()
                WHERE guild_id = %s AND user_id = %s
                RETURNING {field}
                """,
                (int(amount), name, display_name, int(guild_id), int(user_id)),
            ).fetchone()
            return int(row[field])

    def remove_points(
        self,
        guild_id: int,
        user_id: int,
        kind: Literal["text", "voice"],
        amount: int,
        name: str | None = None,
        display_name: str | None = None,
    ) -> int:
        field = _field_for_kind(kind)
        with self.db.connection() as conn:
            self._ensure_user(conn, guild_id, user_id, name, display_name)
            row = conn.execute(
                f"""
                UPDATE shelley_points_users
                SET {field} = GREATEST(0, {field} - %s),
                    last_name = COALESCE(%s, last_name),
                    last_display_name = COALESCE(%s, last_display_name),
                    updated_at = now()
                WHERE guild_id = %s AND user_id = %s
                RETURNING {field}
                """,
                (int(amount), name, display_name, int(guild_id), int(user_id)),
            ).fetchone()
            return int(row[field])

    def counts(self, guild_id: int) -> dict[str, int]:
        row = self.db.fetchone(
            """
            SELECT
                count(*) AS users,
                COALESCE(sum(text_points), 0) AS text_points,
                COALESCE(sum(voice_points), 0) AS voice_points
            FROM shelley_points_users
            WHERE guild_id = %s
            """,
            (int(guild_id),),
        )
        return {
            "users": int((row or {}).get("users", 0) or 0),
            "text_points": int((row or {}).get("text_points", 0) or 0),
            "voice_points": int((row or {}).get("voice_points", 0) or 0),
        }

    @staticmethod
    def _row_to_points(row: dict) -> PointsRow:
        return PointsRow(
            user_id=int(row["user_id"]),
            text_points=int(row.get("text_points", 0) or 0),
            voice_points=int(row.get("voice_points", 0) or 0),
            last_text_award_at=float(row.get("last_text_award_at", 0) or 0),
            last_voice_award_at=float(row.get("last_voice_award_at", 0) or 0),
            last_name=row.get("last_name"),
            last_display_name=row.get("last_display_name"),
        )
