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
- Daily cost (entity ``cost_daily``, ADR 0023): the same query endpoint,
  ``ActualCost`` for a fixed custom period, daily, grouped by ServiceName and
  ChargeType (``daily_query``). A ``nextLink`` is followed only on this
  client's own host, with the same body.
- Billing invoices (entity ``invoice``): ``GET /providers/Microsoft.Billing/
  billingAccounts/{acct}/billingSubscriptions/{id}/invoices`` for a window of
  invoice periods, ``nextLink`` followed only on this client's own host.
- Role assignments (a customer credential's check, ADR 0027): ``GET /subscriptions/
  {id}/providers/Microsoft.Authorization/roleAssignments`` filtered to what is
  ``assignedTo`` the credential's own principal (its groups included), and a role
  definition by its id, both under one subscription.
- Cloud Services audit (ADR 0038): the same Resource Graph endpoint with one of
  ``audit.QUERIES`` (``graph``), and Defender for Cloud's assessments,
  ``GET /subscriptions/{id}/providers/Microsoft.Security/assessments`` with their
  metadata, ``nextLink`` followed only on this client's own host and subscription.
- Anything else non-2xx raises; Temporal's retry policy owns those retries.

**Target scope** (ADR 0003, guardrail 5). A client built with ``allowed`` (a
customer credential's subscriptions) refuses, before any request, a call naming
a subscription outside them: ``TargetOutOfScopeError``. The listing is the one
call that names none; what it shows beyond ``allowed`` is quarantined by the
caller and never read.
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
from cloudops_connectors.core.identity import TargetOutOfScopeError
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
AUTHORIZATION_API: Final = "2022-04-01"
SECURITY_API: Final = "2021-06-01"
# Fixed: the connector asks for exactly this. The total cost of the month so
# far, as billed (ActualCost), with its currency.
COST_QUERY: Final[dict[str, Any]] = {
    "type": "ActualCost",
    "timeframe": "MonthToDate",
    "dataset": {"granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}},
}
# Fixed apart from its period: daily cost by service and charge type, as billed.
# Two groupings is the query API's limit; the subscription is the request's scope.
DAILY_GROUPING: Final = ("ServiceName", "ChargeType")


def daily_query(start: date, end: date) -> dict[str, Any]:
    """``ActualCost`` from ``start`` to ``end`` inclusive, by day, service and charge type."""
    if end < start:
        raise ValueError("a period ends after it starts")
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start.isoformat()}T00:00:00Z",
                       "to": f"{end.isoformat()}T23:59:59Z"},
        "dataset": {"granularity": "Daily",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                    "grouping": [{"type": "Dimension", "name": n} for n in DAILY_GROUPING]},
    }


_SUBSCRIPTION: Final = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_BILLING_ACCOUNT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.\-]{0,199}")
_COST_RETRY: Final = tuple(
    f"x-ms-ratelimit-microsoft.costmanagement-{k}-retry-after"
    for k in ("qpu", "entity", "tenant", "client"))


