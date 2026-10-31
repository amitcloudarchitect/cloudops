"""A fake Azure management plane serving recorded fixtures through httpx.

    fake = FakeAzure.from_fixtures()
    client = AzureClient(FIXTURE_HOST, transport=fake.transport())

It answers the two APIs as the client uses them: ARM's subscription listing,
``subs_page`` per page with an absolute ``nextLink``; and Resource Graph, rows
of the requested subscriptions only, in id order, ``$top`` per page with an
opaque ``$skipToken``. Resource Graph responses carry the user quota headers;
the quota refills once spent (the client is expected to have waited). Tests
can add or remove resources, force 429s with ``Retry-After``, or failures.
"""

from __future__ import annotations

import base64
import copy
import json
from importlib import resources
from typing import Any, Final

import httpx

from cloudops_connectors.azure.client import GRAPH_PATH, SUBSCRIPTIONS_PATH

FIXTURE_HOST: Final = "https://management.azure.fixture.example"


def load_fixture(name: str) -> bytes:
    return resources.files("cloudops_connectors.azure").joinpath(f"fixtures/{name}").read_bytes()


def _token(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def _offset(token: str | None) -> int:
    if not token:
        return 0
    return int(json.loads(base64.urlsafe_b64decode(token.encode()))["offset"])


def _id(row: Any) -> str:
    return str(row.get("id") or "") if isinstance(row, dict) else ""


class FakeAzure:
    def __init__(self, subscriptions: bytes, resources_: bytes, *, quota: int = 15,
                 subs_page: int = 100) -> None:
        self.subscriptions: list[dict[str, Any]] = json.loads(subscriptions)["value"]
        self.rows: list[Any] = json.loads(resources_)["data"]
        self.quota = quota
        self.remaining = quota
        self.subs_page = subs_page
        self.throttle_next = 0
        self.fail_next = 0
        self.requests: list[httpx.Request] = []

    @classmethod
    def from_fixtures(cls, **kwargs: int) -> FakeAzure:
        return cls(load_fixture("subscriptions.json"), load_fixture("resources.json"), **kwargs)

    def remove(self, native_id: str) -> dict[str, Any]:
        """The resource is gone from Azure."""
        for i, row in enumerate(self.rows):
            if _id(row) == native_id:
                removed: dict[str, Any] = self.rows.pop(i)
                return removed
        raise KeyError(native_id)

    def add(self, row: dict[str, Any]) -> None:
        self.rows.append(copy.deepcopy(row))

    def edit(self, native_id: str, **fields: Any) -> None:
        for row in self.rows:
            if _id(row) == native_id:
                row.update(fields)
                return
        raise KeyError(native_id)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _quota(self) -> dict[str, str]:
        return {
            "x-ms-user-quota-remaining": str(max(self.remaining, 0)),
            "x-ms-user-quota-resets-after": "00:00:05",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(429, headers={**self._quota(), "Retry-After": "7"},
                                  json={"error": {"code": "RateLimiting"}})
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(503, content=b"unavailable")
        path = request.url.path
        if path == SUBSCRIPTIONS_PATH and request.method == "GET":
            return httpx.Response(200, json=self._subscriptions(request.url.params))
        if path == GRAPH_PATH and request.method == "POST":
            if self.remaining <= 0:
                self.remaining = self.quota  # the window has passed
            self.remaining -= 1
            return httpx.Response(200, headers=self._quota(),
                                  content=self._graph(json.loads(request.content)))
        return httpx.Response(404, json={"error": {"code": "NotFound"}})

    def _subscriptions(self, params: httpx.QueryParams) -> dict[str, Any]:
        start = _offset(params.get("$skiptoken"))
        page = self.subscriptions[start:start + self.subs_page]
        out: dict[str, Any] = {"value": page,
                               "count": {"type": "Total", "value": len(self.subscriptions)}}
        if start + self.subs_page < len(self.subscriptions):
            out["nextLink"] = (f"{FIXTURE_HOST}{SUBSCRIPTIONS_PATH}?api-version=2022-12-01"
                               f"&$skiptoken={_token(start + self.subs_page)}")
        return out

    def _graph(self, body: dict[str, Any]) -> bytes:
        wanted = {str(s).lower() for s in body.get("subscriptions", [])}
        options = body.get("options") or {}
        top = int(options.get("$top", 1000))
        start = _offset(options.get("$skipToken"))
        matching = sorted(
            (r for r in self.rows if isinstance(r, dict)
             and str(r.get("subscriptionId") or "").lower() in wanted),
            key=_id,
        )
        page = matching[start:start + top]
        out: dict[str, Any] = {"totalRecords": len(matching), "count": len(page),
                               "data": page, "facets": [], "resultTruncated": "false"}
        if start + top < len(matching):
            out["$skipToken"] = _token(start + top)
        return json.dumps(out).encode()
