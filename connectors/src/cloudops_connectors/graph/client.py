"""Microsoft Graph client: one request per call, ``Retry-After`` honoured absolutely.

    client = GraphClient(auth=..., transport=...)
    body = await client.get("/v1.0/users/delta?$select=id")
    body = await client.get(next_link)          # @odata.nextLink / @odata.deltaLink
    data = await client.download("/v1.0/sites/{site}/drive/items/{id}/content")

Bodies come back untouched; the caller stores them in bronze before anything
parses them. The same code runs against Graph and the fakes; only the httpx
transport (and ``auth``, which the fixtures do not need) differs.

Limits (LLD, Connector contract: "Honour Retry-After absolutely", concurrency 2):

- At most ``CONCURRENCY`` requests in flight per client.
- 429, or 503 with ``Retry-After``: no request leaves this client before the
  wait Graph asked for has elapsed, the throttled one or any other. The wait is
  never shortened: ``core/ratelimit.py`` caps waits at ``MAX_WAIT_SECONDS`` so a
  nonsense header cannot park a worker, but a capped wait would retry early. So
  a wait up to ``max_inline_wait`` (that same cap) is slept in full, and a
  longer one raises ``GraphThrottledError`` carrying it; ``retry_later`` turns
  that into a Temporal error whose next attempt is delayed by exactly that long,
  with no worker held meanwhile.
- ``@odata.nextLink`` and ``@odata.deltaLink`` are absolute URLs from the
  response. They are followed only when they point at the client's own Graph
  origin: a link elsewhere would carry the bearer token to a stranger.
- Any other non-2xx raises ``GraphError`` with Graph's error code (never the
  message text); Temporal's retry policy owns those retries.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from email.utils import parsedate_to_datetime
from typing import Final

import httpx
from temporalio.exceptions import ApplicationError

from cloudops_connectors.core.ratelimit import DEFAULT_RETRY_AFTER, MAX_WAIT_SECONDS

GRAPH: Final = "https://graph.microsoft.com"
CONCURRENCY: Final = 2  # LLD: Microsoft Graph, concurrency 2
MAX_DOWNLOAD_BYTES: Final = 4 * 1024 * 1024
_CODE: Final = re.compile(r"[A-Za-z0-9_.]{1,64}")


class GraphError(Exception):
    """A non-2xx answer other than throttling. ``code`` is Graph's error code
    (``accessDenied``, ``itemNotFound``, ``resyncRequired``), never its message."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"graph answered {status} {code}")
        self.status = status
        self.code = code


class GraphThrottledError(Exception):
    """Graph asked for a wait longer than this client sleeps, or kept refusing."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"throttled by graph; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class ContentTooLargeError(Exception):
    """A download exceeded ``MAX_DOWNLOAD_BYTES``."""


class ForeignLinkError(ValueError):
    """A paging link that leaves the client's Graph origin. Not retryable."""


def retry_after(headers: Mapping[str, str], now: float) -> float | None:
    """Seconds Graph asks for (delta-seconds or an HTTP date), or None.
    ``headers`` must be case-insensitive (httpx.Headers)."""
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(raw).timestamp() - now)
    except (TypeError, ValueError):
        return None


def error_code(body: bytes) -> str:
    try:
        err = json.loads(body).get("error", {})
        code = err.get("code") if isinstance(err, dict) else None
    except (ValueError, AttributeError):
        code = None
    return code if isinstance(code, str) and _CODE.fullmatch(code) else "unknown"


def retry_later(exc: GraphThrottledError) -> ApplicationError:
    """For an activity: Temporal waits exactly what Graph asked before retrying."""
    return ApplicationError(str(exc), type="GraphThrottledError",
                            next_retry_delay=timedelta(seconds=max(exc.retry_after, 1.0)))


class GraphClient:
    def __init__(
        self,
        base_url: str = GRAPH,
        *,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = CONCURRENCY,
        max_inline_wait: float = MAX_WAIT_SECONDS,
        max_throttle_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._origin = httpx.URL(self.base_url)
        self._gate = asyncio.Semaphore(concurrency)
        self._max_inline_wait = max_inline_wait
        self._max_throttle_retries = max_throttle_retries
        self._sleep = sleep
        self._clock = clock
        self._not_before = 0.0
        self._http = httpx.AsyncClient(base_url=self.base_url, auth=auth, transport=transport,
                                       timeout=120.0, headers={"Accept": "application/json"})

    def link(self, url: str) -> str:
        """A path, or an absolute link on this client's origin."""
        if url.startswith("/"):
            return url
        target = httpx.URL(url)
        if (target.scheme, target.host, target.port) != (
                self._origin.scheme, self._origin.host, self._origin.port):
            raise ForeignLinkError("paging link leaves the graph origin")
        return url

    async def _hold(self) -> None:
        """Until the last Retry-After has elapsed, for every request."""
        wait = self._not_before - self._clock()
        if wait > 0:
            await self._sleep(wait)

    async def _send(self, url: str, *, download: bool = False) -> bytes:
        target = self.link(url)
        for attempt in range(self._max_throttle_retries + 1):
            await self._hold()
            async with self._gate, self._http.stream(
                    "GET", target, follow_redirects=download) as resp:
                if resp.status_code < 300:
                    return await _read(resp, MAX_DOWNLOAD_BYTES if download else None)
                body = await resp.aread()
            wait = retry_after(resp.headers, time.time())
            if resp.status_code == 429 or (resp.status_code == 503 and wait is not None):
                wait = DEFAULT_RETRY_AFTER if wait is None else wait
                self._not_before = max(self._not_before, self._clock() + wait)
                if wait > self._max_inline_wait or attempt == self._max_throttle_retries:
                    raise GraphThrottledError(wait)
                continue  # _hold sleeps the whole wait before the next attempt
            raise GraphError(resp.status_code, error_code(body))
        raise GraphThrottledError(DEFAULT_RETRY_AFTER)  # pragma: no cover - loop returns

    async def get(self, url: str) -> bytes:
        return await self._send(url)

    async def download(self, url: str) -> bytes:
        """A file's bytes. Graph answers ``/content`` with a redirect to a
        pre-authenticated URL; httpx drops our Authorization header on the way."""
        return await self._send(url, download=True)

    async def aclose(self) -> None:
        await self._http.aclose()


async def _read(resp: httpx.Response, limit: int | None) -> bytes:
    if limit is None:
        return await resp.aread()
    out = bytearray()
    async for chunk in resp.aiter_bytes():
        out.extend(chunk)
        if len(out) > limit:
            raise ContentTooLargeError(f"download exceeds {limit} bytes")
    return bytes(out)
