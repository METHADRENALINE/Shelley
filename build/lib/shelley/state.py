from __future__ import annotations

import time
from typing import Any

from .db import Database, get_database
from .settings import get_config


def _guild_id(value: int | str | None = None) -> int:
    if isinstance(value, int) and value > 0:
        return int(value)
    try:
        parsed = int(str(value))
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return get_config().runtime_guild_id()


class StateRepository:
    def __init__(self, db: Database, guild_id: int) -> None:
        self.db = db
        self.guild_id = int(guild_id)

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.fetchone(
            "SELECT value FROM shelley_state WHERE guild_id = %s AND key = %s",
            (self.guild_id, str(key)),
        )
        if row is None:
            return default
        return row["value"]

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            """
            INSERT INTO shelley_state (guild_id, key, value, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (guild_id, key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (self.guild_id, str(key), self.db.jsonb(value)),
        )

    def delete(self, key: str) -> None:
        self.db.execute(
            "DELETE FROM shelley_state WHERE guild_id = %s AND key = %s",
            (self.guild_id, str(key)),
        )


def state_repository(guild_id: int | str | None = None) -> StateRepository:
    config = get_config()
    return StateRepository(get_database(config), _guild_id(guild_id))


def get_status_message_id(guild_id: int | str | None, key: str, use_legacy_fallback: bool = False) -> int | None:
    repo = state_repository(guild_id)
    ids = repo.get("status_message_ids", {})
    if isinstance(ids, dict):
        try:
            message_id = int(ids.get(str(key), 0))
            if message_id:
                return message_id
        except (TypeError, ValueError):
            pass
    if use_legacy_fallback:
        try:
            message_id = int(repo.get("message_id", 0))
            if message_id:
                return message_id
        except (TypeError, ValueError):
            pass
    return None


def set_status_message_id(guild_id: int | str | None, message_id: int, key: str | None = None) -> None:
    repo = state_repository(guild_id)
    if key is None:
        repo.set("message_id", int(message_id))
        return
    ids = repo.get("status_message_ids", {})
    if not isinstance(ids, dict):
        ids = {}
    ids[str(key)] = int(message_id)
    repo.set("status_message_ids", ids)


def normalize_status_message_ids_for_embed_layout(guild_id: int | str | None) -> None:
    repo = state_repository(guild_id)
    ids = repo.get("status_message_ids", {})
    if not isinstance(ids, dict):
        return
    old_bm_id = ids.get("bm")
    old_bm_info_id = ids.get("bm_info")
    if not ids.get("smp_interval") and old_bm_id and old_bm_info_id:
        ids["smp_interval"] = int(old_bm_id)
        ids["bm"] = int(old_bm_info_id)
        ids.pop("bm_info", None)
        repo.set("status_message_ids", ids)


def get_welcome_message_id(guild_id: int | str | None) -> int | None:
    try:
        message_id = int(state_repository(guild_id).get("welcome_message_id", 0))
        if message_id:
            return message_id
    except (TypeError, ValueError):
        pass
    return None


def set_welcome_message_id(guild_id: int | str | None, message_id: int) -> None:
    state_repository(guild_id).set("welcome_message_id", int(message_id))


def get_welcome_message_file_hash(guild_id: int | str | None) -> str | None:
    value = state_repository(guild_id).get("welcome_message_file_hash")
    return str(value) if value else None


def set_welcome_message_file_hash(guild_id: int | str | None, file_hash: str) -> None:
    state_repository(guild_id).set("welcome_message_file_hash", str(file_hash))


def mark_starting_status(guild_id: int | str | None, placeholder: str | None, ttl_seconds: int) -> None:
    if not placeholder:
        return
    repo = state_repository(guild_id)
    starting = repo.get("starting_statuses", {})
    if not isinstance(starting, dict):
        starting = {}
    starting[str(placeholder)] = time.time() + max(1, int(ttl_seconds))
    repo.set("starting_statuses", starting)


def clear_starting_status(guild_id: int | str | None, placeholder: str) -> None:
    repo = state_repository(guild_id)
    starting = repo.get("starting_statuses", {})
    if not isinstance(starting, dict) or placeholder not in starting:
        return
    starting.pop(placeholder, None)
    repo.set("starting_statuses", starting)


def load_active_starting_statuses(guild_id: int | str | None) -> set[str]:
    repo = state_repository(guild_id)
    raw = repo.get("starting_statuses", {})
    if not isinstance(raw, dict):
        return set()
    now = time.time()
    active: dict[str, float] = {}
    changed = False
    for placeholder, expires_at in raw.items():
        try:
            expires = float(expires_at)
        except (TypeError, ValueError):
            changed = True
            continue
        if expires > now:
            active[str(placeholder)] = expires
        else:
            changed = True
    if changed:
        repo.set("starting_statuses", active)
    return set(active)


def apply_status_version_memory(guild_id: int | str | None, snapshots: dict[str, dict[str, Any]]) -> None:
    repo = state_repository(guild_id)
    remembered = repo.get("last_online_versions", {})
    if not isinstance(remembered, dict):
        remembered = {}
    changed = False
    for placeholder, snapshot in snapshots.items():
        key = str(placeholder)
        status = str(snapshot.get("status", ""))
        version = str(snapshot.get("version") or "").strip()
        if status == ":green_circle:" and version and version.lower() != "unknown":
            if remembered.get(key) != version:
                remembered[key] = version
                changed = True
        elif not version or version.lower() == "unknown":
            fallback = str(remembered.get(key) or "").strip()
            if fallback:
                snapshot["version"] = fallback
    if changed:
        repo.set("last_online_versions", remembered)


def get_star_forward(guild_id: int | str | None, source_channel_id: int, source_message_id: int) -> int | None:
    db = get_database(get_config())
    row = db.fetchone(
        """
        SELECT forwarded_message_id
        FROM shelley_star_forwards
        WHERE guild_id = %s AND source_channel_id = %s AND source_message_id = %s
        """,
        (_guild_id(guild_id), int(source_channel_id), int(source_message_id)),
    )
    if row is None:
        return None
    return int(row["forwarded_message_id"])


def set_star_forward(
    guild_id: int | str | None,
    source_channel_id: int,
    source_message_id: int,
    target_channel_id: int,
    forwarded_message_id: int,
) -> None:
    db = get_database(get_config())
    db.execute(
        """
        INSERT INTO shelley_star_forwards (
            guild_id, source_channel_id, source_message_id, target_channel_id, forwarded_message_id, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (guild_id, source_channel_id, source_message_id)
        DO UPDATE SET
            target_channel_id = EXCLUDED.target_channel_id,
            forwarded_message_id = EXCLUDED.forwarded_message_id,
            updated_at = now()
        """,
        (
            _guild_id(guild_id),
            int(source_channel_id),
            int(source_message_id),
            int(target_channel_id),
            int(forwarded_message_id),
        ),
    )


def delete_star_forward(guild_id: int | str | None, source_channel_id: int, source_message_id: int) -> int | None:
    db = get_database(get_config())
    with db.connection() as conn:
        row = conn.execute(
            """
            DELETE FROM shelley_star_forwards
            WHERE guild_id = %s AND source_channel_id = %s AND source_message_id = %s
            RETURNING forwarded_message_id
            """,
            (_guild_id(guild_id), int(source_channel_id), int(source_message_id)),
        ).fetchone()
        if row is None:
            return None
        return int(row["forwarded_message_id"])


def count_star_forwards(guild_id: int | str | None) -> int:
    row = get_database(get_config()).fetchone(
        "SELECT count(*) AS count FROM shelley_star_forwards WHERE guild_id = %s",
        (_guild_id(guild_id),),
    )
    return int((row or {}).get("count", 0) or 0)
