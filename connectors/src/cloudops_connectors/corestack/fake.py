"""A fake CoreStack serving recorded fixtures through httpx.

    fake = FakeCoreStack.from_fixtures()
    client = CoreStackClient(FIXTURE_HOST, lambda: FIXTURE_ACCESS_KEY, lambda: FIXTURE_SECRET,
                             transport=fake.transport())

It answers what the client asks, as CoreStack does (ADR 0028): ``POST
/v1/auth/tokens`` with the access key pair (201, the token, the tenants the user
can see), the list (``page``/``limit``, ``page_count``) and a recommendation's
detail with its resources paged the same way, each with ``X-Auth-Token``. A
missing, wrong or expired token is 401; a tenant the user cannot see is 403.
Anything else is 404, and any method but those two is 405: the fake records every
request, so tests can prove the client sent nothing else. Tests change the
account between runs (``resolve``, ``expire_tokens``) or force throttling.
"""

from __future__ import annotations

import copy
import json
import math
import re
import secrets
from importlib import resources
from typing import Any, Final

import httpx

FIXTURE_HOST: Final = "https://corestack.fixture.example"
FIXTURE_ACCESS_KEY: Final = "00000000-0000-4000-8000-00000000c5ac"
FIXTURE_SECRET: Final = "fixture-secret-key"  # noqa: S105 - the fake's own, not a credential
_LIST: Final = re.compile(r"^/recommendations/([^/]+)/service_account/list$")
_DETAIL: Final = re.compile(r"^/recommendations/([^/]+)/get/([^/]+)$")


def load_fixture() -> dict[str, Any]:
    data = resources.files("cloudops_connectors.corestack").joinpath(
        "fixtures/recommendations.json").read_bytes()
    loaded: dict[str, Any] = json.loads(data)
    return loaded


def _respond(status: int, payload: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers={"Content-Type": "application/json", **(headers or {})},
                          content=json.dumps(payload).encode())


class FakeCoreStack:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self.tenants = copy.deepcopy(fixture["tenants"])
        self.recommendations: dict[str, list[dict[str, Any]]] = copy.deepcopy(
            fixture["recommendations"])
        self.tokens: set[str] = set()
        self.throttle_next = 0
        self.requests: list[httpx.Request] = []

    @classmethod
    def from_fixtures(cls) -> FakeCoreStack:
        return cls(load_fixture())

    # --- what tests do between runs -------------------------------------------------

    def resolve(self, tenant: str, rec_id: str) -> None:
        """The recommendation is fixed: CoreStack stops listing it."""
        self.recommendations[tenant] = [r for r in self.recommendations[tenant]
                                        if r["id"] != rec_id]

    def expire_tokens(self) -> None:
        self.tokens.clear()

    # --- the API --------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return _respond(429, {"message": "Too many requests"}, {"Retry-After": "3"})
        path = request.url.path
        if request.method == "POST" and path == "/v1/auth/tokens":
            return self._token(request)
        if request.method != "GET":
            return _respond(405, {"message": "Method not allowed"})
        if request.headers.get("X-Auth-Token") not in self.tokens:
            return _respond(401, {"message": "Authentication required"})
        if (m := _LIST.match(path)) is not None:
            tenant = m.group(1)
            if tenant not in self.recommendations:
                return _respond(403, {"message": "Forbidden"})
            items = [{k: v for k, v in r.items() if k != "detail"}
                     for r in self.recommendations[tenant]]
            return _respond(200, self._page(items, "recommendations", request.url.params))
        if (m := _DETAIL.match(path)) is not None:
            tenant, rec_id = m.groups()
            if tenant not in self.recommendations:
                return _respond(403, {"message": "Forbidden"})
            found = next((r for r in self.recommendations[tenant] if r["id"] == rec_id), None)
            if found is None:
                return _respond(404, {"message": "Recommendation not found"})
            # As CoreStack's detail: the list's fields less id, tenant id and source.
            detail = {k: v for k, v in found.items()
                      if k not in ("detail", "id", "tenant_id", "source")}
            detail.update({k: v for k, v in found["detail"].items() if k != "resources"})
            return _respond(200, {**detail, **self._page(found["detail"]["resources"],
                                                         "resources", request.url.params)})
        return _respond(404, {"message": "Not found"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content)
        except ValueError:
            return _respond(400, {"message": "Bad request"})
        if (body.get("access_key"), body.get("secret_key")) != (FIXTURE_ACCESS_KEY,
                                                                FIXTURE_SECRET):
            return _respond(401, {"message": "Invalid credentials"})
        token = secrets.token_hex(16)
        self.tokens.add(token)
        return _respond(201, {
            "token": {"access_token": token, "expires_at": "2099-01-01 00:00:00.000000",
                      "issued_at": "2026-01-01 00:00:00.000000"},
            "tenants": copy.deepcopy(self.tenants),
            "user": {"active_tenant_id": self.tenants[0]["id"]},
        })

    @staticmethod
    def _page(items: list[dict[str, Any]], key: str,
              params: httpx.QueryParams) -> dict[str, Any]:
        limit = max(1, int(params.get("limit", "10")))
        page = max(1, int(params.get("page", "1")))
        return {"page_count": max(1, math.ceil(len(items) / limit)), "total_count": len(items),
                key: copy.deepcopy(items[(page - 1) * limit:page * limit])}
