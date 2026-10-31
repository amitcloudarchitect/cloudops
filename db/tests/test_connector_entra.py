"""The Entra ID connector end to end: the generic connector workflow on a
time-skipping Temporal server, the migrated database (a module of its own,
holding the synthetic tenants), and the recorded directories served by the fake
Graph through the real client, one client per directory.

Ground truth is the fixture (``cloudops_devdata.graph_fixtures``). The synthetic
dataset has no ``entra`` aliases, so this module maps the known directories as
a person would through the customer mapping (``graph_fixtures.aliases``); the
third directory is mapped by nobody and must be quarantined, never written.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from temporalio import activity
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    PageOutcome,
    ProcessRequest,
    ReplayRequest,
    RunReport,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_connectors.entra import mapping
from cloudops_connectors.entra.fake import FIXTURE_GRAPH, FakeEntra, load_fixture
from cloudops_connectors.entra.source import EntraSource
from cloudops_connectors.graph.client import GraphClient
from cloudops_devdata.graph_fixtures import aliases
from cloudops_devdata.synthetic import tenant_id
from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, raw_first, rows, run

pytestmark = [pytest.mark.integration]

USER, ROLES = mapping.USER, mapping.ROLE_ASSIGNMENT
DIRS = {d["directory_id"]: d for d in load_fixture()["directories"]}
NWL, CRL, UNKNOWN = (d["directory_id"] for d in load_fixture()["directories"])


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(scope="module")
def mapped(synthetic_module: Synthetic) -> Synthetic:
    with psycopg.connect(synthetic_module.admin_dsn, autocommit=True) as cx:
        for source, alias, code in aliases():
            if source == mapping.SOURCE:
                cx.execute("INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias,"
                           " confirmed_by) VALUES (%s, %s, %s, 'test:mapping')"
                           " ON CONFLICT DO NOTHING", (tenant_id(code), source, alias))
    return synthetic_module


@pytest.fixture
async def db(mapped: Synthetic) -> AsyncIterator[Database]:
    with psycopg.connect(mapped.admin_dsn, autocommit=True) as cx:
        for table in ("cloudops.directory_principal", "cloudops.directory_role_assignment"):
            cx.execute(f"DELETE FROM {table}")
        cx.execute("DELETE FROM cloudops.tenant_alias WHERE source_system = 'entra'"
                   " AND alias = %s", (UNKNOWN,))
        for table in ("staging.tenant_alias_quarantine", "staging.ingest_cursor",
                      "staging.ingest_watermark", "staging.schema_field"):
            cx.execute(f"DELETE FROM {table} WHERE source_system = 'entra'")
    database = Database.create(mapped.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _source(fake: FakeEntra) -> EntraSource:
    return EntraSource({d: GraphClient(FIXTURE_GRAPH, transport=fake.transport(d),
                                       sleep=no_sleep) for d in fake.directories})


async def _ingest(env: WorkflowEnvironment, acts: ConnectorActivities,
                  entity: str) -> RunReport:
    (report,) = await run(env, acts, ConnectorIngest.run, [IngestRequest(entity, SINCE)])
    return report


def _users(dsn: str, directory: str) -> dict[str, tuple[Any, ...]]:
    return {r[0]: r[1:] for r in rows(
        dsn, "SELECT object_id, tenant_id, display_name, user_principal_name, account_enabled,"
             " user_type, deleted_at, raw_extra FROM cloudops.directory_principal"
             " WHERE directory_id = %s AND kind = 'user'", directory)}


def _assignments(dsn: str, directory: str) -> dict[str, tuple[Any, ...]]:
    return {r[0]: r[1:] for r in rows(
        dsn, "SELECT assignment_id, tenant_id, principal_object_id, role_name, removed_at,"
             " last_seen FROM cloudops.directory_role_assignment WHERE directory_id = %s",
        directory)}


# --- users ----------------------------------------------------------------------------------


async def test_users_land_in_their_directorys_tenant_and_an_unknown_one_is_quarantined(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    raw, fake = MemoryRawStore(), FakeEntra.from_fixtures(page_size=3)
    report = await _ingest(env, ConnectorActivities(db, raw, _source(fake)), USER)
    assert report.status == "succeeded"
    unknown_records = len(DIRS[UNKNOWN]["users"])
    assert report.counts.quarantined == unknown_records
    assert report.counts.rejected == 2, "one user with no id in each mapped directory"
    for directory, code in ((NWL, "NWL"), (CRL, "CRL")):
        users = _users(dsn, directory)
        good = [u for u in DIRS[directory]["users"] if "id" in u]
        assert set(users) == {u["id"] for u in good}
        for u in good:
            tenant, name, upn, enabled, kind, deleted, extra = users[u["id"]]
            assert (tenant, name, upn, enabled, kind, deleted) == (
                tenant_id(code), u["displayName"], u["userPrincipalName"],
                u["accountEnabled"], u["userType"], None)
            assert extra == ({"employeeType": u["employeeType"]} if "employeeType" in u
                             else None)
    # Nothing the unknown directory carries was written; it waits in quarantine.
    assert _users(dsn, UNKNOWN) == {}
    assert rows(dsn, "SELECT alias, occurrence_count FROM staging.tenant_alias_quarantine"
                     " WHERE source_system = 'entra'") == [(UNKNOWN, unknown_records)]
    assert rows(dsn, "SELECT count(*) FROM cloudops.directory_principal"
                     " WHERE raw_extra::text ILIKE '%%password%%'") == [(0,)]
    # Delta links for the mapped directories only, from their committed final pages.
    cursors = dict(rows(dsn, "SELECT scope_key, cursor FROM staging.ingest_cursor"
                             " WHERE source_system = 'entra'"))
    assert set(cursors) == {f"{FIXTURE_GRAPH}|{NWL}", f"{FIXTURE_GRAPH}|{CRL}"}
    assert all("$deltatoken=" in c for c in cursors.values())
    assert raw_first(raw.events)


async def test_a_second_run_reads_the_delta_link_and_applies_changes_and_deletions(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    fake = FakeEntra.from_fixtures(page_size=3)
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await _ingest(env, acts, USER)
    crl_before = _users(dsn, CRL)
    cursor = rows(dsn, "SELECT cursor FROM staging.ingest_cursor WHERE scope_key = %s",
                  f"{FIXTURE_GRAPH}|{NWL}")[0][0]
    fake.apply_user_changes(NWL)
    before = len(fake.requests)
    second = await _ingest(env, acts, USER)
    assert second.status == "succeeded"
    assert cursor in [str(r.url) for r in fake.requests[before:]]
    assert second.counts.records == 3 + unknown_full(), "mapped directories send only changes"
    disabled, removed, guest = DIRS[NWL]["user_changes"]
    users = _users(dsn, NWL)
    assert users[disabled["id"]][3] is False
    assert users[removed["id"]][5] is not None, "deleted, and kept as evidence"
    assert users[guest["id"]][4] == "Guest"
    assert _users(dsn, CRL) == crl_before


def unknown_full() -> int:
    """The unknown directory's delta link is never persisted: every run is full."""
    return len(DIRS[UNKNOWN]["users"])


