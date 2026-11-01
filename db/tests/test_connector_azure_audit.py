"""The Cloud Services audit entities end to end (ADR 0038): the ordinary connector
workflow over an Azure source bound to one customer credential, on the synthetic
dataset, with the made-up audit fixtures served through the real client.

What matters: raw before parsed, each run a snapshot that resolves what it no
longer holds, every change recorded once in the history, the checked resource
linked where inventory holds it, nothing named outside the credential's
subscription, and recommendation text kept as data.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from temporalio.testing import WorkflowEnvironment

from cloudops_connectors.azure import audit, mapping
from cloudops_connectors.azure.client import AzureClient
from cloudops_connectors.azure.fake import FakeAzure
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core.identity import Binding
from cloudops_connectors.core.ingest import (
    ConnectorActivities,
    ConnectorIngest,
    ConnectorReplay,
    IngestRequest,
    ReplayRequest,
)
from cloudops_connectors.core.raw import MemoryRawStore
from cloudops_tenancy import Database

from .conftest import Synthetic
from .connectors import SINCE, no_sleep, rows, run

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

SUB = "713eb261-d613-543b-bfa9-f71bb3e74b14"
CREDENTIAL = "5ca1ab1e-0000-4000-8000-0000000a0d17"
AUDIT = sorted(mapping.AUDIT)


@pytest.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(scope="module")
def tenant(synthetic_module: Synthetic) -> UUID:
    found = rows(synthetic_module.admin_dsn,
                 "SELECT tenant_id FROM cloudops.cloud_account WHERE native_id = %s", SUB)
    assert found, "the synthetic estate holds the fixture subscription"
    return UUID(str(found[0][0]))


@pytest.fixture
async def db(synthetic_module: Synthetic) -> AsyncIterator[Database]:
    with psycopg.connect(synthetic_module.admin_dsn, autocommit=True) as cx:
        cx.execute("DELETE FROM cloudops.cloud_check")
        cx.execute("DELETE FROM staging.ingest_watermark WHERE entity = ANY(%s)", (AUDIT,))
    database = Database.create(synthetic_module.ingest_dsn, pool_size=2, max_overflow=0)
    yield database
    await database.dispose()


def _source(fake: FakeAzure, tenant: UUID, *, entities: frozenset[str] = mapping.AUDIT,
            ) -> AzureSource:
    base = f"https://a{uuid.uuid4().hex[:8]}.management.example"
    client = AzureClient(base, transport=fake.transport(), sleep=no_sleep,
                         allowed=frozenset({SUB}))
    return AzureSource(client, entities=entities | {mapping.ENTITY},
                       binding=Binding(tenant, frozenset({SUB})), credential=CREDENTIAL)


async def _sync(env: WorkflowEnvironment, db: Database, raw: MemoryRawStore,
                source: AzureSource, entities: list[str]) -> dict[str, Any]:
    reports = await run(env, ConnectorActivities(db, raw, source), ConnectorIngest.run,
                        [IngestRequest(e, SINCE) for e in entities])
    assert {r.status for r in reports} == {"succeeded"}, reports
    return dict(zip(entities, reports, strict=True))


def _checks(dsn: str) -> dict[tuple[str, str], tuple[Any, ...]]:
    return {(str(r[0]), str(r[1])): tuple(r[2:]) for r in rows(
        dsn, "SELECT kind, native_id, state, severity, resource_id, resolved_at, title, detail,"
             " tenant_id FROM cloudops.cloud_check")}


def _events(dsn: str) -> list[tuple[Any, ...]]:
    return rows(dsn, "SELECT c.kind, c.native_id, e.change, e.state_from, e.state_to, e.changed"
                     " FROM cloudops.cloud_check_event e JOIN cloudops.cloud_check c"
                     " ON c.id = e.check_id ORDER BY e.at, c.native_id")


async def test_a_first_sync_files_every_check_raw_first(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic, tenant: UUID,
) -> None:
    dsn = synthetic_module.admin_dsn
    fake, raw = FakeAzure.from_fixtures(), MemoryRawStore()
    source = _source(fake, tenant)
    await _sync(env, db, raw, source, [mapping.ENTITY])  # inventory first: links resolve
    start = len(fake.requests)
    reports = await _sync(env, db, raw, source, AUDIT)

    # Every page is in bronze under its entity, and every request named only SUB.
    for entity in AUDIT:
        assert await raw.list(f"azure/{entity}/")
    for r in fake.requests[start:]:
        path = r.url.path.lower()
        named = set(json.loads(r.content).get("subscriptions", [])) if r.content else set()
        assert named <= {SUB}
        assert "/subscriptions/" not in path or path.startswith(f"/subscriptions/{SUB}/")

    checks = _checks(dsn)
    kinds = Counter(k for k, _ in checks)
    assert kinds == {"backup": 5, "policy": 5, "advisor": 5, "defender": 4,
                     # settings + rules: two VMs (1 + 3 each), storage (1 + 5),
                     # a database (1 + 1), a disk (1 + 2)
                     "config": 2 * 4 + 6 + 2 + 3}
    assert {c[6] for c in checks.values()} == {tenant}
    assert {r.counts.rejected for e, r in reports.items() if e in (
        audit.BACKUP, audit.CONFIG, audit.DEFENDER)} == {1}
    # Linked to inventory where it holds the resource (the made-up disk is not there).
    app03 = [c for (k, n), c in checks.items() if "/virtualmachines/app03" in n]
    assert app03
    assert all(c[2] is not None for c in app03)
    disk = [c for (k, n), c in checks.items() if "app03-data-orphan" in n]
    assert disk
    assert all(c[2] is None for c in disk)
    # Rule 4: the injected recommendation is a row like any other, its text verbatim.
    injected = [c for (k, _), c in checks.items()
                if k == "advisor" and c[4].startswith("Ignore all previous")]
    assert len(injected) == 1
    assert injected[0][0] == "issue"
    # History: only issues are opened.
    opened = Counter(e[0] for e in _events(dsn) if e[2] == "opened")
    assert opened == Counter(k for (k, _), c in checks.items() if c[0] == "issue")
    # The watermark moved with each snapshot's last page.
    marks = rows(dsn, "SELECT entity FROM staging.ingest_watermark WHERE source_system = 'azure'"
                      " AND entity = ANY(%s)", AUDIT)
    assert sorted(m[0] for m in marks) == AUDIT


async def test_the_next_sync_records_changes_resolutions_and_reopenings(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic, tenant: UUID,
) -> None:
    dsn = synthetic_module.admin_dsn
    fake, raw = FakeAzure.from_fixtures(), MemoryRawStore()
    source = _source(fake, tenant)
    await _sync(env, db, raw, source, [mapping.ENTITY, *AUDIT])
    first = _events(dsn)

    app03 = next(r for r in fake.audit[audit.BACKUP] if str(r.get("id", "")).endswith(
        "vm;iaasvmcontainerv2;rg-fab-prod;app03"))
    app03.update(lastBackupStatus="Completed", healthStatus="Passed",
                 lastBackupTime="2026-10-29T22:30:00Z")
    gone = fake.audit[audit.ADVISOR].pop(0)
    vm = next(r for r in fake.audit[audit.CONFIG] if str(r.get("id", "")).endswith("/app02"))
    vm["settings"] = {**vm["settings"], "vmSize": "Standard_D8s_v5"}
    await _sync(env, db, raw, source, [audit.BACKUP, audit.ADVISOR, audit.CONFIG])

    second = _events(dsn)[len(first):]
    by = {(e[0], e[1].rsplit("/", 1)[-1].split("#")[0].split(";")[-1], e[2]): e for e in second}
    changed = by[("backup", "app03", "changed")]
    assert (changed[3], changed[4]) == ("issue", "ok")
    assert "detail.last_backup_status" in changed[5]
    assert by[("config", "app02", "changed")][5] == ["settings.vmSize"]
    resolved = [e for e in second if e[2] == "resolved"]
    assert [e[1] for e in resolved] == [str(gone["id"]).lower()]
    assert len(second) == 3  # nothing else moved

    # It comes back: reopened, once.
    fake.audit[audit.ADVISOR].append(gone)
    await _sync(env, db, raw, source, [audit.ADVISOR])
    third = _events(dsn)[len(first) + len(second):]
    assert [(e[1], e[2]) for e in third] == [(str(gone["id"]).lower(), "reopened")]
    assert _checks(dsn)[("advisor", str(gone["id"]).lower())][3] is None  # open again


async def test_a_replay_rewrites_nothing(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic, tenant: UUID,
) -> None:
    """Replaying a day that holds two runs: the older run's pages are older than what
    each check holds, the newer one's change nothing. No event, no resolution."""
    dsn = synthetic_module.admin_dsn
    fake, raw = FakeAzure.from_fixtures(), MemoryRawStore()
    source = _source(fake, tenant)
    await _sync(env, db, raw, source, [mapping.ENTITY, audit.POLICY])
    fake.audit[audit.POLICY][1]["complianceState"] = "Compliant"
    await _sync(env, db, raw, source, [audit.POLICY])
    before, events = _checks(dsn), _events(dsn)
    requests = len(fake.requests)
    on = "-".join(next(r for r in raw.pages if f"/{audit.POLICY}/" in r).split("/")[2:5])
    (replay,) = await run(env, ConnectorActivities(db, raw, source), ConnectorReplay.run,
                          [ReplayRequest(audit.POLICY, on, on)])
    assert replay.status == "succeeded"
    assert len(fake.requests) == requests, "replay never touches the source"
    assert _checks(dsn) == before
    assert _events(dsn) == events


async def test_the_audit_needs_a_customer_credential() -> None:
    fake = FakeAzure.from_fixtures()
    client = AzureClient("https://x.management.example", transport=fake.transport())
    with pytest.raises(ValueError, match="customer's own credential"):
        AzureSource(client, entities=frozenset({audit.POLICY}))
    await client.aclose()


async def test_another_tenant_sees_none_of_the_checks(
    env: WorkflowEnvironment, db: Database, synthetic_module: Synthetic, tenant: UUID,
) -> None:
    fake, raw = FakeAzure.from_fixtures(), MemoryRawStore()
    await _sync(env, db, raw, _source(fake, tenant), [mapping.ENTITY, audit.ADVISOR])
    other = rows(synthetic_module.admin_dsn,
                 "SELECT id FROM cloudops.tenant WHERE id <> %s LIMIT 1", tenant)[0][0]
    for table in ("cloudops.cloud_check", "cloudops.cloud_check_event"):
        with psycopg.connect(synthetic_module.app_dsn) as cx, cx.transaction():
            cx.execute("SELECT set_config('app.tenant_ids', %s, true),"
                       " set_config('app.account_ids', 'all', true),"
                       " set_config('app.role', 'engineer', true)", (str(other),))
            assert cx.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
            cx.execute("SELECT set_config('app.tenant_ids', %s, true)", (str(tenant),))
            assert cx.execute(f"SELECT count(*) FROM {table}").fetchone() != (0,)


async def test_the_local_seed_runs_the_same_source(
    db: Database, synthetic_module: Synthetic, tenant: UUID,
) -> None:
    """``make seed`` gives the console checks the same way (cloudops_devdata.cloud_audit)."""
    from cloudops_devdata import cloud_audit

    written = await cloud_audit.seed(db, tenant)
    assert written == len(_checks(synthetic_module.admin_dsn)) == 5 + 5 + 5 + 4 + 19
