from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from ..db import get_database
from ..settings import get_config


_RECOVERY_LOG_LOCK = asyncio.Lock()


async def append_recovery_control_log(_path: str, entry: dict[str, Any], retention_days: int = 365, guild_id: int = 0) -> None:
    now = datetime.now().astimezone()
    created_at_unix = int(now.timestamp())
    cutoff = created_at_unix - max(1, int(retention_days)) * 24 * 60 * 60
    payload = dict(entry)
    user = payload.pop("user", {}) if isinstance(payload.get("user"), dict) else {}
    config = get_config()
    db = get_database(config)
    async with _RECOVERY_LOG_LOCK:
        with db.connection() as conn:
            conn.execute("DELETE FROM shelley_recovery_controls WHERE created_at_unix < %s", (cutoff,))
            conn.execute(
                """
                INSERT INTO shelley_recovery_controls (
                    guild_id, created_at, created_at_unix, button_id, button_label, target, action, command_key,
                    status, returncode, error, user_id, user_name, user_display_name, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    int(guild_id or config.runtime_guild_id()),
                    now,
                    created_at_unix,
                    payload.get("button_id"),
                    payload.get("button_label"),
                    payload.get("target"),
                    payload.get("action"),
                    payload.get("command_key"),
                    payload.get("status"),
                    payload.get("returncode"),
                    payload.get("error"),
                    int(user["id"]) if user.get("id") is not None else None,
                    user.get("name"),
                    user.get("display_name"),
                    db.jsonb(payload),
                ),
            )
