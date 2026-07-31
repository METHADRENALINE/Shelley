from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..db import Database
from .normalization import trademark_index_values


class TrademarkIndexConflict(RuntimeError):
    pass


def refresh_trademark_index(db: Database) -> int:
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, guild_id, display_name, normalized_name, search_name
            FROM shelley_trademarks
            ORDER BY guild_id, created_at, id
            FOR UPDATE
            """
        ).fetchall()
        indexed: list[tuple[dict[str, Any], str, str]] = []
        by_key: dict[tuple[int, str], list[str]] = defaultdict(list)
        for source in rows:
            row = dict(source)
            comparison_key, search_name = trademark_index_values(str(row["display_name"]))
            indexed.append((row, comparison_key, search_name))
            by_key[(int(row["guild_id"]), comparison_key)].append(str(row["display_name"]))

        conflicts = [(guild_id, names) for (guild_id, _comparison_key), names in by_key.items() if len(names) > 1]
        if conflicts:
            details = "; ".join(f"guild {guild_id}: {', '.join(repr(name) for name in names)}" for guild_id, names in conflicts)
            raise TrademarkIndexConflict(f"Confusable trademark names must be resolved before startup: {details}")

        changed = [
            (row, comparison_key, search_name)
            for row, comparison_key, search_name in indexed
            if row["normalized_name"] != comparison_key or row["search_name"] != search_name
        ]
        for row, _comparison_key, _search_name in changed:
            conn.execute(
                """
                UPDATE shelley_trademarks
                SET normalized_name = %s
                WHERE id = %s
                """,
                (f"pending:{row['id']}", row["id"]),
            )
        for row, comparison_key, search_name in changed:
            conn.execute(
                """
                UPDATE shelley_trademarks
                SET normalized_name = %s,
                    search_name = %s
                WHERE id = %s
                """,
                (comparison_key, search_name, row["id"]),
            )
        return len(changed)
