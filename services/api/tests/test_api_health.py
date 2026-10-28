"""The probes: unauthenticated, data-free, and the only routes outside the pipeline."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.routing import APIRoute

from cloudops_api.app import ROUTERS, check_router, create_app
from cloudops_api.auth import StaticKeys, TokenValidator
from cloudops_api.health import PROBES
from cloudops_config import AuthConfig
from cloudops_tenancy import Database


def _app() -> Any:
    auth = AuthConfig("https://i", "a", "t", "s", None, None)
    # Nothing listens on port 1: the database is unreachable.
    db = Database.create("postgresql://nobody:x@127.0.0.1:1/none")
    return create_app(db=db, validator=TokenValidator(auth, StaticKeys({"keys": []})))


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        return await client.get(path)


async def test_healthz_answers_without_a_token() -> None:
    response = await _get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_is_503_with_no_detail_when_the_database_is_down() -> None:
    response = await _get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "127.0.0.1" not in response.text


def test_the_probes_are_the_only_unaudited_routes() -> None:
    """Checked on the routers, as ``check_router`` is: FastAPI wraps included routes."""
    assert sorted(r.path for r in PROBES.routes if isinstance(r, APIRoute)) == ["/healthz",
                                                                                "/readyz"]
    assert PROBES not in ROUTERS
    for router in ROUTERS:
        check_router(router)
