"""A fake ServiceNow instance serving recorded fixtures through httpx.

    fake = FakeServiceNow.from_fixtures()
    client = ServiceNowClient("https://fixture.service-now.example", transport=fake.transport())

It answers the Table API exactly as the client uses it: the encoded query's
``sys_updated_on>=`` filter and ordering, limit and offset,
``display_value=all`` records, and the rate-limit headers
(``X-RateLimit-Limit``/``-Remaining``/``-Reset``, 429 with ``Retry-After``). So
the client, the pipeline and the workflow run unchanged against fixtures and a
real instance.

Tests can edit records (``touch``), shrink the quota, or force throttling.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from importlib import resources
from typing import Any, Final

import httpx

from cloudops_connectors.servicenow.mapping import ALL_ENTITIES

FIXTURE_HOST: Final = "https://fixture.service-now.example"
_SINCE = re.compile(r"sys_updated_on>=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _updated(record: Mapping[str, Any]) -> str:
    return str(record["sys_updated_on"]["value"])


def _sys_id(record: Mapping[str, Any]) -> str:
    return str(record["sys_id"]["value"])


def load_fixture(entity: str) -> list[dict[str, Any]]:
    text = resources.files("cloudops_connectors.servicenow").joinpath(
        f"fixtures/{entity}.json"
    ).read_text("utf-8")
    records: list[dict[str, Any]] = json.loads(text)["result"]
    return records


class FakeServiceNow:
    def __init__(
        self,
        tables: Mapping[str, list[dict[str, Any]]],
        *,
        quota: int = 10_000,
        reset_in: float = 60.0,
        clock_start: float = 1_800_000_000.0,
    ) -> None:
        self.tables = {t: deepcopy(r) for t, r in tables.items()}
        self.quota = quota
        self.remaining = quota
        self.reset_in = reset_in
        self.now = clock_start
        self.throttle_next = 0
        self.fail_next = 0
        self.requests: list[httpx.Request] = []

    @classmethod
    def from_fixtures(cls, **kwargs: Any) -> FakeServiceNow:
        return cls({e: load_fixture(e) for e in ALL_ENTITIES}, **kwargs)

    def touch(self, entity: str, number: str, updated: str, **fields: str) -> None:
        """Edit a record as ServiceNow would: new values and a later sys_updated_on."""
        for record in self.tables[entity]:
            if record["number"]["value"] == number:
                for name, value in fields.items():
                    record[name] = {"value": value, "display_value": value}
                record["sys_updated_on"] = {"value": updated, "display_value": updated}
                return
        raise KeyError(number)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.quota),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(int(self.now + self.reset_in)),
        }

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(429, headers={"Retry-After": "7", **self._headers()})
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        match = re.fullmatch(r"/api/now/table/(\w+)", request.url.path)
        if match is None:
            return httpx.Response(404, json={"error": {"message": "Not found"}})
        if match.group(1) not in self.tables:
            # As a real instance answers for a table that does not exist (dev407774,
            # pm_project_task with PPM inactive, 2026-11-01).
            return httpx.Response(400, json={
                "error": {"message": f"Invalid table {match.group(1)}", "detail": None},
                "status": "failure"})
        params = request.url.params
        since = _SINCE.search(params.get("sysparm_query", ""))
        rows = [
            r for r in self.tables[match.group(1)]
            if since is None or _updated(r) >= since.group(1)
        ]
        rows.sort(key=lambda r: (_updated(r), _sys_id(r)))
        offset = int(params.get("sysparm_offset", "0"))
        limit = int(params.get("sysparm_limit", "10000"))
        page = rows[offset:offset + limit]
        self.remaining -= 1
        headers = {**self._headers(), "X-Total-Count": str(len(rows))}
        return httpx.Response(200, json={"result": page}, headers=headers)
