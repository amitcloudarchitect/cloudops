"""Triage results and reviews under RLS (migration 0008, ADR 0014): another
tenant's triage is invisible, a subscription-limited principal sees triage only
for resources in its subscriptions, and a review is append-only and says what
it changed and why."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest

from .conftest import Seeded
from .support import raw_scope


def _result(cx: psycopg.Connection, tenant: UUID, resource: UUID | None) -> UUID:
    rid = uuid4()
    cx.execute(
        "INSERT INTO cloudops.triage_result (id, tenant_id, number, resource_id, route, stage,"
        " classification, cost_currency, workflow_id)"
        " VALUES (%s, %s, %s, %s, 'human', 2, '{\"intent\": \"disk_full\"}', 'USD', %s)",
        (rid, tenant, f"INC{rid.hex[:7]}", resource, f"triage-{rid}"),
    )
    return rid


def _review(cx: psycopg.Connection, tenant: UUID, result: UUID) -> None:
    cx.execute(
        "INSERT INTO cloudops.triage_review (tenant_id, triage_result_id, decision, reviewer_oid)"
        " VALUES (%s, %s, 'accept', %s)",
        (tenant, result, uuid4()),
    )


def test_another_tenants_triage_is_invisible(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        theirs = _result(cx, seeded.b.id, seeded.b.resource_id)
        _review(cx, seeded.b.id, theirs)
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "platform_admin") as cx:
        results = cx.execute("SELECT id FROM cloudops.triage_result").fetchall()
        reviews = cx.execute("SELECT triage_result_id FROM cloudops.triage_review").fetchall()
    assert theirs not in {r[0] for r in results}
    assert theirs not in {r[0] for r in reviews}


def test_a_review_cannot_be_written_for_another_tenant(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        theirs = _result(cx, seeded.b.id, seeded.b.resource_id)
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "engineer") as cx, \
            pytest.raises(psycopg.errors.Error, match=r"row-level security|foreign key"):
        _review(cx, seeded.b.id, theirs)


def test_subscription_limited_sees_triage_only_in_its_subscriptions(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        inside = _result(cx, seeded.c.id, seeded.c.resource_id)
        outside = _result(cx, seeded.c.id, seeded.c_resource2)
        _review(cx, seeded.c.id, outside)
    limited = str(seeded.c.account_id)
    with raw_scope(seeded.app_dsn, str(seeded.c.id), "finops", account_ids=limited) as cx:
        seen = {r[0] for r in cx.execute("SELECT id FROM cloudops.triage_result")}
        reviewed = {r[0] for r in cx.execute("SELECT triage_result_id FROM cloudops.triage_review")}
    assert inside in seen
    assert outside not in seen
    assert outside not in reviewed
    with raw_scope(seeded.app_dsn, str(seeded.c.id), "finops") as cx:
        everything = {r[0] for r in cx.execute("SELECT id FROM cloudops.triage_result")}
    assert {inside, outside} <= everything


@pytest.mark.parametrize("statement", [
    "UPDATE cloudops.triage_review SET decision = 'override'",
    "DELETE FROM cloudops.triage_review",
    "UPDATE cloudops.triage_result SET route = 'ready_for_planning'",
    "DELETE FROM cloudops.triage_result",
])
def test_the_application_cannot_rewrite_history(seeded: Seeded, statement: str) -> None:
    with raw_scope(seeded.app_dsn, str(seeded.a.id), "platform_admin") as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute(statement)


@pytest.mark.parametrize(("decision", "overrides", "reason"), [
    ("override", None, "wrong intent"),
    ("override", "{}", "wrong intent"),
    ("override", '{"intent": "network"}', None),
    ("override", '{"intent": "network"}', "   "),
    ("accept", '{"intent": "network"}', None),
])
def test_an_override_says_what_changed_and_why(
    seeded: Seeded, decision: str, overrides: str | None, reason: str | None,
) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        result = _result(cx, seeded.a.id, seeded.a.resource_id)
        with pytest.raises(psycopg.errors.CheckViolation):
            cx.execute(
                "INSERT INTO cloudops.triage_review (tenant_id, triage_result_id, decision,"
                " overrides, reason, reviewer_oid) VALUES (%s, %s, %s, %s, %s, %s)",
                (seeded.a.id, result, decision, overrides, reason, uuid4()),
            )


def test_triage_review_is_granted_to_the_reviewing_roles_only(seeded: Seeded) -> None:
    with psycopg.connect(seeded.admin_dsn) as cx:
        holders = {r[0] for r in cx.execute(
            "SELECT r.role_class FROM access.role_permission p"
            " JOIN access.role_definition r ON r.id = p.role_id"
            " WHERE p.permission = 'triage.review'")}
    assert holders == {"engineer", "delivery_lead", "platform_admin"}
