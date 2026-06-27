from __future__ import annotations

from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

from .config import BotConfig


class DatabaseUnavailable(RuntimeError):
    pass


def _driver():
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as e:
        raise DatabaseUnavailable("PostgreSQL driver is not installed. Install the project dependencies first.") from e
    return psycopg, dict_row, Jsonb


class Database:
    def __init__(self, dsn: str, connect_timeout: int = 5) -> None:
        self.dsn = str(dsn)
        self.connect_timeout = int(connect_timeout)

    @contextmanager
    def connection(self) -> Iterator[Any]:
        psycopg, dict_row, _jsonb = _driver()
        try:
            conn = psycopg.connect(
                self.dsn,
                connect_timeout=self.connect_timeout,
                row_factory=dict_row,
            )
        except Exception as e:
            raise DatabaseUnavailable("PostgreSQL is unavailable. Check database.url, PostgreSQL service, and permissions.") from e

        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connection() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.rowcount or 0)

    def jsonb(self, value: Any) -> Any:
        _psycopg, _dict_row, Jsonb = _driver()
        return Jsonb(value)


_DATABASE: Database | None = None


def get_database(config: BotConfig) -> Database:
    global _DATABASE
    dsn = config.database.resolved_url()
    if _DATABASE is None or _DATABASE.dsn != dsn or _DATABASE.connect_timeout != config.database.connect_timeout:
        _DATABASE = Database(dsn, config.database.connect_timeout)
    return _DATABASE


def reset_database_cache() -> None:
    global _DATABASE
    _DATABASE = None


def schema_files() -> list[tuple[str, str]]:
    package = resources.files("shelley.schema")
    files: list[tuple[str, str]] = []
    for item in sorted(package.iterdir(), key=lambda entry: entry.name):
        if item.name.endswith(".sql"):
            files.append((Path(item.name).stem, item.read_text(encoding="utf-8")))
    return files


def apply_schema(db: Database) -> list[str]:
    applied: list[str] = []
    with db.connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS shelley_schema_versions (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )

    for version, sql in schema_files():
        with db.connection() as conn:
            existing = conn.execute("SELECT 1 FROM shelley_schema_versions WHERE version = %s", (version,)).fetchone()
            if existing:
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO shelley_schema_versions (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied
