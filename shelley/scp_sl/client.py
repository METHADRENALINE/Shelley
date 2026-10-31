from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import TypeVar

import aiohttp
from pydantic import BaseModel, ValidationError

from .config import ScpSlConfig
from .protocol import Snapshot, Vote, VoteResult

MAX_RESPONSE_BYTES = 64 * 1024
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class ScpSlApiError(Exception):
    pass


class ScpSlClient:
    def __init__(self, config: ScpSlConfig) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        self.config.validate_runtime()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def snapshot(self) -> Snapshot:
        return await self.request("GET", "/status", Snapshot)

    async def vote(self, vote: Vote) -> VoteResult:
        return await self.request("POST", "/vote", VoteResult, vote.model_dump())

    async def request(self, method: str, path: str, model: type[ResponseModel], payload: dict | None = None) -> ResponseModel:
        if self._session is None or self._session.closed:
            raise ScpSlApiError("SCP:SL client is not started")
        token = os.environ.get(self.config.token_env, "")
        if not 32 <= len(token) <= 256 or any(not 33 <= ord(character) <= 126 for character in token):
            raise ScpSlApiError("SCP:SL bearer token is unavailable")
        try:
            async with self._session.request(
                method,
                self.config.api_url + path,
                json=payload,
                headers={"Authorization": "Bearer " + token},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ScpSlApiError("SCP:SL API returned an unsuccessful response")
                if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                    raise ScpSlApiError("SCP:SL API response exceeded the size limit")
                body = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ScpSlApiError("SCP:SL API response exceeded the size limit")
                    body.extend(chunk)
                return model.model_validate_json(bytes(body))
        except (aiohttp.ClientError, TimeoutError, ValidationError, ValueError) as error:
            raise ScpSlApiError("SCP:SL API request failed") from error


class SnapshotCache:
    def __init__(self, stale_seconds: float = 5) -> None:
        self.stale_seconds = min(stale_seconds, 5)
        self.snapshot: Snapshot | None = None
        self._sequence_at = 0.0
        self._received_at = 0.0
        self._available = False

    def update(self, snapshot: Snapshot, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        previous = self.snapshot
        if previous is not None and previous.boot_id == snapshot.boot_id:
            if snapshot.heartbeat_sequence < previous.heartbeat_sequence:
                self.fail()
                return
        if previous is None or (previous.boot_id, previous.heartbeat_sequence) != (snapshot.boot_id, snapshot.heartbeat_sequence):
            self._sequence_at = now
        self.snapshot = snapshot
        self._received_at = now
        self._available = True

    def fail(self) -> None:
        self._available = False

    def current(self, now: float | None = None, wall: datetime | None = None) -> Snapshot | None:
        now = time.monotonic() if now is None else now
        wall = datetime.now(UTC) if wall is None else wall
        snapshot = self.snapshot
        if not self._available or snapshot is None:
            return None
        age = (wall - snapshot.heartbeat_at).total_seconds()
        if not -self.stale_seconds <= age < self.stale_seconds:
            return None
        if now - self._received_at >= self.stale_seconds or now - self._sequence_at >= self.stale_seconds:
            return None
        return snapshot
