import asyncio
import json
import os
from datetime import datetime
from typing import Any


_RECOVERY_LOG_LOCK = asyncio.Lock()


def _entry_timestamp(entry: dict[str, Any]) -> float:
    try:
        return float(entry.get("created_at_unix", 0))
    except (TypeError, ValueError):
        return 0.0


def _rewrite_recovery_log(path: str, entry: dict[str, Any], retention_days: int) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    cutoff = float(entry["created_at_unix"]) - max(1, int(retention_days)) * 24 * 60 * 60
    entries: list[dict[str, Any]] = []

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    old_entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(old_entry, dict) and _entry_timestamp(old_entry) >= cutoff:
                    entries.append(old_entry)

    entries.append(entry)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for item in entries:
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            f.write("\n")
    os.replace(tmp, path)


async def append_recovery_control_log(path: str, entry: dict[str, Any], retention_days: int = 365) -> None:
    now = datetime.now().astimezone()
    entry = {
        "created_at": now.isoformat(timespec="seconds"),
        "created_at_unix": int(now.timestamp()),
        **entry,
    }

    async with _RECOVERY_LOG_LOCK:
        await asyncio.to_thread(_rewrite_recovery_log, path, entry, retention_days)
