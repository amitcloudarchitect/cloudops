"""What the Graph fakes share: delta sync and paging, as Graph serves them.

``DeltaLog`` keeps a resource's items in change order. A delta request with no
token returns every live item; one with ``$deltatoken=N`` returns what changed
after change N, deleted items as tombstones. Pages carry ``@odata.nextLink``
(``$skiptoken``) until the last, which carries ``@odata.deltaLink``. A page
range is fixed when the first page is served (the token names its upper
bound), so a change made while a client is paging appears in the next delta,
not half in this one: that is how Graph behaves too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


def respond(status: int, payload: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers={"Content-Type": "application/json", **(headers or {})},
                          content=json.dumps(payload).encode())


def error(status: int, code: str, message: str = "") -> httpx.Response:
    return respond(status, {"error": {"code": code, "message": message or code}})


@dataclass
class _Entry:
    seq: int
    item: dict[str, Any]
    live: bool


@dataclass
class DeltaLog:
    entries: dict[str, _Entry] = field(default_factory=dict)
    seq: int = 0

    def put(self, item: dict[str, Any], key: str | None = None) -> None:
        """Add or change an item. ``key`` names a record without an id."""
        self.seq += 1
        k = key or str(item["id"])
        self.entries[k] = _Entry(self.seq, item, True)

    def remove(self, key: str, tombstone: dict[str, Any]) -> None:
        self.seq += 1
        self.entries[key] = _Entry(self.seq, tombstone, False)

    def live(self, key: str) -> dict[str, Any] | None:
        e = self.entries.get(key)
        return e.item if e is not None and e.live else None

    def changes(self, since: int | None, upto: int) -> list[dict[str, Any]]:
        ordered = sorted(self.entries.values(), key=lambda e: e.seq)
        if since is None:
            return [e.item for e in ordered if e.live and e.seq <= upto]
        return [e.item for e in ordered if since < e.seq <= upto]

    def page(self, base: str, path: str, params: httpx.QueryParams,
             page_size: int) -> dict[str, Any]:
        """One delta page for a request's query parameters."""
        skip = params.get("$skiptoken")
        if skip is not None:
            s, upto, offset = skip.split(".")
            since = None if s == "x" else int(s)
            start = int(offset)
        else:
            token = params.get("$deltatoken")
            since = None if token is None else int(token)
            upto, start = self.seq, 0
        items = self.changes(since, int(upto))
        end = start + page_size
        out: dict[str, Any] = {"@odata.context": f"{base}/v1.0/$metadata",
                               "value": items[start:end]}
        if end < len(items):
            out["@odata.nextLink"] = (f"{base}{path}?$skiptoken="
                                      f"{'x' if since is None else since}.{upto}.{end}")
        else:
            out["@odata.deltaLink"] = f"{base}{path}?$deltatoken={upto}"
        return out


def snapshot_page(base: str, path: str, items: list[dict[str, Any]], params: httpx.QueryParams,
                  page_size: int, query: str = "") -> dict[str, Any]:
    """One page of a plain collection, paged by ``$skiptoken``."""
    start = int(params.get("$skiptoken", "0"))
    end = start + page_size
    out: dict[str, Any] = {"@odata.context": f"{base}/v1.0/$metadata",
                           "value": items[start:end]}
    if end < len(items):
        out["@odata.nextLink"] = f"{base}{path}?{query}$skiptoken={end}"
    return out


@dataclass
class Throttle:
    """``next`` requests answer 429 with ``Retry-After: seconds``."""

    next: int = 0
    seconds: str = "7"

    def take(self) -> httpx.Response | None:
        if self.next <= 0:
            return None
        self.next -= 1
        return respond(429, {"error": {"code": "TooManyRequests", "message": "throttled"}},
                       {"Retry-After": self.seconds})
