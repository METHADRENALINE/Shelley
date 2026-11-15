from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast

from ..db import Database
from .models import (
    ArchivedMedal,
    Dashboard,
    Draft,
    EraBoard,
    EraSlot,
    MeowError,
    PointSource,
    RankChange,
    Season,
    SeasonDesign,
    SeasonState,
    Showcase,
    TrialShowcase,
    can_update_live,
    next_era_slot,
)


class StaleDraft(MeowError):
    def __init__(self) -> None:
        super().__init__("The draft changed. Refresh the dashboard and try again.")


def _date(value: datetime | str | None) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _season(row: dict[str, Any]) -> Season:
    return Season(
        id=row["id"],
        guild_id=row["guild_id"],
        design=SeasonDesign.model_validate(row["design"]),
        state=cast(SeasonState, row["state"]),
        roles=row["roles"],
        revision=row["revision"],
        draft_revision=row["draft_revision"],
        role_revision=row["role_revision"],
        started_at=_date(row["started_at"]),
        closed_at=_date(row["closed_at"]),
        winner_id=row["winner_id"],
        test_user_id=row["test_user_id"],
        cleaned=row["cleaned"],
    )


def _draft(row: dict[str, Any] | None) -> Draft:
    if row is None:
        return Draft(SeasonDesign(), 0, None)
    return Draft(
        SeasonDesign.model_validate(row["draft"]),
        row["draft_revision"],
        row["draft_season_id"],
    )


class SeasonRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def dashboard(self, guild_id: int, *, include_era: bool = False) -> Dashboard:
        row = self.db.fetchone(
            """
            SELECT g.*, to_jsonb(live) AS live, to_jsonb(preparing) AS preparing,
                   to_jsonb(latest) AS latest
            FROM shelley_meow_guilds g
            LEFT JOIN shelley_meow_seasons live
                ON live.guild_id = g.guild_id AND live.state IN ('active', 'awarding')
            LEFT JOIN shelley_meow_seasons preparing
                ON preparing.guild_id = g.guild_id AND preparing.state = 'preparing'
            LEFT JOIN LATERAL (
                SELECT * FROM shelley_meow_seasons
                WHERE guild_id = g.guild_id AND started_at IS NOT NULL
                ORDER BY id DESC LIMIT 1
            ) latest ON true
            WHERE g.guild_id = %s
            """,
            (guild_id,),
        )
        next_slot = self.suggest_era_slot(guild_id) if include_era else None
        if row is None:
            return Dashboard(_draft(None), None, None, None, next_slot)
        return Dashboard(
            _draft(row),
            _season(row["live"]) if row["live"] else None,
            _season(row["preparing"]) if row["preparing"] else None,
            _season(row["latest"]) if row["latest"] else None,
            next_slot,
        )

    @staticmethod
    def _completed_slots(conn: Any, guild_id: int) -> set[EraSlot]:
        rows = conn.execute(
            """
            SELECT 1 AS era, stage FROM shelley_meow_era_medals WHERE guild_id = %s
            UNION
            SELECT (design #>> '{era_slot,era}')::integer, (design #>> '{era_slot,stage}')::integer
            FROM shelley_meow_seasons
            WHERE guild_id = %s AND state = 'finished' AND design ->> 'era_slot' IS NOT NULL
            """,
            (guild_id, guild_id),
        ).fetchall()
        return {EraSlot(era=row["era"], stage=row["stage"]) for row in rows}

    def suggest_era_slot(self, guild_id: int) -> EraSlot:
        with self.db.connection() as conn:
            return next_era_slot(self._completed_slots(conn, guild_id))

    def _lock_guild(self, conn: Any, guild_id: int) -> dict[str, Any]:
        conn.execute(
            "INSERT INTO shelley_meow_guilds (guild_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (guild_id,),
        )
        return conn.execute(
            "SELECT * FROM shelley_meow_guilds WHERE guild_id = %s FOR UPDATE",
            (guild_id,),
        ).fetchone()

    @staticmethod
    def _live(conn: Any, guild_id: int) -> Season | None:
        row = conn.execute(
            """
            SELECT * FROM shelley_meow_seasons
            WHERE guild_id = %s AND state IN ('active', 'awarding') FOR UPDATE
            """,
            (guild_id,),
        ).fetchone()
        return _season(row) if row else None

    def save_draft(self, guild_id: int, revision: int, design: SeasonDesign, based_on: int | None) -> None:
        with self.db.connection() as conn:
            row = self._lock_guild(conn, guild_id)
            if row["draft_revision"] != revision:
                raise StaleDraft()
            conn.execute(
                """
                UPDATE shelley_meow_guilds
                SET draft = %s, draft_season_id = %s, draft_revision = draft_revision + 1
                WHERE guild_id = %s
                """,
                (self.db.jsonb(design.model_dump()), based_on, guild_id),
            )

    def publish_draft(
        self,
        guild_id: int,
        revision: int,
        expected_live: int | None,
        *,
        update: bool,
        test_user_id: int | None = None,
        expected_slot: EraSlot | None = None,
    ) -> Season:
        with self.db.connection() as conn:
            draft = _draft(self._lock_guild(conn, guild_id))
            if draft.revision != revision:
                raise StaleDraft()
            draft.design.ready()
            if conn.execute(
                "SELECT id FROM shelley_meow_seasons WHERE guild_id = %s AND state = 'preparing'",
                (guild_id,),
            ).fetchone():
                raise MeowError("A season is already being prepared.")
            live = self._live(conn, guild_id)
            if (live.id if live else None) != expected_live:
                raise MeowError("The live season changed. Refresh the dashboard.")
            if live and live.state == "awarding":
                raise MeowError("The winner's medal is still being awarded.")
            completed = self._completed_slots(conn, guild_id)
            slot = draft.design.era_slot or (live.design.era_slot if live else None) or next_era_slot(completed)
            if slot in completed:
                raise MeowError("That era stage already has a winner. Choose another stage.")
            if expected_slot is not None and slot != expected_slot:
                raise MeowError("The suggested era stage changed. Refresh the dashboard.")
            design = draft.design.updated(era_slot=slot)
            if update:
                if live is None or not can_update_live(draft, live):
                    raise MeowError("Adding or removing reward roles requires a new season.")
                row = conn.execute(
                    """
                    UPDATE shelley_meow_seasons
                    SET design = %s, source = %s, revision = revision + 1, draft_revision = %s
                    WHERE id = %s RETURNING *
                    """,
                    (self.db.jsonb(design.model_dump()), design.source, draft.revision, live.id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    INSERT INTO shelley_meow_seasons
                        (guild_id, source, design, state, draft_revision, test_user_id)
                    VALUES (%s, %s, %s, 'preparing', %s, %s)
                    RETURNING *
                    """,
                    (
                        guild_id,
                        design.source,
                        self.db.jsonb(design.model_dump()),
                        draft.revision,
                        test_user_id,
                    ),
                ).fetchone()
            return _season(row)

    def activate(self, guild_id: int, season_id: int) -> bool:
        with self.db.connection() as conn:
            guild = self._lock_guild(conn, guild_id)
            live = self._live(conn, guild_id)
            if live is not None and live.state == "awarding":
                return False
            row = conn.execute(
                """
                SELECT * FROM shelley_meow_seasons
                WHERE id = %s AND guild_id = %s AND state = 'preparing' FOR UPDATE
                """,
                (season_id, guild_id),
            ).fetchone()
            if row is None:
                return False
            season = _season(row)
            if any(reward.key not in season.roles for reward in season.design.rewards):
                return False
            slot = season.design.era_slot
            if slot is not None and slot in self._completed_slots(conn, guild_id):
                raise MeowError("That era stage now has a winner. Cancel setup and choose another stage.")
            if live is not None:
                self._close(conn, guild_id, live.id, "active")
            conn.execute(
                """
                UPDATE shelley_meow_seasons
                SET state = 'active', started_at = clock_timestamp(), role_revision = revision
                WHERE id = %s
                """,
                (season_id,),
            )
            if guild["draft_revision"] == season.draft_revision:
                conn.execute(
                    "UPDATE shelley_meow_guilds SET draft_season_id = %s WHERE guild_id = %s",
                    (season_id, guild_id),
                )
            return True

    @staticmethod
    def _close(conn: Any, guild_id: int, season_id: int, expected: Literal["active", "preparing"]) -> bool:
        row = conn.execute(
            """
            UPDATE shelley_meow_seasons SET state = 'cancelled',
                closed_at = clock_timestamp(), revision = revision + 1
            WHERE guild_id = %s AND id = %s AND state = %s
            """,
            (guild_id, season_id, expected),
        )
        return bool(row.rowcount)

    def cancel_setup(self, guild_id: int, season_id: int) -> None:
        with self.db.connection() as conn:
            self._lock_guild(conn, guild_id)
            if not self._close(conn, guild_id, season_id, "preparing"):
                raise MeowError("That setup is no longer pending. Refresh the dashboard.")

    def end_season(self, guild_id: int, season_id: int) -> None:
        with self.db.connection() as conn:
            self._lock_guild(conn, guild_id)
            if not self._close(conn, guild_id, season_id, "active"):
                raise MeowError("That season is no longer active. Refresh the dashboard.")
            pending = conn.execute(
                "SELECT id FROM shelley_meow_seasons WHERE guild_id = %s AND state = 'preparing'",
                (guild_id,),
            ).fetchone()
            if pending is not None:
                self._close(conn, guild_id, pending["id"], "preparing")

    @staticmethod
    def _points_field(source: PointSource) -> str:
        return "voice_points" if source == "voice" else "text_points"

    @staticmethod
    def _participant_filter(season: Season) -> tuple[str, tuple[int, ...]]:
        if season.test_user_id is None:
            return "", ()
        return "AND user_id = %s", (season.test_user_id,)

    def medal_candidates(self, season: Season, excluded: list[int], *, limit: int = 10) -> list[int]:
        medal = season.design.medal
        if medal is None:
            raise MeowError("The active season has no medal.")
        field = self._points_field(season.design.source)
        participant, extra = self._participant_filter(season)
        rows = self.db.fetchall(
            f"""
            SELECT user_id FROM shelley_points_users
            WHERE guild_id = %s AND {field} >= %s {participant}
                AND user_id <> ALL(%s::bigint[])
            ORDER BY {field} DESC, user_id
            LIMIT %s
            """,
            (season.guild_id, medal.points, *extra, excluded, limit),
        )
        return [row["user_id"] for row in rows]

    def claim_medal(self, season: Season, user_id: int) -> bool:
        medal = season.design.medal
        if medal is None or (season.test_user_id is not None and season.test_user_id != user_id):
            return False
        field = self._points_field(season.design.source)
        return bool(
            self.db.execute(
                f"""
                UPDATE shelley_meow_seasons
                SET state = 'awarding', winner_id = %s, revision = revision + 1
                WHERE id = %s AND guild_id = %s AND state = 'active' AND revision = %s
                    AND EXISTS (
                        SELECT 1 FROM shelley_points_users
                        WHERE guild_id = %s AND user_id = %s AND {field} >= %s
                    )
                """,
                (
                    user_id,
                    season.id,
                    season.guild_id,
                    season.revision,
                    season.guild_id,
                    user_id,
                    medal.points,
                ),
            )
        )

    def sync_rank_targets(self, season: Season) -> None:
        field = self._points_field(season.design.source)
        participant, extra = self._participant_filter(season)
        ranks = season.design.ordered_ranks
        with self.db.connection() as conn:
            if not conn.execute(
                """
                SELECT id FROM shelley_meow_seasons
                WHERE id = %s AND guild_id = %s AND state = 'active' AND revision = %s
                FOR UPDATE
                """,
                (season.id, season.guild_id, season.revision),
            ).fetchone():
                return
            rows = conn.execute(
                f"""
                SELECT user_id, {field} AS points FROM shelley_points_users
                WHERE guild_id = %s {participant} AND {field} >= %s
                """,
                (season.guild_id, *extra, ranks[0].points),
            ).fetchall()
            targets: dict[str, str] = {}
            for row in rows:
                rank = season.design.rank_at(row["points"])
                if rank is not None:
                    targets[str(row["user_id"])] = rank.key
            conn.execute(
                """
                INSERT INTO shelley_meow_role_assignments (season_id, user_id, desired_rank)
                SELECT %s, key::bigint, value FROM jsonb_each_text(%s)
                ON CONFLICT (season_id, user_id) DO UPDATE SET
                    desired_rank = EXCLUDED.desired_rank, retry_at = '-infinity'
                WHERE shelley_meow_role_assignments.desired_rank IS DISTINCT FROM EXCLUDED.desired_rank
                """,
                (season.id, self.db.jsonb(targets)),
            )
            conn.execute(
                """
                UPDATE shelley_meow_role_assignments SET desired_rank = NULL, retry_at = '-infinity'
                WHERE season_id = %s AND desired_rank IS NOT NULL AND user_id <> ALL(%s::bigint[])
                """,
                (season.id, list(map(int, targets))),
            )

    def store_role(self, season_id: int, key: str, role_id: int) -> None:
        self.db.execute(
            "UPDATE shelley_meow_seasons SET roles = roles || %s WHERE id = %s",
            (self.db.jsonb({key: role_id}), season_id),
        )

    def mark_roles_synced(self, season_id: int, revision: int) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_seasons SET role_revision = %s
            WHERE id = %s AND revision = %s
            """,
            (revision, season_id, revision),
        )

    def invalidate_role(self, guild_id: int, role_id: int) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_seasons SET role_revision = 0
            WHERE guild_id = %s AND state IN ('active', 'awarding')
                AND EXISTS (SELECT 1 FROM jsonb_each_text(roles) WHERE value = %s)
            """,
            (guild_id, str(role_id)),
        )

    def pending_ranks(self, season_id: int) -> list[RankChange]:
        rows = self.db.fetchall(
            """
            SELECT assignment.user_id, assignment.desired_rank
            FROM shelley_meow_role_assignments assignment
            JOIN shelley_meow_seasons season ON season.id = assignment.season_id
            WHERE season.id = %s AND season.state = 'active'
                AND assignment.retry_at <= now()
                AND assignment.applied_role_id IS DISTINCT FROM
                    (season.roles ->> assignment.desired_rank)::bigint
            ORDER BY assignment.retry_at, assignment.user_id
            LIMIT 20
            """,
            (season_id,),
        )
        return [RankChange(row["user_id"], row["desired_rank"]) for row in rows]

    def refresh_member_rank(self, guild_id: int, user_id: int, *, rejoined: bool) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_role_assignments assignment
            SET retry_at = '-infinity',
                applied_role_id = CASE WHEN %s THEN NULL ELSE assignment.applied_role_id END
            FROM shelley_meow_seasons season
            WHERE season.id = assignment.season_id AND season.guild_id = %s
                AND season.state = 'active' AND assignment.user_id = %s
            """,
            (rejoined, guild_id, user_id),
        )

    def assigned_rank_ids(self, season_id: int) -> dict[int, int]:
        rows = self.db.fetchall(
            """
            SELECT user_id, applied_role_id FROM shelley_meow_role_assignments
            WHERE season_id = %s AND applied_role_id IS NOT NULL
            """,
            (season_id,),
        )
        return {row["user_id"]: row["applied_role_id"] for row in rows}

    def earned_medal_roles(self, guild_id: int, user_id: int | None = None, *, joined_at: datetime | None = None) -> dict[int, set[int]]:
        rows = self.db.fetchall(
            """
            SELECT winner_id, role_id FROM (
                SELECT winner_id, (roles ->> (design -> 'medal' ->> 'key'))::bigint AS role_id,
                       closed_at AS earned_at
                FROM shelley_meow_seasons WHERE guild_id = %s AND state = 'finished'
                UNION ALL
                SELECT winner_id, role_id, recorded_at FROM shelley_meow_era_medals
                WHERE guild_id = %s
            ) rewards
            WHERE winner_id IS NOT NULL AND role_id IS NOT NULL
                AND (%s::bigint IS NULL OR winner_id = %s)
                AND (%s::timestamptz IS NULL OR earned_at < %s)
            """,
            (guild_id, guild_id, user_id, user_id, joined_at, joined_at),
        )
        medals: dict[int, set[int]] = {}
        for row in rows:
            if row["role_id"] is not None:
                medals.setdefault(row["winner_id"], set()).add(row["role_id"])
        return medals

    def era_medals(self, guild_id: int) -> list[ArchivedMedal]:
        rows = self.db.fetchall(
            """
            SELECT stage, winner_id, username, role_id, medal_emoji, season_emoji
            FROM shelley_meow_era_medals WHERE guild_id = %s ORDER BY stage
            """,
            (guild_id,),
        )
        return [ArchivedMedal(**row) for row in rows]

    def era_results(self, guild_id: int) -> dict[int, list[ArchivedMedal]]:
        with self.db.connection() as conn:
            historic = conn.execute(
                """
                SELECT stage, winner_id, username, role_id, medal_emoji, season_emoji
                FROM shelley_meow_era_medals WHERE guild_id = %s ORDER BY stage
                """,
                (guild_id,),
            ).fetchall()
            seasons = conn.execute(
                """
                SELECT design, winner_id, winner_username, roles
                FROM shelley_meow_seasons
                WHERE guild_id = %s AND state = 'finished' AND design ->> 'era_slot' IS NOT NULL
                """,
                (guild_id,),
            ).fetchall()
        results = {1: [ArchivedMedal(**row) for row in historic]} if historic else {}
        for row in seasons:
            design = SeasonDesign.model_validate(row["design"])
            slot, medal = design.era_slot, design.medal
            if slot is None or medal is None or not row["winner_username"]:
                continue
            role_id = row["roles"].get(medal.key)
            if role_id is None:
                continue
            results.setdefault(slot.era, []).append(
                ArchivedMedal(
                    slot.stage,
                    row["winner_id"],
                    row["winner_username"],
                    role_id,
                    medal.emoji,
                    design.emoji,
                )
            )
        return {era: sorted(rows, key=lambda row: row.stage) for era, rows in sorted(results.items())}

    def mark_rank_applied(self, season_id: int, user_id: int, role_id: int | None) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_role_assignments SET applied_role_id = %s, retry_at = '-infinity'
            WHERE season_id = %s AND user_id = %s
            """,
            (role_id, season_id, user_id),
        )

    def retry_rank_later(self, season_id: int, user_id: int) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_role_assignments SET retry_at = now() + interval '2 minutes'
            WHERE season_id = %s AND user_id = %s
            """,
            (season_id, user_id),
        )

    def finish(self, season_id: int, winner_username: str) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_seasons SET state = 'finished',
                winner_username = %s, closed_at = clock_timestamp(), revision = revision + 1
            WHERE id = %s AND state = 'awarding'
            """,
            (winner_username, season_id),
        )

    def reopen_medal_award(self, season: Season) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_seasons
            SET state = 'active', winner_id = NULL, revision = revision + 1
            WHERE id = %s AND guild_id = %s AND state = 'awarding' AND winner_id = %s
            """,
            (season.id, season.guild_id, season.winner_id),
        )

    def pending_cleanup(self, guild_id: int) -> list[Season]:
        rows = self.db.fetchall(
            """
            SELECT * FROM shelley_meow_seasons WHERE guild_id = %s
                AND state IN ('finished', 'cancelled') AND NOT cleaned
            ORDER BY id LIMIT 10
            """,
            (guild_id,),
        )
        return [_season(row) for row in rows]

    def mark_cleaned(self, season_id: int) -> None:
        self.db.execute(
            "UPDATE shelley_meow_seasons SET cleaned = true WHERE id = %s",
            (season_id,),
        )

    def showcase(self, guild_id: int) -> Showcase:
        row = self.db.fetchone(
            """
            SELECT showcase_channel_id, showcase_message_id, showcase_season_id,
                   showcase_revision, retired_showcase_message_id
            FROM shelley_meow_guilds WHERE guild_id = %s
            """,
            (guild_id,),
        )
        if row is None:
            return Showcase(None, None, None, 0)
        return Showcase(
            row["showcase_channel_id"],
            row["showcase_message_id"],
            row["showcase_season_id"],
            row["showcase_revision"],
            row["retired_showcase_message_id"],
        )

    def save_showcase(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        season_id: int,
        revision: int,
        *,
        retired_message_id: int | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_guilds
            SET showcase_channel_id = %s, showcase_message_id = %s,
                showcase_season_id = %s, showcase_revision = %s,
                retired_showcase_message_id = COALESCE(%s, retired_showcase_message_id)
            WHERE guild_id = %s
            """,
            (channel_id, message_id, season_id, revision, retired_message_id, guild_id),
        )

    def clear_retired_showcase(self, guild_id: int, message_id: int) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_guilds SET retired_showcase_message_id = NULL,
                trial_message_id = CASE WHEN trial_message_id = %s THEN NULL ELSE trial_message_id END
            WHERE guild_id = %s AND retired_showcase_message_id = %s
            """,
            (message_id, guild_id, message_id),
        )

    def era_archive(self, guild_id: int) -> TrialShowcase:
        row = self.db.fetchone(
            """
            SELECT channel_id, message_id
            FROM shelley_meow_era_boards WHERE guild_id = %s AND era = 1
            """,
            (guild_id,),
        )
        return TrialShowcase(row["channel_id"], row["message_id"]) if row else TrialShowcase(None, None)

    def save_era_archive(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self.save_era_board(guild_id, 1, channel_id, message_id, None)

    def era_boards(self, guild_id: int) -> dict[int, EraBoard]:
        rows = self.db.fetchall(
            """
            SELECT era, channel_id, message_id, signature
            FROM shelley_meow_era_boards WHERE guild_id = %s
            """,
            (guild_id,),
        )
        return {row["era"]: EraBoard(row["channel_id"], row["message_id"], row["signature"]) for row in rows}

    def save_era_board(self, guild_id: int, era: int, channel_id: int, message_id: int, signature: str | None) -> None:
        self.db.execute(
            """
            INSERT INTO shelley_meow_era_boards (guild_id, era, channel_id, message_id, signature)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (guild_id, era) DO UPDATE SET channel_id = EXCLUDED.channel_id,
                message_id = EXCLUDED.message_id, signature = EXCLUDED.signature
            """,
            (guild_id, era, channel_id, message_id, signature),
        )

    def invalidate_showcase(self, guild_id: int, message_id: int) -> None:
        self.db.execute(
            """
            UPDATE shelley_meow_guilds
            SET showcase_revision = CASE WHEN showcase_message_id = %s THEN 0 ELSE showcase_revision END,
                era_archive_message_id = CASE WHEN era_archive_message_id = %s
                    THEN NULL ELSE era_archive_message_id END
            WHERE guild_id = %s AND (showcase_message_id = %s OR era_archive_message_id = %s)
            """,
            (message_id, message_id, guild_id, message_id, message_id),
        )
        self.db.execute(
            """
            UPDATE shelley_meow_era_boards SET message_id = NULL
            WHERE guild_id = %s AND message_id = %s
            """,
            (guild_id, message_id),
        )

    def trial_showcase(self, guild_id: int) -> TrialShowcase:
        row = self.db.fetchone(
            "SELECT trial_channel_id, trial_message_id FROM shelley_meow_guilds WHERE guild_id = %s",
            (guild_id,),
        )
        return TrialShowcase(row["trial_channel_id"], row["trial_message_id"]) if row else TrialShowcase(None, None)

    def save_trial_showcase(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self.db.execute(
            """
            INSERT INTO shelley_meow_guilds (guild_id, trial_channel_id, trial_message_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (guild_id) DO UPDATE SET
                trial_channel_id = EXCLUDED.trial_channel_id,
                trial_message_id = EXCLUDED.trial_message_id
            """,
            (guild_id, channel_id, message_id),
        )