async def test_a_directory_mapped_later_is_read_from_the_start(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    fake = FakeEntra.from_fixtures(page_size=3)
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await _ingest(env, acts, USER)
    with psycopg.connect(dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias,"
                   " confirmed_by) VALUES (%s, 'entra', %s, 'test:mapping')",
                   (tenant_id("FAB"), UNKNOWN))
    report = await _ingest(env, acts, USER)
    assert report.counts.quarantined == 0
    assert {u[0] for u in _users(dsn, UNKNOWN).values()} == {tenant_id("FAB")}
    assert len(_users(dsn, UNKNOWN)) == len(DIRS[UNKNOWN]["users"]) - 1


# --- role assignments ---------------------------------------------------------------------


class _FailsFinalPage(ConnectorActivities):
    @activity.defn(name="process")
    async def process(self, req: ProcessRequest) -> PageOutcome:
        if req.final:
            raise RuntimeError("normaliser defect")
        return await super().process(req)


async def test_the_role_assignment_snapshot_closes_only_what_a_whole_run_did_not_see(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    raw, fake = MemoryRawStore(), FakeEntra.from_fixtures(page_size=2)
    acts = ConnectorActivities(db, raw, _source(fake))
    first = await _ingest(env, acts, ROLES)
    assert first.status == "succeeded"
    assert first.counts.rejected == 2
    expected = [a for a in DIRS[NWL]["role_assignments"] if "principalId" in a]
    got = _assignments(dsn, NWL)
    assert set(got) == {a["id"] for a in expected}
    for a in expected:
        tenant, principal, role, removed, _ = got[a["id"]]
        assert (tenant, principal, role, removed) == (
            tenant_id("NWL"), a["principalId"], a["roleDefinition"]["displayName"], None)
    assert _assignments(dsn, UNKNOWN) == {}

    # One assignment is withdrawn: the next whole run closes it, and only it.
    gone = expected[0]["id"]
    fake.unassign(NWL, gone)
    second = await _ingest(env, acts, ROLES)
    assert second.status == "succeeded"
    now = _assignments(dsn, NWL)
    assert now[gone][3] is not None
    assert all(v[3] is None for k, v in now.items() if k != gone)
    assert all(v[3] is None for v in _assignments(dsn, CRL).values())

    # A run that stops before its final page closes nothing.
    fake.unassign(NWL, expected[1]["id"])
    failed = await _ingest(env, _FailsFinalPage(db, raw, _source(fake)), ROLES)
    assert failed.status == "dead_lettered"
    assert _assignments(dsn, NWL)[expected[1]["id"]][3] is None

    # Replay never closes and never reopens.
    snapshot = {d: _assignments(dsn, d) for d in (NWL, CRL)}
    requests = len(fake.requests)
    day = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(ROLES, day, day)])
    assert replay.status == "succeeded"
    assert len(fake.requests) == requests, "replay never touches Graph"
    after = {d: _assignments(dsn, d) for d in (NWL, CRL)}
    assert {d: {k: v[:4] for k, v in a.items()} for d, a in after.items()} == {
        d: {k: v[:4] for k, v in a.items()} for d, a in snapshot.items()}
    assert all(after[d][k][4] >= snapshot[d][k][4] for d in after for k in after[d])


