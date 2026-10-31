"""Qualys VM API client: host detections and the KnowledgeBase, one page per call.

    client = QualysClient(base_url, auth=..., transport=...)
    body = await client.detections(since, id_min)
    kb = await client.knowledge_base([376190, 91934])

Bodies come back untouched; the caller stores them in bronze before anything
parses them. The same code runs against a subscription and ``FakeQualys``.

- Detections processed after ``since`` (``vm_processed_after``), hosts in id
  order, ``truncation_limit`` hosts per page; the next page starts at the
  ``id_min`` the truncation warning names.
- Limits (LLD: one report pull at a time per customer): Qualys answers 409
  with ``X-RateLimit-ToWait-Sec`` when a rate or concurrency limit is hit. The
  client waits that long and tries again; when ``X-RateLimit-Remaining`` reaches
  zero it waits before the next call instead of spending a refusal.
- Anything else non-2xx raises; Temporal's retry policy owns those retries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Final

import httpx

from cloudops_connectors.core.ratelimit import MAX_WAIT_SECONDS

PAGE_SIZE: Final = 100  # hosts per page
DETECTION_PATH: Final = "/api/2.0/fo/asset/host/vm/detection/"
KB_PATH: Final = "/api/2.0/fo/knowledge_base/vuln/"
DEFAULT_WAIT: Final = 30.0


class ThrottledError(Exception):
    """Still refused after the allowed waits. Retryable by Temporal."""


def to_wait(status: int, headers: Mapping[str, str]) -> float | None:
    """Seconds Qualys asks us to wait, or None. ``headers`` case-insensitive."""
    raw = headers.get("x-ratelimit-towait-sec")
    try:
        wait = float(raw) if raw is not None else None
    except ValueError:
        wait = None
    if status in (409, 429):
        return min(wait if wait is not None and wait >= 0 else DEFAULT_WAIT, MAX_WAIT_SECONDS)
    remaining = headers.get("x-ratelimit-remaining")
    if remaining is not None and remaining.strip() == "0":
        return min(wait if wait is not None and wait >= 0 else DEFAULT_WAIT, MAX_WAIT_SECONDS)
    return None


class QualysClient:
    def __init__(
        self,
        base_url: str,
        *,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        page_size: int = PAGE_SIZE,
        max_throttle_retries: int = 5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self._max_throttle_retries = max_throttle_retries
        self._sleep = sleep
        self._pending_pause = 0.0
        self._http = httpx.AsyncClient(
            base_url=self.base_url, auth=auth, transport=transport, timeout=300.0,
            headers={"X-Requested-With": "cloudops-connector"},
        )

    @staticmethod
    def since_param(since: datetime) -> str:
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        return since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _get(self, path: str, params: dict[str, str]) -> bytes:
        if self._pending_pause > 0:
            await self._sleep(self._pending_pause)
            self._pending_pause = 0.0
        for attempt in range(self._max_throttle_retries + 1):
            response = await self._http.get(path, params=params)
            wait = to_wait(response.status_code, response.headers)
            if response.status_code in (409, 429):
                if attempt == self._max_throttle_retries or wait is None:
                    raise ThrottledError(f"{path}: refused after {attempt + 1} attempts")
                await self._sleep(wait)
                continue
            response.raise_for_status()
            if wait is not None:
                self._pending_pause = wait
            return response.content
        raise ThrottledError(path)  # pragma: no cover - the loop returns or raises

    async def detections(self, since: datetime, id_min: str | None) -> bytes:
        params = {
            "action": "list", "output_format": "XML", "show_tags": "1", "show_asset_id": "1",
            "vm_processed_after": self.since_param(since),
            "truncation_limit": str(self.page_size),
        }
        if id_min is not None:
            params["id_min"] = id_min
        return await self._get(DETECTION_PATH, params)

    async def knowledge_base(self, qids: list[int]) -> bytes:
        return await self._get(KB_PATH, {
            "action": "list", "details": "All", "ids": ",".join(str(q) for q in sorted(qids)),
        })

    async def aclose(self) -> None:
        await self._http.aclose()
