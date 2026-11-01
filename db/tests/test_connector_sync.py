"""Connector sync (migration 0034, ADR 0037): the six-hour expected interval, the
requests the database queues when a customer is onboarded, and the worker's claim.
A request carries a source and a reason only, the application can neither read nor
write one, and the claim triggers each schedule once or leaves the request waiting."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text
from temporalio.client import Client, ScheduleOverlapPolicy
from temporalio.service import RPCError, RPCStatusCode

from cloudops_connectors.core import sync
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Principals, Seeded
from .support import raw_scope

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
async def mapper(seeded: Seeded, principals: Principals) -> UUID:
    """A Customer master with every tenant, approving alone (dev's policy)."""
    oid = uuid4()
    db = Database.create(seeded.app_dsn, pool_size=1, max_overflow=0)
    try:
        async with db.transaction(TenantScope.for_resolution("setup:sync")) as conn:
            await conn.execute(text(
                "SELECT access.set_principal(:a, :o, 'staff', 'sync', 'Customer master', true)"),
                {"a": principals.admin, "o": oid})
    finally:
        await db.dispose()
    return oid


@pytest.fixture(autouse=True)
def empty(seeded: Seeded) -> Iterator[None]:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("DELETE FROM staging.sync_request")
        cx.execute("UPDATE access.mapping_policy SET approvers = 1")
    yield
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("UPDATE access.mapping_policy SET approvers = 2")


def _waiting(seeded: Seeded) -> dict[str, str]:
    with psycopg.connect(seeded.admin_dsn) as cx:
        return {str(s): str(r) for s, r in cx.execute(
            "SELECT source_system, reason FROM staging.sync_request WHERE claimed_at IS NULL")}


async def _call(db: Database, tenants: list[UUID], sql: str, **params: Any) -> Any:
    async with db.transaction(TenantScope(tenants, Role.DELIVERY_LEAD, "test:sync")) as conn:
        return (await conn.execute(text(sql), params)).all()


async def _onboard(app_db: Database, seeded: Seeded, mapper: UUID) -> UUID:
    code = f"S{uuid4().hex[:6].upper()}"
    rows = await _call(app_db, [seeded.a.id], "SELECT access.onboard_tenant(:a, :n, :c)",
                       a=mapper, n=f"Sync {code}", c=code)
    return UUID(str(rows[0][0]))


def test_the_sources_named_are_expected_every_six_hours(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        every = dict(((s, e), v) for s, e, v in cx.execute(
            "SELECT source_system, entity, every FROM staging.connector_schedule"))
    for key in (("sharepoint", "drive_item"), ("sharepoint", "folder_item"),
                ("qualys", "host_detection"), ("corestack", "finding")):
        assert every[key] == sync.CUSTOMER_SYNC, key
    # Not these: ServiceNow stays at five minutes, the FX feed daily.
    assert every[("servicenow", "incident")] < sync.CUSTOMER_SYNC
    assert every[("ecb", "fx")] > sync.CUSTOMER_SYNC


def test_the_worker_and_the_database_agree_on_the_sources(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        (check,) = cx.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'staging.sync_request'::regclass"
            "   AND pg_get_constraintdef(oid) LIKE '%source_system%'").fetchone() or ("",)
    assert {s for s in sync.SYNC_SOURCES if f"'{s}'" in check} == sync.SYNC_SOURCES


async def test_onboarding_a_tenant_queues_one_sync_of_each_source(
    app_db: Database, seeded: Seeded, mapper: UUID
) -> None:
    await _onboard(app_db, seeded, mapper)
    assert _waiting(seeded) == dict.fromkeys(sync.SYNC_SOURCES, "tenant_onboarded")
    # A second customer while those wait adds nothing: one waiting request a source.
    await _onboard(app_db, seeded, mapper)
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT count(*) FROM staging.sync_request").fetchone() == (3,)


async def test_an_approved_alias_queues_its_own_source_only(
    app_db: Database, seeded: Seeded, mapper: UUID
) -> None:
    for source in ("corestack", "servicenow"):
        alias = f"Sync Alias {uuid4().hex[:6]}"
        rows = await _call(app_db, [seeded.c.id],
                           "SELECT access.propose_alias(:a, :s, :al, :t, 'test')",
                           a=mapper, s=source, al=alias, t=seeded.c.id)
        await _call(app_db, [seeded.c.id], "SELECT access.decide_alias(:a, :p, 'approve', NULL)",
                    a=mapper, p=rows[0][0])
    # ServiceNow syncs every five minutes, and its held records move by re-process.
    assert _waiting(seeded) == {"corestack": "alias_approved"}


def test_an_account_added_queues_the_sources_that_link_to_accounts(seeded: Seeded) -> None:
    with raw_scope(seeded.ingest_dsn, str(seeded.b.id), "ingest") as cx:
        cx.execute("INSERT INTO cloudops.cloud_account (tenant_id, provider, native_id,"
                   " display_name) VALUES (%s, 'azure', %s, 'sync test')",
                   (seeded.b.id, str(uuid4())))
        # raw_scope rolls back on exit: read the queue in the same transaction.
        waiting = dict(cx.execute("SELECT source_system, reason FROM staging.sync_request"))
    assert waiting == {"corestack": "account_added", "qualys": "account_added"}


def test_the_application_can_neither_read_nor_write_a_request(seeded: Seeded) -> None:
    for sql in ("SELECT count(*) FROM staging.sync_request",
                "INSERT INTO staging.sync_request (source_system, reason)"
                " VALUES ('qualys', 'tenant_onboarded')"):
        with raw_scope(seeded.app_dsn, str(seeded.a.id), "platform_admin") as cx, \
                pytest.raises(psycopg.errors.InsufficientPrivilege):
            cx.execute(sql)
    # Nor the ingest role directly: only the database queues.
    with raw_scope(seeded.ingest_dsn, "", "ingest") as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute("INSERT INTO staging.sync_request (source_system, reason)"
                   " VALUES ('qualys', 'tenant_onboarded')")


class _Handle:
    def __init__(self, temporal: _Temporal, schedule_id: str) -> None:
        self.temporal, self.id = temporal, schedule_id

    async def trigger(self, *, overlap: ScheduleOverlapPolicy | None = None) -> None:
        if self.id in self.temporal.missing:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        if self.id in self.temporal.failing:
            raise RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
        self.temporal.triggered.append((self.id, overlap))


class _Temporal:
    def __init__(self, missing: set[str] | None = None,
                 failing: set[str] | None = None) -> None:
        self.missing, self.failing = missing or set(), failing or set()
        self.triggered: list[tuple[str, ScheduleOverlapPolicy | None]] = []

    def get_schedule_handle(self, schedule_id: str) -> _Handle:
        return _Handle(self, schedule_id)


async def test_the_worker_claims_its_request_and_triggers_each_schedule_once(
    app_db: Database, ingest_db: Database, seeded: Seeded, mapper: UUID
) -> None:
    await _onboard(app_db, seeded, mapper)
    fake = _Temporal(missing={"sharepoint-drive_item"})
    ids = ["sharepoint-drive_item", "sharepoint-folder_item"]
    assert await sync.claim(ingest_db, cast(Client, fake), "sharepoint", ids) == [
        "sharepoint-folder_item"]
    # BUFFER_ONE: a run already going started before the customer existed.
    assert fake.triggered == [("sharepoint-folder_item", ScheduleOverlapPolicy.BUFFER_ONE)]
    assert set(_waiting(seeded)) == {"qualys", "corestack"}, "only its own source"
    # Claimed: the next pass has nothing to do.
    assert await sync.claim(ingest_db, cast(Client, fake), "sharepoint", ids) == []
    assert len(fake.triggered) == 1


async def test_a_trigger_that_fails_leaves_the_request_waiting(
    app_db: Database, ingest_db: Database, seeded: Seeded, mapper: UUID
) -> None:
    await _onboard(app_db, seeded, mapper)
    failing = _Temporal(failing={"corestack-finding"})
    with pytest.raises(RPCError):
        await sync.claim(ingest_db, cast(Client, failing), "corestack", ["corestack-finding"])
    assert "corestack" in _waiting(seeded)
    working = _Temporal()
    assert await sync.claim(ingest_db, cast(Client, working), "corestack",
                            ["corestack-finding"]) == ["corestack-finding"]
    assert "corestack" not in _waiting(seeded)


async def test_claimed_requests_are_cleared_after_a_month(
    ingest_db: Database, seeded: Seeded
) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO staging.sync_request (source_system, reason, requested_at,"
                   " claimed_at) VALUES ('qualys', 'tenant_onboarded', now() - interval '40 days',"
                   " now() - interval '40 days'), ('qualys', 'account_added',"
                   " now() - interval '2 days', now() - interval '2 days')")
    await sync.claim(ingest_db, cast(Client, _Temporal()), "qualys", ["qualys-host_detection"])
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert [r[0] for r in cx.execute("SELECT reason FROM staging.sync_request")] == [
            "account_added"]