_ROLE_DEFINITION: Final = re.compile(
    r"/subscriptions/(?P<sub>[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12})"
    r"/providers/Microsoft\.Authorization/roleDefinitions/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_GUID: Final = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


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
        allowed: frozenset[str] | None = None,
    ) -> None:
        """``allowed``: the only subscriptions this client may name (a customer
        credential's). ``None``: any (the platform's own identity, fixtures)."""
        if allowed is not None and (not allowed or not all(
                _SUBSCRIPTION.fullmatch(a) for a in allowed)):
            raise ValueError("allowed names at least one subscription id")
        self.allowed = allowed
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

    def _target(self, subscription: str) -> str:
        """The second lock behind the sources' own filtering: refused, never sent."""
        _subscription(subscription)
        if self.allowed is not None and subscription not in self.allowed:
            raise TargetOutOfScopeError("subscription outside the credential's scope")
        return subscription

    async def resources(self, subscriptions: list[str], skip_token: str | None) -> bytes:
        return await self.graph(subscriptions, QUERY, skip_token)

    async def graph(self, subscriptions: list[str], query: str, skip_token: str | None) -> bytes:
        """A fixed Resource Graph query (``QUERY`` or one of ``audit.QUERIES``)."""
        for sub in subscriptions:
            self._target(sub)
        options: dict[str, Any] = {"$top": self.page_size, "resultFormat": "objectArray"}
        if skip_token is not None:
            options["$skipToken"] = skip_token
        return await self._send(
            "POST", GRAPH_PATH, {"api-version": GRAPH_API},
            {"subscriptions": subscriptions, "query": query, "options": options},
        )

    async def assessments(self, subscription: str, next_link: str | None = None) -> bytes:
        """Defender for Cloud's assessments of one subscription, with their metadata."""
        self._target(subscription)
        if next_link is not None:
            link = httpx.URL(next_link)
            if f"{link.scheme}://{link.netloc.decode()}" != self.base_url:
                raise ValueError("nextLink points away from the management endpoint")
            if not link.path.lower().startswith(f"/subscriptions/{subscription}/"):
                raise TargetOutOfScopeError("nextLink names another subscription")
            return await self._send("GET", str(link), None, None)
        return await self._send(
            "GET", f"/subscriptions/{subscription}/providers/Microsoft.Security/assessments",
            {"api-version": SECURITY_API, "$expand": "metadata"}, None)

    async def cost_query(self, subscription: str) -> bytes:
        return await self._send("POST", cost_query_path(self._target(subscription)),
                                {"api-version": COST_QUERY_API}, COST_QUERY)

    async def cost_daily(self, subscription: str, start: date, end: date,
                         next_link: str | None = None) -> bytes:
        body = daily_query(start, end)
        self._target(subscription)
        if next_link is not None:
            link = httpx.URL(next_link)
            if f"{link.scheme}://{link.netloc.decode()}" != self.base_url:
                raise ValueError("nextLink points away from the management endpoint")
            if not link.path.startswith(f"/subscriptions/{subscription}/"):
                raise TargetOutOfScopeError("nextLink names another subscription")
            return await self._send("POST", str(link), None, body)
        return await self._send("POST", cost_query_path(subscription),
                                {"api-version": COST_QUERY_API}, body)

    async def invoices(self, billing_account: str, subscription: str, start: date,
                       end: date, next_link: str | None = None) -> bytes:
        self._target(subscription)
        if next_link is not None:
            return await self._own(next_link)
        if not _BILLING_ACCOUNT.fullmatch(billing_account):
            raise ValueError("not a billing account name")
        return await self._send(
            "GET", invoices_path(billing_account, subscription),
            {"api-version": BILLING_API, "periodStartDate": start.isoformat(),
             "periodEndDate": end.isoformat()}, None)

    async def role_assignments(self, subscription: str, principal_id: str) -> bytes:
        """Every assignment the principal holds, directly or through a group, at,
        above or below the subscription."""
        if not _GUID.fullmatch(principal_id):
            raise ValueError("not a principal object id")
        return await self._send(
            "GET", f"/subscriptions/{self._target(subscription)}/providers/"
                   "Microsoft.Authorization/roleAssignments",
            {"api-version": AUTHORIZATION_API, "$filter": f"assignedTo('{principal_id}')"},
            None)

    async def role_definition(self, definition_id: str) -> bytes:
        """A role definition, by the id an assignment names, under its subscription."""
        found = _ROLE_DEFINITION.fullmatch(definition_id)
        if found is None:
            raise ValueError("not a subscription-scoped role definition id")
        self._target(found["sub"])
        return await self._send("GET", definition_id, {"api-version": AUTHORIZATION_API}, None)

    async def _own(self, link_text: str) -> bytes:
        link = httpx.URL(link_text)
        if f"{link.scheme}://{link.netloc.decode()}" != self.base_url:
            raise ValueError("nextLink points away from the management endpoint")
        return await self._send("GET", str(link), None, None)

    async def aclose(self) -> None:
        await self._http.aclose()
