"""Provenance cannot be forgotten: a structural check over every response model
the API serves, plus the rules a Metric enforces on its own."""

from __future__ import annotations

import types
import typing
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from cloudops_api.app import ROUTERS, check_router, create_app
from cloudops_api.auth import StaticKeys, TokenValidator
from cloudops_api.pipeline import REQUIRED_PERMISSION
from cloudops_api.provenance import Metric, query_ref
from cloudops_config import AuthConfig
from cloudops_tenancy import Database

NUMERIC = (int, float, Decimal, complex)


def _app() -> Any:
    auth = AuthConfig("https://i", "a", "t", "s", None, None)
    # Connections are lazy: nothing is opened by building the app.
    db = Database.create("postgresql://nobody:x@127.0.0.1:1/none")
    return create_app(db=db, validator=TokenValidator(auth, StaticKeys({"keys": []})))


def _numeric_fields(annotation: Any, path: str, seen: set[type]) -> list[str]:
    origin = typing.get_origin(annotation)
    if isinstance(annotation, type) and issubclass(annotation, Metric):
        return []
    if origin is not None and isinstance(origin, type) and issubclass(origin, Metric):
        return []
    if annotation is bool:
        return []
    if isinstance(annotation, type) and issubclass(annotation, NUMERIC):
        return [path]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return []
        seen.add(annotation)
        found: list[str] = []
        for name, field in annotation.model_fields.items():
            found += _numeric_fields(field.annotation, f"{path}.{name}", seen)
        return found
    if origin in (list, set, frozenset, tuple, dict, typing.Union, types.UnionType):
        found = []
        for arg in typing.get_args(annotation):
            found += _numeric_fields(arg, path, seen)
        return found
    return []


def _routes() -> list[APIRoute]:
    return [r for router in ROUTERS for r in router.routes if isinstance(r, APIRoute)]


def test_every_route_declares_a_permission_and_a_response_model() -> None:
    routes = _routes()
    assert len(routes) >= 5
    for route in routes:
        assert isinstance(getattr(route.endpoint, REQUIRED_PERMISSION, None), str), route.path
        assert route.response_model is not None, route.path


def test_no_response_carries_a_number_outside_a_metric() -> None:
    offenders: list[str] = []
    for route in _routes():
        offenders += _numeric_fields(route.response_model, route.path, set())
    assert offenders == []


async def test_every_route_refuses_a_request_without_a_token() -> None:
    """Behavioural proof the pipeline sits in front of every endpoint."""
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        for route in _routes():
            path = route.path_format
            for name in ("resource_id", "triage_id"):
                path = path.replace(f"{{{name}}}", str(UUID(int=1)))
            for method in sorted(route.methods or ()):
                response = await client.request(method, path.replace("{number}", "INC0000001"))
                assert response.status_code == 401, (method, path)
                assert response.json()["error"]["code"] == "missing_token"


def test_a_router_with_a_plain_route_is_refused() -> None:
    plain = APIRouter()

    @plain.get("/sneaky")
    async def sneaky() -> dict[str, str]:
        return {}

    with pytest.raises(RuntimeError, match="AuditedRoute"):
        check_router(plain)


def test_the_structural_check_catches_a_bare_number() -> None:
    class Leaky(BaseModel):
        total: Decimal

    class Wrapper(BaseModel):
        items: list[Leaky]

    assert _numeric_fields(Wrapper, "x", set()) == ["x.items.total"]


def _metric(**overrides: Any) -> dict[str, Any]:
    return {
        "value": Decimal("1.500000"),
        "unit": "EUR",
        "source_system": "azure_cost_management",
        "as_of": datetime(2026, 10, 28, tzinfo=UTC),
        "query_ref": "cost.x.v1:abc",
        "is_modelled": False,
        **overrides,
    }


@pytest.mark.parametrize(
    "field", ["value", "unit", "source_system", "as_of", "query_ref", "is_modelled"]
)
def test_every_provenance_field_is_required(field: str) -> None:
    data = _metric()
    del data[field]
    with pytest.raises(ValidationError):
        Metric[Decimal](**data)


@pytest.mark.parametrize(
    "bad",
    [
        {"value": 0.1},
        {"value": True},
        {"as_of": datetime(2026, 10, 28)},
        {"unit": " "},
        {"query_ref": ""},
    ],
)
def test_metric_refuses_floats_naive_times_and_blanks(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Metric[Decimal](**_metric(**bad))


def test_money_serialises_as_a_string_not_a_float() -> None:
    body = Metric[Decimal](**_metric()).model_dump(mode="json")
    assert body["value"] == "1.500000"


def test_query_refs_are_stable_and_carry_no_parameters() -> None:
    a = query_ref("cost.summary", 1, tenant="54190aa0-4b8a-5d71-b0f1-4fea5cf3fb7b", days=30)
    b = query_ref("cost.summary", 1, days=30, tenant="54190aa0-4b8a-5d71-b0f1-4fea5cf3fb7b")
    assert a == b
    assert "54190aa0" not in a
    assert a != query_ref("cost.summary", 1, days=31)
