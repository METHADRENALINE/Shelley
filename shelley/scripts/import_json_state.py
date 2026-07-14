from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db import apply_schema, get_database
from ..services.recovery_log import normalize_recovery_dispatch_type
from ..settings import load_config, reset_config_cache
from ..state import set_star_forward, state_repository


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="import-json-state")
    parser.add_argument("--config", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--points", default=None)
    parser.add_argument("--recovery", default=None)
    parser.add_argument("--guild-id", type=int, default=0)
    return parser.parse_args()


def import_state(state: dict[str, Any], guild_id: int, target_channel_id: int = 0) -> dict[str, int]:
    repo = state_repository(guild_id)
    scalar_keys = [
        "message_id",
        "status_message_ids",
        "starting_statuses",
        "welcome_message_id",
        "welcome_message_file_hash",
        "last_online_versions",
    ]
    state_keys = 0
    for key in scalar_keys:
        if key in state:
            repo.set(key, state[key])
            state_keys += 1
    forwards = state.get("star_forwards", {})
    star_forwards = 0
    if isinstance(forwards, dict):
        for raw_key, raw_forwarded_id in forwards.items():
            try:
                source_channel_id, source_message_id = [int(part) for part in str(raw_key).split(":", 1)]
                forwarded_message_id = int(raw_forwarded_id)
            except (TypeError, ValueError):
                continue
            set_star_forward(guild_id, source_channel_id, source_message_id, target_channel_id, forwarded_message_id)
            star_forwards += 1
    return {"state_keys": state_keys, "star_forwards": star_forwards}


def import_points(points: dict[str, Any], guild_id: int, db) -> dict[str, int]:
    users = points.get("users", {})
    user_count = 0
    text_sum = 0
    voice_sum = 0
    if isinstance(users, dict):
        with db.connection() as conn:
            for raw_user_id, record in users.items():
                if not isinstance(record, dict):
                    continue
                try:
                    user_id = int(raw_user_id)
                except (TypeError, ValueError):
                    continue
                text_points = max(0, int(record.get("text_points", 0) or 0))
                voice_points = max(0, int(record.get("voice_points", 0) or 0))
                text_sum += text_points
                voice_sum += voice_points
                conn.execute(
                    """
                    INSERT INTO shelley_points_users (
                        guild_id, user_id, text_points, voice_points, last_text_award_at, last_voice_award_at,
                        last_text_channel_id, last_text_message_id, last_name, last_display_name, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET
                        text_points = EXCLUDED.text_points,
                        voice_points = EXCLUDED.voice_points,
                        last_text_award_at = EXCLUDED.last_text_award_at,
                        last_voice_award_at = EXCLUDED.last_voice_award_at,
                        last_text_channel_id = EXCLUDED.last_text_channel_id,
                        last_text_message_id = EXCLUDED.last_text_message_id,
                        last_name = EXCLUDED.last_name,
                        last_display_name = EXCLUDED.last_display_name,
                        updated_at = now()
                    """,
                    (
                        guild_id,
                        user_id,
                        text_points,
                        voice_points,
                        float(record.get("last_text_award_at", 0) or 0),
                        float(record.get("last_voice_award_at", 0) or 0),
                        int(record["last_text_channel_id"]) if record.get("last_text_channel_id") else None,
                        int(record["last_text_message_id"]) if record.get("last_text_message_id") else None,
                        record.get("last_name"),
                        record.get("last_display_name"),
                    ),
                )
                user_count += 1
    cursors = points.get("text_channel_cursors", {})
    cursor_count = 0
    if isinstance(cursors, dict):
        with db.connection() as conn:
            for raw_channel_id, raw_message_id in cursors.items():
                try:
                    channel_id = int(raw_channel_id)
                    message_id = int(raw_message_id)
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    """
                    INSERT INTO shelley_text_channel_cursors (guild_id, channel_id, message_id, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (guild_id, channel_id)
                    DO UPDATE SET message_id = EXCLUDED.message_id, updated_at = now()
                    """,
                    (guild_id, channel_id, message_id),
                )
                cursor_count += 1
    if points.get("leaderboard_message_id"):
        state_repository(guild_id).set("points_leaderboard_message_id", int(points["leaderboard_message_id"]))
    return {"users": user_count, "text_points": text_sum, "voice_points": voice_sum, "text_cursors": cursor_count}


