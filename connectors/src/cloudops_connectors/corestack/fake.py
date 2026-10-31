"""A fake CoreStack account serving recorded fixtures through httpx.

    fake = FakeCoreStack.from_fixtures()
    client = CoreStackClient(FIXTURE_HOST, transport=fake.transport())

It answers ``GET /v1/findings`` as the client uses it (the assumed shape in
``mapping.py``): findings with ``updated_at`` at or after ``updated_since``,
ordered by ``updated_at`` then ``id``, ``limit`` from ``offset``, with the
total; plus quota headers and 429 with ``Retry-After``. Tests can edit a
finding (``touch``) or force throttling and failures.

The fixture file is served byte-for-byte per finding: numbers keep their
literal text, as a real API's JSON would.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from typing import Any, Final

import httpx

from cloudops_connectors.corestack.client import FINDINGS_PATH

FIXTURE_HOST: Final = "https://api.corestack.fixture.example"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def load_fixture(name: str) -> bytes:
    return (resources.files("cloudops_connectors.corestack")
            .joinpath(f"fixtures/{name}").read_bytes())


def _time(text: Any) -> datetime:
    if not isinstance(text, str):
        return _EPOCH
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _dumps(value: Any) -> str:
    """JSON with Decimals written as their own text, never through float."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)}: {_dumps(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_dumps(v) for v in value) + "]"
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value)


class FakeCoreStack:
    def __init__(self, findings: bytes, *, quota: int = 1000) -> None:
        doc = json.loads(findings, parse_float=Decimal)
        self.findings: list[dict[str, Any]] = list(doc["data"])
        self.quota = quota
        self.remaining = quota
        self.throttle_next = 0
        self.retry_after = "7"
        self.fail_next = 0
        self.requests: list[httpx.Request] = []

    @classmethod
    def from_fixtures(cls, **kwargs: int) -> FakeCoreStack:
        return cls(load_fixture("findings.json"), **kwargs)

    def touch(self, finding_id: str, updated_at: str, **fields: Any) -> None:
        """Edit a finding as CoreStack would: new values, a later update time."""
        for f in self.findings:
            if f.get("id") == finding_id:
                f.update(fields, updated_at=updated_at)
                return
        raise KeyError(finding_id)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.quota),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "Content-Type": "application/json",
        }

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(429, headers={**self._headers(),
                                                "Retry-After": self.retry_after},
                                  content=b'{"error": "rate limit exceeded"}')
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(503, content=b"unavailable")
        if request.url.path != FINDINGS_PATH:
            return httpx.Response(404, content=b'{"error": "not found"}')
        self.remaining -= 1
        params = request.url.params
        since = _time(params.get("updated_since"))
        offset = int(params.get("offset", "0"))
        limit = int(params.get("limit", "100"))
        matching = sorted((f for f in self.findings if _time(f.get("updated_at")) >= since),
                          key=lambda f: (_time(f.get("updated_at")), str(f.get("id", ""))))
        body = {"data": matching[offset:offset + limit], "total": len(matching),
                "offset": offset, "limit": limit}
        return httpx.Response(200, headers=self._headers(), content=_dumps(body).encode())
