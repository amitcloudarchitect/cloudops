"""CoreStack API client: findings, one page per call.

    client = CoreStackClient(base_url, auth=..., transport=...)
    body = await client.findings(since, offset)

Bodies come back untouched; the caller stores them in bronze before anything
parses them. The same code runs against a CoreStack account and
``FakeCoreStack``. The endpoint is assumed (``mapping.py``) until the API docs
and a sandbox account exist.

- Findings updated at or after ``since`` (``updated_since``), ordered by
  ``updated_at`` then ``id``, ``limit`` per page from ``offset``.
- Limits (LLD: "conservative default until measured", concurrency 2): at most
  ``max_concurrency`` requests in flight, and a fixed ``min_interval`` between
  the starts of consecutive requests. A 429 waits what ``Retry-After`` asks
  (``core/ratelimit.pause_for``) and tries again; a quota header nearly spent
  pauses before the next call instead of spending a refusal.
- No credential is read here: ``auth`` is an ``httpx.Auth`` the caller
  supplies, and today nothing supplies one (``core/cli.py``, fixtures only).
- Anything else non-2xx raises; Temporal's retry policy owns those retries.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

import httpx

from cloudops_connectors.core.ratelimit import pause_for

PAGE_SIZE: Final = 200
FINDINGS_PATH: Final = "/v1/findings"
MIN_INTERVAL: Final = 2.0  # seconds between request starts, until measured
MAX_CONCURRENCY: Final = 2  # LLD


class ThrottledError(Exception):
    """Still refused after the allowed waits. Retryable by Temporal."""


class CoreStackClient:
    def __init__(
        self,
        base_url: str,
        *,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        page_size: int = PAGE_SIZE,
        min_interval: float = MIN_INTERVAL,
        max_concurrency: int = MAX_CONCURRENCY,
        max_throttle_retries: int = 5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self._min_interval = min_interval
        self._max_throttle_retries = max_throttle_retries
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._slots = asyncio.Semaphore(max_concurrency)
        self._spacing = asyncio.Lock()
        self._last_start: float | None = None
        self._pending_pause = 0.0
        self._http = httpx.AsyncClient(
            base_url=self.base_url, auth=auth, transport=transport, timeout=120.0,
            headers={"Accept": "application/json", "User-Agent": "cloudops-connector"},
        )

    @staticmethod
    def since_param(since: datetime) -> str:
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        return since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _space(self) -> None:
        """Wait out the minimum interval since the last request started, and any
        pause a previous response asked for."""
        async with self._spacing:
            wait = self._pending_pause
            self._pending_pause = 0.0
            if self._last_start is not None:
                wait = max(wait, self._last_start + self._min_interval - self._clock())
            if wait > 0:
                await self._sleep(wait)
            self._last_start = self._clock()

    async def _get(self, path: str, params: dict[str, str]) -> bytes:
        async with self._slots:
            for attempt in range(self._max_throttle_retries + 1):
                await self._space()
                response = await self._http.get(path, params=params)
                pause = pause_for(response.status_code, response.headers, self._wall())
                if response.status_code == 429:
                    if attempt == self._max_throttle_retries or pause is None:
                        raise ThrottledError(f"{path}: refused after {attempt + 1} attempts")
                    self._pending_pause = pause.seconds
                    continue
                response.raise_for_status()
                if pause is not None:
                    self._pending_pause = pause.seconds
                return response.content
        raise ThrottledError(path)  # pragma: no cover - the loop returns or raises

    async def findings(self, since: datetime, offset: str | None) -> bytes:
        return await self._get(FINDINGS_PATH, {
            "updated_since": self.since_param(since),
            "offset": offset or "0",
            "limit": str(self.page_size),
            "sort": "updated_at,id",
        })

    async def aclose(self) -> None:
        await self._http.aclose()
