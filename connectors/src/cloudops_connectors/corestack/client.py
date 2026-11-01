"""CoreStack API client: a token, then recommendation lists and details (ADR 0028).

    client = CoreStackClient(base_url, read_access_key, read_secret, transport=...)
    body = await client.recommendations(tenant, page)        # the list, page by page
    body = await client.recommendation(tenant, rec_id, page)  # one, with its resources

Bodies come back untouched; the caller stores them in bronze before anything
parses them. The same code runs against CoreStack and ``FakeCoreStack``.

**Read-only by construction.** The client can send exactly three requests, each
checked against ``ALLOWED`` before it leaves: the token request (``POST
/v1/auth/tokens``, the API's only way in) and two GETs, the list and a detail.
Anything else, any other method or path, is refused before it is sent. The access
key pair belongs to a CoreStack user whose role decides what it could do; this
client never asks for more than reading recommendations.

- **Token.** ``POST /v1/auth/tokens`` with the access key and secret key returns
  ``token.access_token`` and its ``expires_at``. Kept until a minute before it
  expires; a 401 on a data request fetches a new one once. ``read_secret`` is
  called for every token request, so a rotated secret key (Key Vault, re-mounted
  by the CSI driver) is picked up without a restart. The token goes in
  ``X-Auth-Token``. ``read_access_key`` is called alongside it. Neither key nor
  the token is logged, stored in bronze or put in an exception message.
- **Paging.** Lists and details are numbered pages (``page``, ``limit``); the body
  says ``page_count``.
- **Limits** (LLD: "conservative default until measured", concurrency 2): at most
  ``max_concurrency`` requests in flight and ``min_interval`` between request
  starts. A 429 waits what ``Retry-After`` asks (``core/ratelimit.pause_for``) and
  tries again. Anything else non-2xx raises; Temporal's retry policy owns those.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from cloudops_connectors.core.ratelimit import pause_for

PAGE_SIZE: Final = 100
TOKEN_PATH: Final = "/v1/auth/tokens"  # noqa: S105 - a path
MIN_INTERVAL: Final = 1.0  # seconds between request starts, until measured
MAX_CONCURRENCY: Final = 2  # LLD
REFRESH_MARGIN: Final = 60.0
_ID: Final = r"[A-Za-z0-9-]{1,64}"
ALLOWED: Final = (
    ("POST", re.compile(rf"^{re.escape(TOKEN_PATH)}$")),
    ("GET", re.compile(rf"^/recommendations/{_ID}/service_account/list$")),
    ("GET", re.compile(rf"^/recommendations/{_ID}/get/{_ID}$")),
)


class ThrottledError(Exception):
    """Still refused after the allowed waits. Retryable by Temporal."""


class RefusedRequestError(ValueError):
    """A request outside ``ALLOWED``. Never sent, never retried."""


class TokenError(Exception):
    """CoreStack refused to issue a token. Carries the status only."""


def list_path(tenant: str) -> str:
    return f"/recommendations/{tenant}/service_account/list"


def detail_path(tenant: str, rec_id: str) -> str:
    return f"/recommendations/{tenant}/get/{rec_id}"


def allowed(method: str, path: str) -> bool:
    return any(method == m and p.fullmatch(path) for m, p in ALLOWED)


def _expiry(token: dict[str, Any], now: float, wall: float) -> float:
    """``expires_at`` (CoreStack's naive UTC) as a monotonic deadline; an hour if
    it is missing or unreadable."""
    raw = token.get("expires_at")
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return now + 3600.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return now + max(0.0, moment.timestamp() - wall)


class CoreStackClient:
    def __init__(
        self,
        base_url: str,
        read_access_key: Callable[[], str],
        read_secret: Callable[[], str],
        *,
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
        self._read_access_key = read_access_key
        self._read_secret = read_secret
        self._min_interval = min_interval
        self._max_throttle_retries = max_throttle_retries
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._slots = asyncio.Semaphore(max_concurrency)
        self._spacing = asyncio.Lock()
        self._token_lock = asyncio.Lock()
        self._token: str | None = None
        self._token_until = 0.0
        self._last_start: float | None = None
        self._pending_pause = 0.0
        self._http = httpx.AsyncClient(
            base_url=self.base_url, transport=transport, timeout=120.0,
            headers={"Accept": "application/json", "User-Agent": "cloudops-connector"},
        )

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

    async def _send(self, method: str, path: str, *, params: dict[str, str] | None = None,
                    body: dict[str, str] | None = None,
                    headers: dict[str, str] | None = None) -> httpx.Response:
        """The one place a request leaves this client."""
        if not allowed(method, path):
            raise RefusedRequestError(f"{method} {path.split('?')[0][:80]} is not allowed")
        for attempt in range(self._max_throttle_retries + 1):
            await self._space()
            response = await self._http.request(method, path, params=params, json=body,
                                                 headers=headers)
            pause = pause_for(response.status_code, response.headers, self._wall())
            if response.status_code == 429:
                if attempt == self._max_throttle_retries or pause is None:
                    raise ThrottledError(f"{path}: refused after {attempt + 1} attempts")
                self._pending_pause = pause.seconds
                continue
            if pause is not None:
                self._pending_pause = pause.seconds
            return response
        raise ThrottledError(path)  # pragma: no cover - the loop returns or raises

    async def _bearer(self, stale: str | None = None) -> str:
        async with self._token_lock:
            if (self._token is not None and self._token != stale
                    and self._clock() < self._token_until - REFRESH_MARGIN):
                return self._token
            response = await self._send("POST", TOKEN_PATH, body={
                "access_key": self._read_access_key(), "secret_key": self._read_secret()})
            if response.status_code not in (200, 201):
                raise TokenError(f"token request answered {response.status_code}")
            try:
                token = json.loads(response.content)["token"]
                value = token["access_token"]
            except (ValueError, KeyError, TypeError):
                raise TokenError("token response has no access_token") from None
            if not isinstance(value, str) or not value:
                raise TokenError("token response has no access_token")
            self._token = value
            self._token_until = _expiry(token, self._clock(), self._wall())
            return value

    async def _get(self, path: str, params: dict[str, str]) -> bytes:
        if not allowed("GET", path):  # before a token is fetched for it
            raise RefusedRequestError(f"GET {path[:80]} is not allowed")
        async with self._slots:
            token = await self._bearer()
            response = await self._send("GET", path, params=params,
                                        headers={"X-Auth-Token": token})
            if response.status_code == 401:  # expired or revoked early: once more
                token = await self._bearer(stale=token)
                response = await self._send("GET", path, params=params,
                                            headers={"X-Auth-Token": token})
            response.raise_for_status()
            return response.content

    async def recommendations(self, tenant: str, page: int) -> bytes:
        return await self._get(list_path(tenant), {"page": str(page),
                                                   "limit": str(self.page_size)})

    async def recommendation(self, tenant: str, rec_id: str, page: int) -> bytes:
        return await self._get(detail_path(tenant, rec_id), {"page": str(page),
                                                             "limit": str(self.page_size)})

    async def aclose(self) -> None:
        await self._http.aclose()
