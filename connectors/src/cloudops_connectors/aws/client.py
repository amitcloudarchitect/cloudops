"""AWS clients: Config aggregator advanced queries and S3 for the CUR.

    config = ConfigClient("eu-west-1", "org-aggregator", auth=..., transport=...)
    body = await config.select(since, next_token)
    s3 = S3Client("cur-bucket", "eu-west-1", auth=..., transport=...)
    body = await s3.list(prefix, continuation_token)
    obj = await s3.get(key)

Bodies come back untouched; the caller stores them in bronze before anything
parses them. The same code runs against AWS and the fakes in ``fake.py``.

- **No credential path.** Requests are signed by the ``httpx.Auth`` passed in;
  there is no SigV4 signer here and nothing reads a key (CLAUDE.md rule 2, ADR
  0003). With no auth the client talks only to what its transport serves.
- **Throttling** (LLD: exponential backoff on throttling): Config answers 400
  with ``__type`` ``ThrottlingException``, S3 503 ``SlowDown``. The client waits
  ``base * 2**attempt`` seconds, capped, with jitter (half fixed, half random:
  ``sleep`` and ``rand`` are injectable), and after the allowed retries raises
  ``ThrottledError`` for Temporal's own backoff to take over.
- Anything else non-2xx raises, except a missing S3 object, which is
  ``NoSuchKeyError`` carrying S3's response body: a CUR file not delivered yet.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

import httpx

from cloudops_connectors.aws.mapping import expression

CONFIG_TARGET: Final = "StarlingDoveService.SelectAggregateResourceConfig"
CONFIG_PAGE: Final = 100  # the API's maximum Limit
THROTTLE_TYPES: Final = frozenset({"ThrottlingException", "TooManyRequestsException"})


class ThrottledError(Exception):
    """Still throttled after the allowed waits. Retryable by Temporal."""


class NoSuchKeyError(Exception):
    """The object is not in the bucket (yet). Carries S3's response body."""

    def __init__(self, key: str, body: bytes) -> None:
        super().__init__(key)
        self.key = key
        self.body = body


def _config_throttled(response: httpx.Response) -> bool:
    if response.status_code not in (400, 429):
        return False
    try:
        kind = str(json.loads(response.content).get("__type", ""))
    except (ValueError, AttributeError):
        return response.status_code == 429
    return kind.rsplit("#", 1)[-1] in THROTTLE_TYPES or response.status_code == 429


def _s3_throttled(response: httpx.Response) -> bool:
    if response.status_code in (429, 503):
        return True
    return response.status_code >= 400 and b"<Code>SlowDown</Code>" in response.content[:512]


class _Backoff:
    def __init__(
        self,
        max_retries: int,
        base: float,
        cap: float,
        sleep: Callable[[float], Awaitable[None]],
        rand: Callable[[], float],
    ) -> None:
        self.max_retries = max_retries
        self.base = base
        self.cap = cap
        self._sleep = sleep
        self._rand = rand

    def delay(self, attempt: int) -> float:
        ceiling = float(min(self.cap, self.base * 2.0 ** attempt))
        return ceiling / 2 + ceiling / 2 * self._rand()

    async def send(
        self,
        what: str,
        request: Callable[[], Awaitable[httpx.Response]],
        throttled: Callable[[httpx.Response], bool],
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            response = await request()
            if not throttled(response):
                return response
            if attempt == self.max_retries:
                break
            await self._sleep(self.delay(attempt))
        raise ThrottledError(f"{what}: throttled after {self.max_retries + 1} attempts")


class ConfigClient:
    def __init__(
        self,
        region: str,
        aggregator: str,
        *,
        base_url: str | None = None,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        limit: int = CONFIG_PAGE,
        max_throttle_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.region = region
        self.aggregator = aggregator
        self.base_url = (base_url or f"https://config.{region}.amazonaws.com").rstrip("/")
        self.limit = limit
        self.backoff = _Backoff(max_throttle_retries, backoff_base, backoff_cap, sleep, rand)
        self._http = httpx.AsyncClient(base_url=self.base_url, auth=auth, transport=transport,
                                       timeout=120.0)

    async def select(self, since: datetime, next_token: str | None) -> bytes:
        payload: dict[str, object] = {
            "Expression": expression(since),
            "ConfigurationAggregatorName": self.aggregator,
            "Limit": self.limit,
        }
        if next_token is not None:
            payload["NextToken"] = next_token
        content = json.dumps(payload, sort_keys=True).encode()
        headers = {"X-Amz-Target": CONFIG_TARGET,
                   "Content-Type": "application/x-amz-json-1.1"}

        async def request() -> httpx.Response:
            return await self._http.post("/", content=content, headers=headers)

        response = await self.backoff.send("config select", request, _config_throttled)
        response.raise_for_status()
        return response.content

    async def aclose(self) -> None:
        await self._http.aclose()


@dataclass(frozen=True)
class Object:
    key: str
    body: bytes
    last_modified: datetime | None


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC)
    except (TypeError, ValueError):
        return None


class S3Client:
    def __init__(
        self,
        bucket: str,
        region: str,
        *,
        base_url: str | None = None,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_keys: int = 1000,
        max_throttle_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.bucket = bucket
        self.base_url = (base_url or f"https://{bucket}.s3.{region}.amazonaws.com").rstrip("/")
        self.max_keys = max_keys
        self.backoff = _Backoff(max_throttle_retries, backoff_base, backoff_cap, sleep, rand)
        self._http = httpx.AsyncClient(base_url=self.base_url, auth=auth, transport=transport,
                                       timeout=300.0)

    async def list(self, prefix: str, token: str | None) -> bytes:
        """One ListObjectsV2 page under ``prefix``."""
        params = {"list-type": "2", "prefix": prefix, "max-keys": str(self.max_keys)}
        if token is not None:
            params["continuation-token"] = token

        async def request() -> httpx.Response:
            return await self._http.get("/", params=params)

        response = await self.backoff.send("s3 list", request, _s3_throttled)
        response.raise_for_status()
        return response.content

    async def get(self, key: str) -> Object:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError("bad object key")

        async def request() -> httpx.Response:
            return await self._http.get(f"/{key}")

        response = await self.backoff.send("s3 get", request, _s3_throttled)
        if response.status_code == 404:
            raise NoSuchKeyError(key, response.content)
        response.raise_for_status()
        return Object(key, response.content, _http_date(response.headers.get("last-modified")))

    async def aclose(self) -> None:
        await self._http.aclose()
