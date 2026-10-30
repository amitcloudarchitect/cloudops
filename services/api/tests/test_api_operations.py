"""Console support without a database (ADR 0014): CORS for the browser app, and
the triage values a reviewer may set kept equal to the classifier's schema."""

from __future__ import annotations

import typing
from typing import Any

import httpx
import pytest

from cloudops_agents import classifier
from cloudops_api import operations
from cloudops_api.app import create_app
from cloudops_api.auth import StaticKeys, TokenValidator
from cloudops_config import AuthConfig, ConfigurationError
from cloudops_config.settings import _web_origins
from cloudops_tenancy import Database

ORIGIN = "https://console.cloudops.example"


def _app(origins: tuple[str, ...]) -> Any:
    auth = AuthConfig("https://i", "a", "t", "s", None, None)
    db = Database.create("postgresql://nobody:x@127.0.0.1:1/none")
    return create_app(db=db, validator=TokenValidator(auth, StaticKeys({"keys": []})),
                      web_origins=origins)


def test_override_values_are_the_classifiers() -> None:
    assert set(operations.INTENTS) == set(typing.get_args(classifier.Intent))
    assert set(operations.ACTION_CLASSES) == set(typing.get_args(classifier.ActionClass))
    assert set(operations.RISKS) == set(typing.get_args(classifier.Risk))
    assert set(operations.REVIEWABLE) <= set(classifier.TicketTriage.model_fields)


async def _preflight(app: Any, origin: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://api") as client:
        return await client.options("/v1/work-items", headers={
            "Origin": origin, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        })


async def test_the_console_origin_may_send_a_bearer_token() -> None:
    response = await _preflight(_app((ORIGIN,)), ORIGIN)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in response.headers


async def test_any_other_origin_is_not_allowed() -> None:
    for app in (_app((ORIGIN,)), _app(())):
        response = await _preflight(app, "https://evil.example")
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("value", [
    "http://console.example", "https://*.example", "https://console.example/app",
    "https://u:p@console.example", "*",
])
def test_web_origins_must_be_exact_https_origins(value: str) -> None:
    with pytest.raises(ConfigurationError):
        _web_origins(value)


def test_web_origins_parse() -> None:
    assert _web_origins("") == ()
    assert _web_origins(f" {ORIGIN}/ , https://b.example") == (ORIGIN, "https://b.example")