async def test_replay_of_users_never_refetches_or_brings_back_a_deleted_user(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    dsn = mapped.admin_dsn
    raw, fake = MemoryRawStore(), FakeEntra.from_fixtures(page_size=3)
    acts = ConnectorActivities(db, raw, _source(fake))
    await _ingest(env, acts, USER)
    fake.apply_user_changes(NWL)
    await _ingest(env, acts, USER)
    snapshot = {d: _users(dsn, d) for d in (NWL, CRL)}
    cursors = rows(dsn, "SELECT scope_key, cursor FROM staging.ingest_cursor ORDER BY 1")
    requests = len(fake.requests)
    day = "-".join(next(iter(raw.pages)).split("/")[2:5])
    (replay,) = await run(env, acts, ConnectorReplay.run, [ReplayRequest(USER, day, day)])
    assert replay.status == "succeeded"
    assert len(fake.requests) == requests
    removed = DIRS[NWL]["user_changes"][1]["id"]
    assert _users(dsn, NWL)[removed][5] is not None
    assert {d: {k: v[5] is None for k, v in _users(dsn, d).items()} for d in (NWL, CRL)} == {
        d: {k: v[5] is None for k, v in s.items()} for d, s in snapshot.items()}
    assert rows(dsn, "SELECT scope_key, cursor FROM staging.ingest_cursor ORDER BY 1") == cursors


# --- isolation --------------------------------------------------------------------------------


@pytest.mark.isolation
async def test_one_customers_directory_is_invisible_to_another(
    env: WorkflowEnvironment, db: Database, mapped: Synthetic
) -> None:
    fake = FakeEntra.from_fixtures()
    acts = ConnectorActivities(db, MemoryRawStore(), _source(fake))
    await _ingest(env, acts, USER)
    await _ingest(env, acts, ROLES)
    nwl, crl = tenant_id("NWL"), tenant_id("CRL")
    app = Database.create(mapped.app_dsn, pool_size=1, max_overflow=0)
    by_dir = ("SELECT count(*) FROM cloudops.{} WHERE directory_id = :d")
    try:
        async with app.transaction(TenantScope({crl}, Role.SECOPS, "test:crl")) as conn:
            for table in ("directory_principal", "directory_role_assignment"):
                assert await conn.scalar(text(by_dir.format(table)), {"d": NWL}) == 0
                assert await conn.scalar(text(by_dir.format(table)), {"d": CRL}) > 0
                assert await conn.scalar(text(
                    f"SELECT count(*) FROM cloudops.{table} WHERE tenant_id = :t"),
                    {"t": nwl}) == 0
        async with app.transaction(TenantScope((), Role.SECOPS, "test:none")) as conn:
            assert await conn.scalar(text(
                "SELECT count(*) FROM cloudops.directory_principal")) == 0
        # A directory is tenant-wide: an account-restricted scope sees none of it.
        async with app.transaction(TenantScope({crl}, Role.SECOPS, "test:acct",
                                               account_ids=[uuid.uuid4()])) as conn:
            for table in ("directory_principal", "directory_role_assignment"):
                assert await conn.scalar(text(
                    f"SELECT count(*) FROM cloudops.{table}")) == 0
    finally:
        await app.dispose()