def import_recovery(path: Path, guild_id: int, db) -> dict[str, int]:
    if not path.exists():
        return {"recovery_entries": 0}
    inserted = 0
    with db.connection() as conn:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            source_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
            user = entry.get("user", {}) if isinstance(entry.get("user"), dict) else {}
            created_at_unix = int(entry.get("created_at_unix", 0) or 0)
            created_at = datetime.fromtimestamp(created_at_unix).astimezone() if created_at_unix else datetime.now().astimezone()
            dispatch_type = normalize_recovery_dispatch_type(entry.get("dispatch_type"), entry.get("status"), entry.get("returncode"))
            payload = dict(entry)
            payload["dispatch_type"] = dispatch_type
            row = conn.execute(
                """
                INSERT INTO shelley_recovery_controls (
                    guild_id, created_at, created_at_unix, button_id, button_label, target, action, command_key,
                    dispatch_type, status, returncode, error, user_id, user_name, user_display_name, source_hash, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_hash) WHERE source_hash IS NOT NULL DO NOTHING
                RETURNING id
                """,
                (
                    guild_id,
                    created_at,
                    created_at_unix or int(created_at.timestamp()),
                    entry.get("button_id"),
                    entry.get("button_label"),
                    entry.get("target"),
                    entry.get("action"),
                    entry.get("command_key"),
                    dispatch_type,
                    entry.get("status"),
                    entry.get("returncode"),
                    entry.get("error"),
                    int(user["id"]) if user.get("id") is not None else None,
                    user.get("name"),
                    user.get("display_name"),
                    source_hash,
                    db.jsonb(payload),
                ),
            ).fetchone()
            if row:
                inserted += 1
    return {"recovery_entries": inserted}


def verification_counts(guild_id: int, db) -> dict[str, int]:
    row = db.fetchone(
        """
        SELECT
            count(*) AS users,
            COALESCE(sum(text_points), 0) AS text_points,
            COALESCE(sum(voice_points), 0) AS voice_points
        FROM shelley_points_users
        WHERE guild_id = %s
        """,
        (guild_id,),
    )
    star_row = db.fetchone("SELECT count(*) AS count FROM shelley_star_forwards WHERE guild_id = %s", (guild_id,))
    recovery_row = db.fetchone("SELECT count(*) AS count FROM shelley_recovery_controls WHERE guild_id = %s", (guild_id,))
    state_row = db.fetchone("SELECT count(*) AS count FROM shelley_state WHERE guild_id = %s", (guild_id,))
    return {
        "db_users": int((row or {}).get("users", 0) or 0),
        "db_text_points": int((row or {}).get("text_points", 0) or 0),
        "db_voice_points": int((row or {}).get("voice_points", 0) or 0),
        "db_star_forwards": int((star_row or {}).get("count", 0) or 0),
        "db_recovery_entries": int((recovery_row or {}).get("count", 0) or 0),
        "db_state_keys": int((state_row or {}).get("count", 0) or 0),
    }


def main() -> None:
    args = parse_args()
    if args.config:
        os.environ["BOT_CONFIG_PATH"] = args.config
    reset_config_cache()
    config = load_config(args.config, validate_runtime=False)
    db = get_database(config)
    apply_schema(db)
    guild_id = int(args.guild_id or config.runtime_guild_id())
    if guild_id <= 0:
        raise SystemExit("guild id is required for state import")
    state_path = Path(args.state or config.state_path)
    points_path = Path(args.points or config.points.state_path)
    recovery_path = Path(args.recovery or config.recovery_log_path)
    summary = {}
    summary.update(import_state(load_json_file(state_path), guild_id, int(config.star_forward.target_channel_id)))
    summary.update(import_points(load_json_file(points_path), guild_id, db))
    summary.update(import_recovery(recovery_path, guild_id, db))
    summary.update(verification_counts(guild_id, db))
    for key in sorted(summary):
        print(f"{key}={summary[key]}")


if __name__ == "__main__":
    main()
