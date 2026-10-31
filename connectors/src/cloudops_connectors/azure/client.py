"""Azure management client: the delegated subscriptions, and Resource Graph pages.

    client = AzureClient(base_url, auth=..., transport=...)
    body = await client.subscriptions(None)              # or the listing's nextLink
    body = await client.resources(["<sub>", ...], skip_token)

Bodies come back untouched; the caller stores them in bronze before anything
parses them. The same code runs against ARM and ``FakeAzure``.

- Subscriptions: ARM ``GET /subscriptions`` (Lighthouse-delegated ones
  included), following ``nextLink`` only on this client's own host: a link
  elsewhere would carry the token there, so it is refused.
- Resources: Resource Graph, ``QUERY`` over a batch of subscriptions, ``$top``
  rows per page, the next page by ``$skipToken``.
- Limits (LLD: honour ``x-ms-user-quota-remaining``; concurrency 2, 1000 rows
  per page): when the quota reaches zero the client waits
  ``x-ms-user-quota-resets-after`` (hh:mm:ss) before its next call instead of
  spending a refusal; a 429 waits ``Retry-After`` (else the quota reset) and
  tries again. At most two requests are in flight per client.
- Cost Management query (entity ``cost_total``): ``POST /subscriptions/{id}/
  providers/Microsoft.CostManagement/query``, ``ActualCost`` month to date,
  the sum of cost with its currency (``COST_QUERY``). A 429 there waits the
  ``x-ms-ratelimit-microsoft.costmanagement-*-retry-after`` headers too.
- Billing invoices (entity ``invoice``): ``GET /providers/Microsoft.Billing/
  billingAccounts/{acct}/billingSubscriptions/{id}/invoices`` for a window of
  invoice periods, ``nextLink`` followed only on this client's own host.
- Anything else non-2xx raises; Temporal's retry policy owns those retries.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from typing import Any, Final
from urllib.parse import quote

import httpx

from cloudops_connectors.azure.mapping import QUERY
from cloudops_connectors.core.ratelimit import MAX_WAIT_SECONDS

ARM: Final = "https://management.azure.com"
PAGE_SIZE: Final = 1000  # rows per Resource Graph page (LLD)
CONCURRENCY: Final = 2
SUBSCRIPTIONS_PATH: Final = "/subscriptions"
SUBSCRIPTIONS_API: Final = "2022-12-01"
GRAPH_PATH: Final = "/providers/Microsoft.ResourceGraph/resources"
GRAPH_API: Final = "2022-10-01"
DEFAULT_WAIT: Final = 5.0  # Resource Graph's quota window is five seconds
COST_QUERY_API: Final = "2023-11-01"
BILLING_API: Final = "2024-04-01"
# Fixed: the connector asks for exactly this. The total cost of the month so
# far, as billed (ActualCost), with its currency.
COST_QUERY: Final[dict[str, Any]] = {
    "type": "ActualCost",
    "timeframe": "MonthToDate",
    "dataset": {"granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}},
}
_SUBSCRIPTION: Final = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_BILLING_ACCOUNT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.\-]{0,199}")
_COST_RETRY: Final = tuple(
    f"x-ms-ratelimit-microsoft.costmanagement-{k}-retry-after"
    for k in ("qpu", "entity", "tenant", "client"))


def cost_query_path(subscription: str) -> str:
    return f"/subscriptions/{subscription}/providers/Microsoft.CostManagement/query"


def invoices_path(billing_account: str, subscription: str) -> str:
    return (f"/providers/Microsoft.Billing/billingAccounts/{quote(billing_account, safe='')}"
            f"/billingSubscriptions/{subscription}/invoices")


def _subscription(value: str) -> str:
    if not _SUBSCRIPTION.fullmatch(value):
        raise ValueError("not a subscription id")
    return value


class ThrottledError(Exception):
    """Still refused after the allowed waits. Retryable by Temporal."""


def _seconds(value: str | None) -> float | None:
    """``hh:mm:ss`` (the quota reset) or plain seconds (Retry-After)."""
    if value is None:
        return None
    try:
        parts = [float(p) for p in value.strip().split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3 or any(p < 0 for p in parts):
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def to_wait(status: int, headers: Mapping[str, str]) -> float | None:
    """Seconds to wait before the next call, or None. ``headers`` case-insensitive."""
    reset = _seconds(headers.get("x-ms-user-quota-resets-after"))
    if status == 429:
        wait = _seconds(headers.get("retry-after"))
        if wait is None:
            cost = [w for w in (_seconds(headers.get(h)) for h in _COST_RETRY) if w is not None]
            wait = max(cost) if cost else None
        wait = wait if wait is not None else reset
        return min(wait if wait is not None else DEFAULT_WAIT, MAX_WAIT_SECONDS)
    remaining = headers.get("x-ms-user-quota-remaining")
    try:
        exhausted = remaining is not None and int(remaining.strip()) <= 0
    except ValueError:
        exhausted = False
    if exhausted:
        return min(reset if reset is not None else DEFAULT_WAIT, MAX_WAIT_SECONDS)
    return None


class AzureClient:
    def __init__(
        self,
        base_url: str = ARM,
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
        self._slots = asyncio.Semaphore(CONCURRENCY)
        self._http = httpx.AsyncClient(
            base_url=self.base_url, auth=auth, transport=transport, timeout=120.0,
            headers={"x-ms-app": "cloudops-connector"},
        )

    async def _send(self, method: str, url: str, params: dict[str, str] | None,
                    body: dict[str, Any] | None) -> bytes:
        async with self._slots:
            if self._pending_pause > 0:
                pause, self._pending_pause = self._pending_pause, 0.0
                await self._sleep(pause)
            for attempt in range(self._max_throttle_retries + 1):
                response = await self._http.request(method, url, params=params, json=body)
                wait = to_wait(response.status_code, response.headers)
                if response.status_code == 429:
                    if attempt == self._max_throttle_retries or wait is None:
                        raise ThrottledError(f"{method} {httpx.URL(url).path}: refused after "
                                             f"{attempt + 1} attempts")
                    await self._sleep(wait)
                    continue
                response.raise_for_status()
                if wait is not None:
                    self._pending_pause = wait
                return response.content
        raise ThrottledError(url)  # pragma: no cover - the loop returns or raises

    async def subscriptions(self, next_link: str | None) -> bytes:
        if next_link is None:
            return await self._send("GET", SUBSCRIPTIONS_PATH,
                                    {"api-version": SUBSCRIPTIONS_API}, None)
        link = httpx.URL(next_link)
        if f"{link.scheme}://{link.netloc.decode()}" != self.base_url:
            raise ValueError("nextLink points away from the management endpoint")
        return await self._send("GET", str(link), None, None)

    async def resources(self, subscriptions: list[str], skip_token: str | None) -> bytes:
        options: dict[str, Any] = {"$top": self.page_size, "resultFormat": "objectArray"}
        if skip_token is not None:
            options["$skipToken"] = skip_token
        return await self._send(
            "POST", GRAPH_PATH, {"api-version": GRAPH_API},
            {"subscriptions": subscriptions, "query": QUERY, "options": options},
        )

    async def cost_query(self, subscription: str) -> bytes:
        return await self._send("POST", cost_query_path(_subscription(subscription)),
                                {"api-version": COST_QUERY_API}, COST_QUERY)

    async def invoices(self, billing_account: str, subscription: str, start: date,
                       end: date, next_link: str | None = None) -> bytes:
        if next_link is not None:
            return await self._own(next_link)
        if not _BILLING_ACCOUNT.fullmatch(billing_account):
            raise ValueError("not a billing account name")
        return await self._send(
            "GET", invoices_path(billing_account, _subscription(subscription)),
            {"api-version": BILLING_API, "periodStartDate": start.isoformat(),
             "periodEndDate": end.isoformat()}, None)

    async def _own(self, link_text: str) -> bytes:
        link = httpx.URL(link_text)
        if f"{link.scheme}://{link.netloc.decode()}" != self.base_url:
            raise ValueError("nextLink points away from the management endpoint")
        return await self._send("GET", str(link), None, None)

    async def aclose(self) -> None:
        await self._http.aclose()
