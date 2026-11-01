"""ServiceNow Table API client: one page per call, rate limits honoured.

    client = ServiceNowClient(base_url, auth=..., transport=...)
    body = await client.fetch("incident", since, offset)

Returns the response body as bytes, untouched: the caller stores it in bronze
before anything parses it. The same code runs against a real instance and
against ``FakeServiceNow`` (fixtures); only the httpx transport differs.

- Query: ``sys_updated_on >= since``, ordered by sys_updated_on then sys_id so
  offsets are stable. ``since`` is the watermark minus the overlap.
- 429: waits Retry-After and tries again, up to ``max_throttle_retries``.
- Quota below 20%: returns the page, then waits for the window to reset
  before the next call can go out.
- 400 "Invalid table" raises ``SourceTableAbsentError``: the table does not exist
  on the instance (a plugin not active, ADR 0038). Not retryable, not a failure.
- Anything else non-2xx raises ``httpx.HTTPStatusError``; Temporal's activity
  retry policy owns those retries (LLD: exponential backoff, five attempts).

The integration user's timezone must be GMT: encoded-query date-times are read
in the user's timezone, and ``mapping.py`` reads raw values as UTC.
``check_integration_user`` enforces it before a real instance is read; an unset
timezone falls back to the instance default, so it is refused too.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final

import httpx

from cloudops_connectors.core.ratelimit import pause_for
from cloudops_connectors.servicenow.mapping import TIME_FORMAT

PAGE_SIZE: Final = 200  # LLD: 200 records per page


# What ServiceNow stores for GMT/UTC in sys_user.time_zone.
UTC_ZONES: Final = frozenset({"GMT", "UTC", "Etc/UTC", "Etc/GMT", "Etc/Universal"})


class ThrottledError(Exception):
    """Still throttled after the allowed waits. Retryable by Temporal."""


class SourceTableAbsentError(Exception):
    """The instance has no such table (its plugin is not active). Carries the table
    name only. Not retryable: the run ends ``source_absent``."""


def _table_absent(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    try:
        message = str(response.json().get("error", {}).get("message", ""))
    except ValueError:
        return False
    return message.startswith("Invalid table")


class IntegrationUserError(Exception):
    """The integration user would make incremental reads wrong. Not retryable:
    a person has to fix the account."""


class ServiceNowClient:
    def __init__(
        self,
        base_url: str,
        *,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        page_size: int = PAGE_SIZE,
        max_throttle_retries: int = 5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self._max_throttle_retries = max_throttle_retries
        self._sleep = sleep
        self._clock = clock
        self._pending_pause = 0.0
        self._http = httpx.AsyncClient(
            base_url=self.base_url, auth=auth, transport=transport, timeout=60.0,
            headers={"Accept": "application/json"},
        )

    @staticmethod
    def query(since: datetime) -> str:
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware (UTC)")
        return f"sys_updated_on>={since:{TIME_FORMAT}}^ORDERBYsys_updated_on^ORDERBYsys_id"

    def params(self, since: datetime, offset: int) -> dict[str, str]:
        return {
            "sysparm_query": self.query(since),
            "sysparm_limit": str(self.page_size),
            "sysparm_offset": str(offset),
            "sysparm_display_value": "all",
            "sysparm_exclude_reference_link": "true",
        }

    async def fetch(self, table: str, since: datetime, offset: int) -> bytes:
        if self._pending_pause > 0:
            await self._sleep(self._pending_pause)
            self._pending_pause = 0.0
        params = self.params(since, offset)
        for attempt in range(self._max_throttle_retries + 1):
            response = await self._http.get(f"/api/now/table/{table}", params=params)
            pause = pause_for(response.status_code, response.headers, self._clock())
            if response.status_code == 429:
                if attempt == self._max_throttle_retries or pause is None:
                    raise ThrottledError(f"{table}: throttled after {attempt + 1} attempts")
                await self._sleep(pause.seconds)
                continue
            if _table_absent(response):
                raise SourceTableAbsentError(table)
            response.raise_for_status()
            if pause is not None:
                # Wait before the next request, not before handing this page back.
                self._pending_pause = pause.seconds
            return response.content
        raise ThrottledError(table)  # pragma: no cover - loop always returns or raises

    async def check_integration_user(self) -> str:
        """The account the credentials act as, refusing one whose timezone is not
        GMT. Watermarks are UTC; a user in another zone shifts every query."""
        response = await self._http.get("/api/now/table/sys_user", params={
            "sysparm_query": "user_name=javascript:gs.getUserName()",
            "sysparm_fields": "user_name,time_zone",
            "sysparm_limit": "2",
        })
        response.raise_for_status()
        users = response.json().get("result", [])
        if len(users) != 1:
            raise IntegrationUserError("could not identify the integration user")
        name, zone = str(users[0].get("user_name", "")), str(users[0].get("time_zone", ""))
        if zone not in UTC_ZONES:
            raise IntegrationUserError(
                f"integration user {name!r} has time zone {zone or 'unset (instance default)'}; "
                "set it to GMT, or every incremental query is shifted"
            )
        return name

    async def aclose(self) -> None:
        await self._http.aclose()
