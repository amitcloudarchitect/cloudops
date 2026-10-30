"""How the API process serves (ADR 0011): it keeps idle connections open longer
than the ingress proxies do, so a proxy never reuses a connection the API has
just closed (a 503 with Envoy's UC flag)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn

import cloudops_api.__main__ as server
from cloudops_config import Environment
from cloudops_tenancy import Database


def test_the_api_outlasts_the_proxys_idle_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}
    settings = SimpleNamespace(
        auth=SimpleNamespace(jwks_secret=None, jwks_url="https://login.example/keys"),
        environment=Environment.AZURE,
        database_dsn=lambda login: "postgresql://app@db/cloudops",
    )
    monkeypatch.setattr(server, "load_settings", lambda environ, require: settings)
    monkeypatch.setattr(Database, "create", staticmethod(lambda dsn: object()))
    monkeypatch.setattr(server, "TokenValidator", lambda auth, keys: object())
    monkeypatch.setattr(server, "create_app", lambda **kwargs: "app")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.update(kwargs))

    assert server.main() == 0
    assert calls["timeout_keep_alive"] == server.KEEP_ALIVE_SECONDS
    assert server.KEEP_ALIVE_SECONDS > server.ENVOY_UPSTREAM_IDLE_SECONDS
    assert calls["host"] == "0.0.0.0"  # noqa: S104 - the kubelet and the gateway reach the pod IP
