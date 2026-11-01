"""The resource list pages through everything in scope and says how much there
is: a page is never mistaken for the whole (Resources screen). Staff see each
resource's customer; customers see their own tenant only, on every page."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from .conftest import Api, Principals, Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


async def _pages(api: Api, oid: UUID, limit: int) -> tuple[list[dict[str, Any]], list[Any]]:
    items: list[dict[str, Any]] = []
    bodies: list[Any] = []
    cursor: str | None = "0"
    while cursor is not None:
        response = await api.get("/v1/resources", oid,
                                 params={"limit": str(limit), "cursor": cursor})
        assert response.status_code == 200, response.text
        body = response.json()
        bodies.append(body)
        items += body["items"]
        cursor = body["next_cursor"]
        assert len(bodies) < 1000
    return items, bodies


async def test_paging_returns_everything_once_and_the_true_total(
    api: Api, principals: Principals,
) -> None:
    items, bodies = await _pages(api, principals.finops_all, limit=1)
    total = bodies[0]["count"]["value"]
    assert total >= 2
    assert len(bodies) == total
    assert len(items) == len({i["id"] for i in items}) == total
    assert [b["next_cursor"] for b in bodies] == [str(n) for n in range(1, total)] + [None]
    names = [(i["name"], i["id"]) for i in items]
    assert names == sorted(names)
    whole = (await api.get("/v1/resources", principals.finops_all)).json()
    assert whole["next_cursor"] is None
    assert {i["id"] for i in whole["items"]} == {i["id"] for i in items}


async def test_staff_see_each_resources_customer(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    items, _ = await _pages(api, principals.finops_all, limit=500)
    by_id = {i["id"]: i for i in items}
    c = by_id[str(seeded.c.resource_id)]
    assert c["customer"] == seeded.c.name
    assert c["tenant_id"] == str(seeded.c.id)


async def test_a_customer_pages_through_its_own_tenant_only(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    items, bodies = await _pages(api, principals.customer_a, limit=1)
    assert {i["id"] for i in items} == {str(seeded.a.resource_id)}
    assert bodies[0]["count"]["value"] == 1
    assert all("customer" not in i for i in items)


@pytest.mark.parametrize("params", [
    {"limit": "0"}, {"limit": "501"}, {"cursor": "-1"}, {"cursor": "abc"}])
async def test_a_page_outside_the_bounds_is_refused(
    api: Api, principals: Principals, params: dict[str, str],
) -> None:
    assert (await api.get("/v1/resources", principals.finops_all, params=params)).status_code \
        == 422
