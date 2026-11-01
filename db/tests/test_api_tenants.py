"""Editing and deleting a customer through the API (ADR 0038): who may, what the
refusals say, and that an offboarded tenant vanishes from every list, selector and
dashboard at once, comes back on restore, and is purged only after the retention.

Its own database: it offboards the seeded tenants."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from cloudops_tenancy import Database, TenantScope

from .conftest import Api, Seeded, _access, _database, _seeded, onboard_portal

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


@dataclass(frozen=True)
class World:
    seeded: Seeded
    api: Api
    admin: UUID  # platform administrator, every tenant
    mapper: UUID  # Customer master, every tenant
    engineer_a: UUID
    finops: UUID
    cxo_a: UUID


@pytest.fixture(scope="module")
async def world(admin_server: str) -> AsyncIterator[World]:
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from cloudops_api.app import create_app
    from cloudops_api.auth import StaticKeys, TokenValidator
    from cloudops_config import AuthConfig
    from cloudops_devdata.tokens import new_signing_key
    from cloudops_domain.assurance.pack import Ed25519Signer

    with _database(admin_server, "tenants") as provisioned:
        seeded = _seeded(provisioned)
        admin, mapper, engineer_a, finops, cxo_a = (uuid4() for _ in range(5))
        db = Database.create(seeded.app_dsn, pool_size=4, max_overflow=0)
        a = seeded.a.id
        async with db.transaction(TenantScope.for_resolution("setup:tenants")) as conn:
            await _access(conn, "SELECT access.bootstrap_admin(:o, 'Admin')", o=admin)
            for oid, role, tenant in ((mapper, "Customer master", None),
                                      (engineer_a, "Engineer", a), (finops, "FinOps", None)):
                await _access(conn, "SELECT access.set_principal(:a, :o, 'staff', :n, :r, :all)",
                              a=admin, o=oid, n=f"{role} {oid}", r=role, all=tenant is None)
                if tenant is not None:
                    await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)",
                                  a=admin, o=oid, t=tenant)
            await onboard_portal(conn, admin, a, cxo_a)
        auth = AuthConfig(
            issuer="https://login.example.test/77777777-7777-4777-8777-777777777777/v2.0",
            audience="api://cloudops-tenants", tenant_id="77777777-7777-4777-8777-777777777777",
            required_scope="access_as_user", jwks_url=None, jwks_secret=None,
        )
        private, public = new_signing_key("tenants")
        signer = Ed25519Signer(Ed25519PrivateKey.generate())
        app = create_app(db=db, validator=TokenValidator(auth, StaticKeys(public)), signer=signer)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://api") as client:
                yield World(seeded, Api(client, private, auth, signer), admin, mapper,
                            engineer_a, finops, cxo_a)
        finally:
            await db.dispose()


async def _post(w: World, path: str, oid: UUID, body: Any = None, status: int = 200) -> Any:
    response = await w.api.post(path, oid, body)
    assert response.status_code == status, (path, response.text)
    return response.json()


async def _get(w: World, path: str, oid: UUID, status: int = 200, **params: str) -> Any:
    response = await w.api.get(path, oid, params=params or None)
    assert response.status_code == status, (path, response.text)
    return response.json()


def _ids(body: Any) -> set[str]:
    """Every value under an ``id`` or ``tenant_id`` key, anywhere in a response."""
    found: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("id", "tenant_id") and isinstance(x, str):
                    found.add(x)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(body)
    return found


def _backdate(w: World, tenant: UUID) -> None:
    with psycopg.connect(w.seeded.admin_dsn) as cx:
        cx.execute("SET LOCAL ROLE cloudops_resolver")
        cx.execute("UPDATE cloudops.tenant SET purge_after = now() - interval '1 minute'"
                   " WHERE id = %s", (tenant,))


async def test_nobody_but_the_right_roles_reaches_these(world: World) -> None:
    b = world.seeded.b
    offboard = {"confirm_name": b.name, "reason": "test"}
    for oid in (world.cxo_a, world.engineer_a, world.finops, world.mapper):
        await _get(world, "/v1/tenants/offboarded", oid, status=403)
        await _post(world, f"/v1/tenants/{b.id}/offboard", oid, offboard, status=403)
        await _post(world, f"/v1/tenants/{b.id}/restore", oid, status=403)
        await _post(world, f"/v1/tenants/{b.id}/purge", oid,
                    {"confirm_name": b.name, "confirm_code": "BRAVO"}, status=403)
    for oid in (world.cxo_a, world.engineer_a, world.finops):
        await _post(world, f"/v1/tenants/{b.id}/edit", oid,
                    {"canonical_name": "X", "short_code": "BRAVO"}, status=403)
    # A tenant nobody has: the same 404 as one that does not exist.
    for path, body in ((f"/v1/tenants/{uuid4()}/offboard", offboard),
                       (f"/v1/tenants/{uuid4()}/restore", None)):
        assert (await _post(world, path, world.admin, body, status=404))["error"]["code"] \
            == "not_found"


async def test_a_customer_master_edits_a_name_and_a_code(world: World) -> None:
    b = world.seeded.b
    edited = await _post(world, f"/v1/tenants/{b.id}/edit", world.mapper,
                         {"canonical_name": "  Bravo Group  ", "short_code": "BRV"})
    assert edited == {"tenant": {"id": str(b.id), "name": "Bravo Group"}, "short_code": "BRV"}
    listed = await _get(world, "/v1/mapping/tenants", world.mapper)
    [row] = [t for t in listed if t["tenant"]["id"] == str(b.id)]
    assert (row["tenant"]["name"], row["short_code"], len(row["aliases"])) \
        == ("Bravo Group", "BRV", 1)
    clash = await _post(world, f"/v1/tenants/{b.id}/edit", world.mapper,
                        {"canonical_name": world.seeded.a.name, "short_code": "BRV"},
                        status=409)
    assert clash["error"]["code"] == "conflict"
    await _post(world, f"/v1/tenants/{b.id}/edit", world.mapper,
                {"canonical_name": "Bravo", "short_code": "lower"}, status=422)
    await _post(world, f"/v1/tenants/{b.id}/edit", world.mapper,
                {"canonical_name": b.name, "short_code": "BRAVO"})


async def test_offboarding_needs_the_exact_name_and_a_reason(world: World) -> None:
    a = world.seeded.a
    wrong = await _post(world, f"/v1/tenants/{a.id}/offboard", world.admin,
                        {"confirm_name": a.name.upper(), "reason": "Contract ended"},
                        status=422)
    assert wrong["error"]["code"] == "confirmation_mismatch"
    assert a.name not in str(wrong)
    assert str(a.id) not in str(wrong)
    await _post(world, f"/v1/tenants/{a.id}/offboard", world.admin,
                {"confirm_name": a.name, "reason": "   "}, status=422)
    await _post(world, f"/v1/tenants/{a.id}/offboard", world.admin,
                {"confirm_name": a.name}, status=422)


async def test_an_offboarded_tenant_vanishes_from_every_list_selector_and_dashboard(
    world: World,
) -> None:
    a = world.seeded.a
    paths = ("/v1/me", "/v1/customers", "/v1/mapping/tenants", "/v1/home/operations",
             "/v1/home/cost", "/v1/work-items", "/v1/resources")
    for path in paths:
        assert str(a.id) in _ids(await _get(world, path, world.admin)), path
    done = await _post(world, f"/v1/tenants/{a.id}/offboard", world.admin,
                       {"confirm_name": a.name, "reason": "Contract ended"})
    assert (done["status"], done["tenant"]["id"]) == ("offboarded", str(a.id))

    finops_paths = ("/v1/me", "/v1/customers", "/v1/home/cost", "/v1/resources")
    for oid, reachable in ((world.admin, paths), (world.finops, finops_paths)):
        for path in reachable:
            body = await _get(world, path, oid)
            assert str(a.id) not in _ids(body), path
            assert a.name not in str(body), path
        # The selector: naming it is the same 404 as a tenant that never existed.
        await _get(world, "/v1/resources", oid, status=404, tenant_id=str(a.id))
    # Named staff of it alone are left with no scope; its customers with no access.
    me = await world.api.get("/v1/me", world.engineer_a)
    assert me.status_code == 403 or _ids(me.json()) & {str(a.id)} == set()
    assert (await world.api.get("/v1/me", world.cxo_a)).status_code == 403

    listed = await _get(world, "/v1/tenants/offboarded", world.admin)
    [row] = [t for t in listed if t["tenant"]["id"] == str(a.id)]
    assert (row["status"], row["reason"], row["offboarded_by_me"], row["may_restore"],
            row["may_purge"], row["purge"]) == ("offboarded", "Contract ended", True, True,
                                                False, None)


async def test_purge_is_refused_before_the_retention_ends(world: World) -> None:
    a = world.seeded.a
    body = {"confirm_name": a.name, "confirm_code": "ALPHA"}
    early = await _post(world, f"/v1/tenants/{a.id}/purge", world.admin, body, status=409)
    assert early["error"]["code"] == "retention_not_ended"


async def test_restore_brings_it_back_everywhere(world: World) -> None:
    a = world.seeded.a
    assert await _post(world, f"/v1/tenants/{a.id}/restore", world.admin) \
        == {"tenant_id": str(a.id), "status": "restored"}
    assert str(a.id) in _ids(await _get(world, "/v1/customers", world.admin))
    assert str(a.id) in _ids(await _get(world, "/v1/me", world.engineer_a))
    # The portal stays suspended until a platform administrator sets it live again.
    assert (await world.api.get("/v1/me", world.cxo_a)).status_code == 403
    gone = await _get(world, "/v1/tenants/offboarded", world.admin)
    assert str(a.id) not in _ids(gone)
    # Live again, so nothing to restore.
    again = await _post(world, f"/v1/tenants/{a.id}/restore", world.admin, status=409)
    assert again["error"]["code"] == "tenant_state"


async def test_purge_after_the_retention_is_a_request_with_two_confirmations(
    world: World,
) -> None:
    c = world.seeded.c
    await _post(world, f"/v1/tenants/{c.id}/offboard", world.admin,
                {"confirm_name": c.name, "reason": "Customer left"})
    _backdate(world, c.id)
    expired = await _post(world, f"/v1/tenants/{c.id}/restore", world.admin, status=409)
    assert expired["error"]["code"] == "retention_ended"
    listed = await _get(world, "/v1/tenants/offboarded", world.admin)
    [row] = [t for t in listed if t["tenant"]["id"] == str(c.id)]
    assert (row["may_restore"], row["may_purge"]) == (False, True)
    for name, code in ((c.name, "WRONG"), (c.name.upper(), "CHARLIE")):
        wrong = await _post(world, f"/v1/tenants/{c.id}/purge", world.admin,
                            {"confirm_name": name, "confirm_code": code}, status=422)
        assert wrong["error"]["code"] == "confirmation_mismatch"
    accepted = await _post(world, f"/v1/tenants/{c.id}/purge", world.admin,
                           {"confirm_name": c.name, "confirm_code": "CHARLIE"}, status=202)
    assert (accepted["status"], accepted["may_purge"], accepted["purge"]["status"]) \
        == ("purging", False, "requested")
    twice = await _post(world, f"/v1/tenants/{c.id}/purge", world.admin,
                        {"confirm_name": c.name, "confirm_code": "CHARLIE"}, status=409)
    assert twice["error"]["code"] == "tenant_state"
