"""Azure Blob Storage, read-only, for Cost Management exports.

    blobs = BlobClient("https://<account>.blob.core.windows.net", auth=..., transport=...)
    body = await blobs.list("<container>", "<prefix>", marker)
    obj = await blobs.get("<container>", "<blob name>")

Bodies come back untouched; the caller stores them in bronze before anything
parses them. The same code runs against Azure Storage and ``FakeBlobStore``.

- **No credential path.** Requests are authorised by the ``httpx.Auth`` passed
  in (a workload-identity bearer token, when one exists); nothing here reads a
  key or a SAS (CLAUDE.md rule 2, ADR 0003).
- **Throttling**: Storage answers 503 ``ServerBusy`` (or 500 ``OperationTimedOut``)
  when it wants the caller to slow down. The client waits ``base * 2**attempt``
  seconds, capped, half fixed and half jitter, then raises ``ThrottledError``
  for Temporal's backoff to take over.
- A missing blob raises ``BlobNotFoundError`` carrying Storage's response body:
  an export part not delivered yet.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final
from urllib.parse import quote

import httpx

API_VERSION: Final = "2023-11-03"
_BUSY: Final = (b"<Code>ServerBusy</Code>", b"<Code>OperationTimedOut</Code>")


class ThrottledError(Exception):
    """Still refused after the allowed waits. Retryable by Temporal."""


class BlobNotFoundError(Exception):
    """The blob is not in the container (yet). Carries Storage's response body."""

    def __init__(self, name: str, body: bytes) -> None:
        super().__init__(name)
        self.name = name
        self.body = body


@dataclass(frozen=True)
class Blob:
    name: str
    body: bytes
    last_modified: datetime | None


def _busy(response: httpx.Response) -> bool:
    if response.status_code in (429, 503):
        return True
    return response.status_code >= 500 and any(c in response.content[:512] for c in _BUSY)


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _segment(value: str, what: str) -> str:
    if not value or value.startswith("/") or ".." in value.split("/"):
        raise ValueError(f"bad {what}")
    return value


class BlobClient:
    def __init__(
        self,
        account_url: str,
        *,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_results: int = 5000,
        max_throttle_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.account_url = account_url.rstrip("/")
        self.max_results = max_results
        self._retries = max_throttle_retries
        self._base = backoff_base
        self._cap = backoff_cap
        self._sleep = sleep
        self._rand = rand
        self._http = httpx.AsyncClient(base_url=self.account_url, auth=auth,
                                       transport=transport, timeout=300.0,
                                       headers={"x-ms-version": API_VERSION})

    def delay(self, attempt: int) -> float:
        ceiling = float(min(self._cap, self._base * 2.0 ** attempt))
        return ceiling / 2 + ceiling / 2 * self._rand()

    async def _get(self, what: str, url: str, params: dict[str, str] | None) -> httpx.Response:
        for attempt in range(self._retries + 1):
            response = await self._http.get(url, params=params)
            if not _busy(response):
                return response
            if attempt < self._retries:
                await self._sleep(self.delay(attempt))
        raise ThrottledError(f"{what}: throttled after {self._retries + 1} attempts")

    async def list(self, container: str, prefix: str, marker: str | None) -> bytes:
        """One List Blobs page under ``prefix``."""
        params = {"restype": "container", "comp": "list", "prefix": prefix,
                  "maxresults": str(self.max_results)}
        if marker is not None:
            params["marker"] = marker
        response = await self._get("blob list", f"/{_segment(container, 'container')}",
                                   params)
        response.raise_for_status()
        return response.content

    async def get(self, container: str, name: str) -> Blob:
        path = f"/{_segment(container, 'container')}/{quote(_segment(name, 'blob name'))}"
        response = await self._get("blob get", path, None)
        if response.status_code == 404:
            raise BlobNotFoundError(name, response.content)
        response.raise_for_status()
        return Blob(name, response.content, _http_date(response.headers.get("last-modified")))

    async def aclose(self) -> None:
        await self._http.aclose()
