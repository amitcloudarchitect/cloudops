"""The triage sweep's choice of tickets against the real schema: open incidents
in the window with no triage result, one tenant's scope at a time, none while
the tenant's AI budget is near its alert fraction (CLAUDE.md rule 3: a fixed
query decides, never a model)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from cloudops_agents.remediation import SweepRequest, TriageSweepActivities
from cloudops_gateway.gateway import Gateway, Limits
from cloudops_gateway.pricing import LOCAL_DEPLOYMENTS
from cloudops_gateway.providers import MockProvider
from cloudops_tenancy import Database

from .conftest import Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


@pytest.fixture
async def app(seeded: Seeded) -> AsyncIterator[Database]:
    db = Database.create(seeded.app_dsn, pool_size=2, max_overflow=0)
    yield db
    await db.dispose()


def _gateway(db: Database, limits: Limits | None = None) -> Gateway:
    providers = {"mock": MockProvider("mock"), "mock-secondary": MockProvider("mock-secondary")}
    return Gateway(db, providers, LOCAL_DEPLOYMENTS, limits=limits)


def _ticket(cx: psycopg.Connection, tenant: UUID, *, kind: str = "incident",
            state: str | None = "New", age: timedelta = timedelta(hours=1),
            closed: bool = False) -> str:
    number = f"INC{uuid4().hex[:10]}"
    opened = datetime.now(UTC) - age
    cx.execute(
        "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw, opened_at,"
        " closed_at, state, short_description) VALUES (%s, %s, %s, 'X', %s, %s, %s, 'disk')",
        (tenant, kind, number, opened, opened + timedelta(minutes=5) if closed else None, state))
    return number


def _triaged(cx: psycopg.Connection, tenant: UUID, number: str) -> None:
    cx.execute(
        "INSERT INTO cloudops.triage_result (tenant_id, number, route, stage, cost_currency,"
        " workflow_id) VALUES (%s, %s, 'human', 1, 'USD', %s)",
        (tenant, number, f"triage-{uuid4()}"))


async def test_the_sweep_picks_open_untriaged_incidents_in_the_window_only(
    app: Database, seeded: Seeded,
) -> None:
    a = seeded.a.id
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        wanted = _ticket(cx, a)
        triaged = _ticket(cx, a)
        _triaged(cx, a, triaged)
        closed = _ticket(cx, a, closed=True)
        resolved = _ticket(cx, a, state="Resolved")
        old = _ticket(cx, a, age=timedelta(days=8))
        task = _ticket(cx, a, kind="change_task")
    picked = await TriageSweepActivities(app, _gateway(app)).untriaged(SweepRequest(str(a), 500))
    assert wanted in picked
    assert not {triaged, closed, resolved, old, task} & set(picked)


async def test_the_sweep_sees_only_the_tenant_it_was_asked_about(
    app: Database, seeded: Seeded,
) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        theirs = _ticket(cx, seeded.b.id)
    sweep = TriageSweepActivities(app, _gateway(app))
    assert theirs not in await sweep.untriaged(SweepRequest(str(seeded.a.id), 500))
    assert theirs in await sweep.untriaged(SweepRequest(str(seeded.b.id), 500))


async def test_the_sweep_is_bounded(app: Database, seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        for _ in range(3):
            _ticket(cx, seeded.c.id)
    sweep = TriageSweepActivities(app, _gateway(app))
    assert len(await sweep.untriaged(SweepRequest(str(seeded.c.id), 2))) == 2


async def test_a_tenant_near_its_ai_budget_waits_for_a_later_run(
    app: Database, seeded: Seeded,
) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        _ticket(cx, seeded.a.id)
    spent = _gateway(app, Limits(tenant_monthly_tokens=0))
    assert await TriageSweepActivities(app, spent).untriaged(SweepRequest(str(seeded.a.id), 5)) \
        == []


async def test_the_sweep_lists_every_tenant(app: Database, seeded: Seeded) -> None:
    tenants = await TriageSweepActivities(app, _gateway(app)).triage_tenants()
    assert {str(t.id) for t in seeded.tenants} <= set(tenants)
