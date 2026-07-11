from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from .config import BotConfig


class DatabaseUnavailable(RuntimeError):
    pass


def _driver():
    try:
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        from psycopg_pool import ConnectionPool
    except ImportError as e:
        raise DatabaseUnavailable("PostgreSQL driver is not installed. Install the project dependencies first.") from e
    return dict_row, Jsonb, ConnectionPool


class Database:
    def __init__(
        self,
        dsn: str,
        connect_timeout: int = 5,
        pool_min_size: int = 1,
        pool_max_size: int = 5,
    ) -> None:
        self.dsn = str(dsn)
        self.connect_timeout = int(connect_timeout)
        self.pool_min_size = int(pool_min_size)
        self.pool_max_size = int(pool_max_size)
        self._pool: Any | None = None

    def pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        dict_row, _jsonb, ConnectionPool = _driver()
        try:
            pool = ConnectionPool(
                self.dsn,
                min_size=self.pool_min_size,
                max_size=self.pool_max_size,
                open=False,
                kwargs={
                    "connect_timeout": self.connect_timeout,
                    "row_factory": dict_row,
                },
                timeout=self.connect_timeout,
            )
            pool.open(wait=True, timeout=self.connect_timeout)
        except Exception as e:
            raise DatabaseUnavailable("PostgreSQL is unavailable. Check database.url, PostgreSQL service, and permissions.") from e
        self._pool = pool
        return pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @contextmanager
    def connection(self) -> Iterator[Any]:
        try:
            pool = self.pool()
        except Exception as e:
            raise DatabaseUnavailable("PostgreSQL is unavailable. Check database.url, PostgreSQL service, and permissions.") from e

        with pool.connection(timeout=self.connect_timeout) as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

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

    async def fetchone_async(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.fetchone, sql, params)

    async def fetchall_async(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.fetchall, sql, params)

    async def execute_async(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return await asyncio.to_thread(self.execute, sql, params)

    def jsonb(self, value: Any) -> Any:
        _dict_row, Jsonb, _pool = _driver()
        return Jsonb(value)


_DATABASE: Database | None = None


def get_database(config: BotConfig) -> Database:
    global _DATABASE
    dsn = config.database.resolved_url()
    if (
        _DATABASE is None
        or _DATABASE.dsn != dsn
        or _DATABASE.connect_timeout != config.database.connect_timeout
        or _DATABASE.pool_min_size != config.database.pool_min_size
        or _DATABASE.pool_max_size != config.database.pool_max_size
    ):
        if _DATABASE is not None:
            _DATABASE.close()
        _DATABASE = Database(
            dsn,
            config.database.connect_timeout,
            config.database.pool_min_size,
            config.database.pool_max_size,
        )
    return _DATABASE


def reset_database_cache() -> None:
    global _DATABASE
    if _DATABASE is not None:
        _DATABASE.close()
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
