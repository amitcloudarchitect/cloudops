"""The customer master at the database level (migration 0009, ADR 0015): who may
see the quarantine, onboard a tenant and propose or approve a mapping; the
two-person rule; and that a mapping never reaches a tenant outside the actor's
scope. The API tests prove the path; these prove the floor under it."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Principals, Seeded
from .support import raw_scope

pytestmark = [pytest.mark.integration, pytest.mark.isolation]


@dataclass(frozen=True)
class Mappers:
    first: UUID  # Customer master, all tenants
    second: UUID  # Customer master, all tenants
    only_a: UUID  # Customer master, tenant a only


@pytest.fixture(scope="module")
async def mappers(seeded: Seeded, principals: Principals) -> Mappers:
    m = Mappers(uuid4(), uuid4(), uuid4())
    db = Database.create(seeded.app_dsn, pool_size=1, max_overflow=0)
    try:
        async with db.transaction(TenantScope.for_resolution("setup:mappers")) as conn:
            for oid, all_tenants in ((m.first, True), (m.second, True), (m.only_a, False)):
                await conn.execute(text(
                    "SELECT access.set_principal(:a, :o, 'staff', 'mapper', 'Customer master',"
                    " :all)"),
                    {"a": principals.admin, "o": oid, "all": all_tenants})
            await conn.execute(text("SELECT access.grant_tenant(:a, :o, :t)"),
                               {"a": principals.admin, "o": m.only_a, "t": seeded.a.id})
    finally:
        await db.dispose()
    return m


@pytest.fixture
def approvers(seeded: Seeded) -> Iterator[Any]:
    """Set the policy for one test, then put the default (two) back."""

    def set_to(n: int) -> None:
        with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
            cx.execute("UPDATE access.mapping_policy SET approvers = %s", (n,))

    yield set_to
    set_to(2)


def _scope(*tenants: UUID, role: Role = Role.DELIVERY_LEAD) -> TenantScope:
    return TenantScope(list(tenants), role, "test:mapping")


async def _call(db: Database, s: TenantScope, sql: str, **params: Any) -> Any:
    async with db.transaction(s) as conn:
        return (await conn.execute(text(sql), params)).all()


def _quarantine(seeded: Seeded, alias: str, count: int = 5) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO staging.tenant_alias_quarantine (source_system, alias,"
            " occurrence_count, raw_ref) VALUES ('servicenow', %s, %s, 'servicenow/incident/x')",
            (alias, count),
        )


async def _propose(db: Database, actor: UUID, tenant: UUID, alias: str,
                   *scope_tenants: UUID) -> UUID:
    rows = await _call(db, _scope(*(scope_tenants or (tenant,))),
                       "SELECT access.propose_alias(:a, 'servicenow', :al, :t, 'test')",
                       a=actor, al=alias, t=tenant)
    return UUID(str(rows[0][0]))


async def _decide(db: Database, actor: UUID, proposal: UUID, decision: str, *tenants: UUID,
                  reason: str | None = None) -> None:
    await _call(db, _scope(*tenants), "SELECT access.decide_alias(:a, :p, :d, :r)",
                a=actor, p=proposal, d=decision, r=reason)


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None)


# --- the tables -----------------------------------------------------------------


async def test_another_tenants_proposals_and_decisions_are_invisible(
    app_db: Database, seeded: Seeded, mappers: Mappers
) -> None:
    alias = f"Bravo Proposal {uuid4().hex[:6]}"
    proposal = await _propose(app_db, mappers.first, seeded.b.id, alias)
    await _decide(app_db, mappers.first, proposal, "reject", seeded.b.id, reason="test")
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "platform_admin") as cx:
        proposals = {r[0] for r in cx.execute("SELECT id FROM cloudops.alias_proposal")}
        decisions = {r[0] for r in cx.execute("SELECT proposal_id FROM cloudops.alias_decision")}
    assert proposal not in proposals
    assert proposal not in decisions
    with raw_scope(seeded.app_dsn, str(seeded.b.id), "delivery_lead") as cx:
        assert cx.execute("SELECT count(*) FROM cloudops.alias_proposal WHERE id = %s",
                          (proposal,)).fetchone() == (1,)


@pytest.mark.parametrize("statement", [
    "INSERT INTO cloudops.alias_proposal (tenant_id, source_system, alias, proposed_by)"
    " VALUES ('{a}', 'servicenow', 'Direct', gen_random_uuid())",
    "UPDATE cloudops.alias_proposal SET alias = 'x'",
    "DELETE FROM cloudops.alias_proposal",
    "UPDATE cloudops.alias_decision SET decision = 'approve'",
    "DELETE FROM cloudops.alias_decision",
    "INSERT INTO cloudops.tenant_alias (tenant_id, source_system, alias)"
    " VALUES ('{a}', 'servicenow', 'Direct')",
    "UPDATE cloudops.tenant_alias SET tenant_id = '{a}'",
    "INSERT INTO cloudops.tenant (canonical_name, short_code) VALUES ('Direct', 'DIRECT')",
    "UPDATE cloudops.tenant SET canonical_name = 'x'",
    "SELECT * FROM staging.tenant_alias_quarantine",
])
def test_the_application_reaches_the_customer_master_only_through_functions(
    seeded: Seeded, statement: str
) -> None:
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "platform_admin") as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute(statement.format(a=seeded.a.id))


def test_the_owner_cannot_rewrite_a_proposal_or_decision_either(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        row = cx.execute(
            "INSERT INTO cloudops.alias_proposal (tenant_id, source_system, alias, proposed_by)"
            " VALUES (%s, 'servicenow', %s, gen_random_uuid()) RETURNING id",
            (seeded.a.id, f"Owner Probe {uuid4().hex[:6]}")).fetchone()
        assert row is not None
        cx.execute("INSERT INTO cloudops.alias_decision (proposal_id, tenant_id, decision,"
                   " reason, decided_by) VALUES (%s, %s, 'reject', 'test', gen_random_uuid())",
                   (row[0], seeded.a.id))
        for statement in ("UPDATE cloudops.alias_decision SET reason = 'x'",
                          "DELETE FROM cloudops.alias_decision",
                          "UPDATE cloudops.alias_proposal SET alias = 'x'",
                          "DELETE FROM cloudops.alias_proposal"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cx.execute(statement + " WHERE " + ("proposal_id" if "decision" in statement
                                                     else "id") + " = %s", (row[0],))


def test_a_customer_scope_reads_no_proposal_even_in_its_own_tenant(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO cloudops.alias_proposal (tenant_id, source_system, alias, proposed_by)"
            " VALUES (%s, 'servicenow', %s, gen_random_uuid())",
            (seeded.a.id, f"Customer Hidden {uuid4().hex[:6]}"))
    for role, visible in (("customer", False), ("delivery_lead", True)):
        with raw_scope(seeded.app_dsn, str(seeded.a.id), role) as cx:
            row = cx.execute("SELECT count(*) FROM cloudops.alias_proposal").fetchone()
        assert (row is not None and row[0] > 0) is visible, role


# --- who may use it ----------------------------------------------------------------


async def test_only_mapping_holders_see_the_quarantine_and_never_its_payload_refs(
    app_db: Database, seeded: Seeded, principals: Principals, mappers: Mappers
) -> None:
    alias = f"Quarantined Corp {uuid4().hex[:6]}"
    _quarantine(seeded, alias, 7)
    every = [t.id for t in seeded.tenants]
    for refused, role in ((principals.engineer_a, Role.ENGINEER),
                          (principals.finops_all, Role.FINOPS),
                          (principals.customer_a, Role.CUSTOMER)):
        with pytest.raises(Exception, match="not permitted") as caught:
            await _call(app_db, _scope(*every, role=role),
                        "SELECT * FROM access.alias_quarantine(:a)", a=refused)
        assert _sqlstate(caught.value) == "42501"
    async with app_db.transaction(_scope(*every)) as conn:
        result = await conn.execute(text("SELECT * FROM access.alias_quarantine(:a)"),
                                    {"a": mappers.first})
        columns = list(result.keys())
        rows = [dict(zip(columns, r, strict=True)) for r in result]
    assert "raw_ref" not in columns
    mine = [r for r in rows if r["alias"] == alias]
    assert mine
    assert mine[0]["occurrence_count"] == 7
    counts = [r["occurrence_count"] for r in rows]
    assert counts == sorted(counts, reverse=True)


async def test_the_suggestion_never_names_a_tenant_outside_scope(
    app_db: Database, seeded: Seeded, mappers: Mappers
) -> None:
    alias = f"{seeded.b.name.upper()} Holdings Europe"
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(
            "INSERT INTO staging.tenant_alias_quarantine (source_system, alias,"
            " suggested_tenant_id) VALUES ('servicenow', %s, %s)", (alias, seeded.b.id))

    async def suggestion(*tenants: UUID) -> Any:
        rows = await _call(app_db, _scope(*tenants),
                           "SELECT suggested_tenant_id FROM access.alias_quarantine(:a)"
                           " WHERE alias = :al", a=mappers.first, al=alias)
        return rows[0][0]

    assert await suggestion(seeded.b.id) == seeded.b.id
    assert await suggestion(seeded.a.id) is None


def test_mapping_manage_cannot_be_given_to_a_customer_role(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege, match="customer role"):
        cx.execute(
            "INSERT INTO access.role_permission (role_id, permission)"
            " SELECT id, 'mapping.manage' FROM access.role_definition"
            " WHERE name = 'Customer viewer'")


def test_the_customer_master_role_and_its_holders(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        holders = {r[0] for r in cx.execute(
            "SELECT r.name FROM access.role_permission p"
            " JOIN access.role_definition r ON r.id = p.role_id"
            " WHERE p.permission = 'mapping.manage' AND r.is_system")}
        perms = {r[0] for r in cx.execute(
            "SELECT p.permission FROM access.role_permission p"
            " JOIN access.role_definition r ON r.id = p.role_id"
            " WHERE r.name = 'Customer master'")}
        cls = cx.execute("SELECT role_class FROM access.role_definition"
                         " WHERE name = 'Customer master'").fetchone()
    assert holders == {"Customer master", "Platform administrator"}
    assert perms == {"mapping.manage", "tickets.read", "inventory.read"}
    assert cls == ("delivery_lead",)


# --- onboarding ----------------------------------------------------------------------


async def test_onboarding_creates_a_tenant_and_logs_it(
    app_db: Database, seeded: Seeded, principals: Principals, mappers: Mappers
) -> None:
    code = f"T{uuid4().hex[:6].upper()}"
    rows = await _call(app_db, _scope(seeded.a.id),
                       "SELECT access.onboard_tenant(:a, :n, :c)",
                       a=mappers.only_a, n=f"Tenant {code}", c=code)
    tenant = UUID(str(rows[0][0]))
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT short_code FROM cloudops.tenant WHERE id = %s",
                          (tenant,)).fetchone() == (code,)
        logged = cx.execute("SELECT actor FROM access.change_log WHERE action = 'onboard_tenant'"
                            " AND detail->>'tenant_id' = %s", (str(tenant),)).fetchone()
    assert logged == (str(mappers.only_a),)
    # Again with the same code: refused, and nothing says whose it is.
    with pytest.raises(Exception, match="exists") as caught:
        await _call(app_db, _scope(seeded.a.id), "SELECT access.onboard_tenant(:a, :n, :c)",
                    a=mappers.only_a, n="Another name", c=code)
    assert _sqlstate(caught.value) == "CMDUP"
    for name, bad in (("x", "lower"), ("x", "A"), ("  ", "OKAY")):
        with pytest.raises(Exception, match="short code") as caught:
            await _call(app_db, _scope(seeded.a.id), "SELECT access.onboard_tenant(:a, :n, :c)",
                        a=mappers.only_a, n=name, c=bad)
        assert _sqlstate(caught.value) == "23514"
    with pytest.raises(Exception, match="not permitted"):
        await _call(app_db, _scope(seeded.a.id, role=Role.ENGINEER),
                    "SELECT access.onboard_tenant(:a, 'Nope', 'NOPE')", a=principals.engineer_a)


# --- proposing and approving ----------------------------------------------------------


async def test_two_people_are_needed_unless_configured_otherwise(
    app_db: Database, seeded: Seeded, mappers: Mappers, approvers: Any
) -> None:
    a = seeded.a.id
    alias = f"Alpha Two Person {uuid4().hex[:6]}"
    _quarantine(seeded, alias, 3)
    proposal = await _propose(app_db, mappers.first, a, alias)
    with pytest.raises(Exception, match="second person") as caught:
        await _decide(app_db, mappers.first, proposal, "approve", a)
    assert _sqlstate(caught.value) == "CMTWO"
    await _decide(app_db, mappers.second, proposal, "approve", a)

    with psycopg.connect(seeded.admin_dsn) as cx:
        mapped = cx.execute("SELECT tenant_id, confirmed_by FROM cloudops.tenant_alias"
                            " WHERE source_system = 'servicenow' AND alias = %s",
                            (alias,)).fetchone()
        held = cx.execute("SELECT 1 FROM staging.tenant_alias_quarantine WHERE alias = %s",
                          (alias,)).fetchone()
        decision = cx.execute("SELECT decided_by, held_occurrences FROM cloudops.alias_decision"
                              " WHERE proposal_id = %s", (proposal,)).fetchone()
    assert mapped == (a, str(mappers.second))
    assert held is None
    assert decision == (mappers.second, 3)

    # Dev: one person may do both, when the configuration says so.
    approvers(1)
    solo = f"Alpha One Person {uuid4().hex[:6]}"
    proposal = await _propose(app_db, mappers.first, a, solo)
    await _decide(app_db, mappers.first, proposal, "approve", a)


async def test_the_proposer_may_withdraw_and_a_rejection_needs_a_reason(
    app_db: Database, seeded: Seeded, mappers: Mappers
) -> None:
    a = seeded.a.id
    proposal = await _propose(app_db, mappers.first, a, f"Withdrawn {uuid4().hex[:6]}")
    with pytest.raises(Exception, match="check constraint") as caught:
        await _decide(app_db, mappers.first, proposal, "reject", a)
    assert _sqlstate(caught.value) == "23514"
    await _decide(app_db, mappers.first, proposal, "reject", a, reason="wrong customer")
    # A decision is final.
    with pytest.raises(Exception, match="already decided"):
        await _decide(app_db, mappers.second, proposal, "approve", a)


async def test_an_approved_mapping_resolves_exactly_for_the_connector(
    app_db: Database, ingest_db: Database, seeded: Seeded, mappers: Mappers
) -> None:
    alias = f"Charlie Resolves {uuid4().hex[:6]}"
    proposal = await _propose(app_db, mappers.first, seeded.c.id, alias)
    await _decide(app_db, mappers.second, proposal, "approve", seeded.c.id)
    rows = await _call(ingest_db, TenantScope((), Role.INGEST, "connector:test"),
                       "SELECT tenant_id, outcome FROM cloudops.resolve_tenant_alias("
                       "'servicenow', :al)", al=alias)
    assert rows == [(seeded.c.id, "exact")]


async def test_a_name_is_mapped_once(
    app_db: Database, seeded: Seeded, mappers: Mappers
) -> None:
    alias = f"Mapped Once {uuid4().hex[:6]}"
    first = await _propose(app_db, mappers.first, seeded.a.id, alias)
    # Pending: a second proposal for the same name waits.
    with pytest.raises(Exception, match="already mapped or awaiting") as caught:
        await _propose(app_db, mappers.second, seeded.a.id, alias)
    assert _sqlstate(caught.value) == "CMDUP"
    # Proposed for b by someone who cannot see a's pending proposal, then a's approved:
    other = await _propose(app_db, mappers.first, seeded.b.id, alias)
    await _decide(app_db, mappers.second, first, "approve", seeded.a.id)
    with pytest.raises(Exception, match="already mapped") as caught:
        await _decide(app_db, mappers.second, other, "approve", seeded.b.id)
    assert _sqlstate(caught.value) == "CMDUP"


async def test_a_mapping_never_targets_a_tenant_outside_the_actors_scope(
    app_db: Database, seeded: Seeded, mappers: Mappers
) -> None:
    a, b = seeded.a.id, seeded.b.id
    # Assigned a only: b is refused even when the transaction scope names it.
    with pytest.raises(Exception, match="tenant not found") as caught:
        await _propose(app_db, mappers.only_a, b, f"Out Of Scope {uuid4().hex[:6]}", a, b)
    assert _sqlstate(caught.value) == "CMSCP"
    # Assigned everything, but this request's scope is a only: b is refused too.
    with pytest.raises(Exception, match="tenant not found"):
        await _propose(app_db, mappers.first, b, f"Out Of Scope {uuid4().hex[:6]}", a)
    # A tenant that does not exist reads the same.
    with pytest.raises(Exception, match="tenant not found"):
        await _propose(app_db, mappers.first, uuid4(), "Nobody", a, b)
    # A proposal for b cannot be approved by someone whose scope is a.
    theirs = await _propose(app_db, mappers.first, b, f"Bravo Only {uuid4().hex[:6]}")
    with pytest.raises(Exception, match="proposal not found") as caught:
        await _decide(app_db, mappers.only_a, theirs, "approve", a)
    assert _sqlstate(caught.value) == "P0002"
    with pytest.raises(Exception, match="tenant not found"):
        await _decide(app_db, mappers.only_a, theirs, "approve", a, b)
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT count(*) FROM cloudops.alias_decision WHERE proposal_id = %s",
                          (theirs,)).fetchone() == (0,)


async def test_a_customer_can_reach_none_of_it(
    app_db: Database, seeded: Seeded, principals: Principals, mappers: Mappers
) -> None:
    a = seeded.a.id
    pending = await _propose(app_db, mappers.first, a, f"Customer Probe {uuid4().hex[:6]}")
    customer = _scope(a, role=Role.CUSTOMER)
    for sql in (
        "SELECT * FROM access.alias_quarantine(:c)",
        "SELECT access.onboard_tenant(:c, 'Customer Co', 'CUSTCO')",
        "SELECT access.propose_alias(:c, 'servicenow', 'Mine', :t, NULL)",
        "SELECT access.decide_alias(:c, :p, 'approve', NULL)",
    ):
        with pytest.raises(Exception, match=r"not permitted|proposal not found"):
            await _call(app_db, customer, sql, c=principals.customer_a, t=a, p=pending)
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT count(*) FROM cloudops.alias_decision WHERE proposal_id = %s",
                          (pending,)).fetchone() == (0,)


def test_mapping_changes_are_logged(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        actions = {r[0] for r in cx.execute("SELECT DISTINCT action FROM access.change_log")}
    assert {"onboard_tenant", "propose_alias", "approve_alias", "reject_alias"} <= actions


# --- re-process requests (ADR 0015) -----------------------------------------------------


async def _approved_with_held(app_db: Database, seeded: Seeded, mappers: Mappers,
                              tenant: UUID) -> UUID:
    alias = f"Held Then Mapped {uuid4().hex[:6]}"
    _quarantine(seeded, alias, 6)
    proposal = await _propose(app_db, mappers.first, tenant, alias)
    await _decide(app_db, mappers.second, proposal, "approve", tenant)
    return proposal


async def _request(app_db: Database, actor: UUID, proposal: UUID, *tenants: UUID) -> UUID:
    rows = await _call(app_db, _scope(*tenants), "SELECT access.request_reprocess(:a, :p)",
                       a=actor, p=proposal)
    return UUID(str(rows[0][0]))


async def test_a_reprocess_is_requested_once_for_an_approved_name_that_held_records(
    app_db: Database, seeded: Seeded, mappers: Mappers
) -> None:
    a = seeded.a.id
    proposal = await _approved_with_held(app_db, seeded, mappers, a)
    request = await _request(app_db, mappers.first, proposal, a)
    with psycopg.connect(seeded.admin_dsn) as cx:
        row = cx.execute("SELECT status, tenant_id, window_end - window_start >= 1"
                         " FROM cloudops.alias_reprocess WHERE id = %s", (request,)).fetchone()
        logged = cx.execute("SELECT 1 FROM access.change_log WHERE action = 'request_reprocess'"
                            " AND detail->>'request_id' = %s", (str(request),)).fetchone()
    assert row == ("requested", a, True)
    assert logged == (1,)
    with pytest.raises(Exception, match="already waiting or running") as caught:
        await _request(app_db, mappers.second, proposal, a)
    assert _sqlstate(caught.value) == "CMDUP"


@pytest.mark.parametrize(("case", "sqlstate"), [
    ("rejected", "CMNOT"), ("pending", "CMNOT"), ("nothing_held", "CMNIL"),
])
async def test_a_reprocess_needs_an_approval_that_held_something(
    app_db: Database, seeded: Seeded, mappers: Mappers, case: str, sqlstate: str
) -> None:
    a = seeded.a.id
    proposal = await _propose(app_db, mappers.first, a, f"Not Reprocessable {uuid4().hex[:6]}")
    if case == "rejected":
        await _decide(app_db, mappers.first, proposal, "reject", a, reason="no")
    elif case == "nothing_held":
        await _decide(app_db, mappers.second, proposal, "approve", a)
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the SQLSTATE is the assertion
        await _request(app_db, mappers.first, proposal, a)
    assert _sqlstate(caught.value) == sqlstate


async def test_a_reprocess_never_reaches_outside_scope_or_a_customer(
    app_db: Database, seeded: Seeded, principals: Principals, mappers: Mappers
) -> None:
    b = seeded.b.id
    proposal = await _approved_with_held(app_db, seeded, mappers, b)
    with pytest.raises(Exception, match="proposal not found"):
        await _request(app_db, mappers.only_a, proposal, seeded.a.id)
    with pytest.raises(Exception, match="tenant not found"):
        await _request(app_db, mappers.only_a, proposal, seeded.a.id, b)
    with pytest.raises(Exception, match="not permitted"):
        await _call(app_db, _scope(b, role=Role.CUSTOMER),
                    "SELECT access.request_reprocess(:c, :p)",
                    c=principals.customer_a, p=proposal)


@pytest.mark.parametrize(("login", "sql"), [
    ("app", "SELECT * FROM cloudops.claim_reprocess('servicenow')"),
    ("app", "SELECT cloudops.finish_reprocess(gen_random_uuid(), 'failed', NULL, 0, NULL)"),
    ("ingest", "SELECT access.request_reprocess(gen_random_uuid(), gen_random_uuid())"),
])
def test_each_side_calls_only_its_own_functions(seeded: Seeded, login: str, sql: str) -> None:
    dsn = seeded.app_dsn if login == "app" else seeded.ingest_dsn
    with raw_scope(dsn, str(seeded.a.id), "platform_admin" if login == "app" else "ingest") \
            as cx, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute(sql)


async def test_the_worker_claims_without_a_scope_and_finishes_under_the_tenants(
    app_db: Database, ingest_db: Database, seeded: Seeded, mappers: Mappers
) -> None:
    a = seeded.a.id
    request = await _request(app_db, mappers.first,
                             await _approved_with_held(app_db, seeded, mappers, a), a)
    unscoped = TenantScope((), Role.INGEST, "connector:test")
    # With no scope the worker sees no request rows, only what the claim hands it.
    assert await _call(ingest_db, unscoped, "SELECT id FROM cloudops.alias_reprocess") == []
    claimed: list[Any] = []
    for _ in range(20):
        rows = await _call(ingest_db, unscoped,
                           "SELECT id, tenant_id FROM cloudops.claim_reprocess('servicenow')")
        if not rows:
            break
        claimed += rows
        if rows[0][0] == request:
            break
    assert (request, a) in claimed
    with pytest.raises(Exception, match="no running re-process"):
        await _call(ingest_db, TenantScope([seeded.b.id], Role.INGEST, "connector:test"),
                    "SELECT cloudops.finish_reprocess(:id, 'succeeded', 3, 2, NULL)", id=request)
    await _call(ingest_db, TenantScope([a], Role.INGEST, "connector:test"),
                "SELECT cloudops.finish_reprocess(:id, 'succeeded', 3, 2, NULL)", id=request)
    with psycopg.connect(seeded.admin_dsn) as cx:
        assert cx.execute("SELECT status, moved FROM cloudops.alias_reprocess WHERE id = %s",
                          (request,)).fetchone() == ("succeeded", 3)
