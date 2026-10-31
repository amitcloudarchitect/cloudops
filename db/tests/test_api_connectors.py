"""The connector health API against the real schema (step 15): staff with
``connectors.read`` see every connector's band, figures and drift; only
``connectors.manage`` acknowledges drift; a customer never reaches any of it,
not even with the permission granted to its role. Responses name no tenant.

Each test schedules connectors of its own (a unique source name), so the shared
database's other rows never decide an assertion."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from cloudops_tenancy import Database, Role, TenantScope

from .conftest import Api, Principals, Seeded

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

HEALTH = "/v1/connectors/health"
DRIFT = "/v1/connectors/drift"
ACK = "/v1/connectors/drift/acknowledge"


def _source() -> str:
    return f"t_{uuid4().hex[:12]}"


async def _send(api: Api, method: str, path: str, oid: UUID, body: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {api.token(oid)}"}
    if method == "GET":
        return await api.client.get(path, headers=headers)
    return await api.client.post(path, json=body, headers=headers)


def _sql(seeded: Seeded, statement: str, *params: Any) -> None:
    with psycopg.connect(seeded.admin_dsn, autocommit=True) as cx:
        cx.execute(statement, params)


def _schedule(seeded: Seeded, source: str, entity: str, every: str) -> None:
    _sql(seeded, "INSERT INTO staging.connector_schedule (source_system, entity, every)"
                 " VALUES (%s, %s, %s::interval)", source, entity, every)


def _run(seeded: Seeded, source: str, entity: str, ago: timedelta, status: str = "succeeded",
         *, records: int = 10, rejected: int = 0, quarantined: int = 0,
         mode: str = "incremental") -> UUID:
    run_id = uuid4()
    _sql(seeded,
         "INSERT INTO staging.ingest_run (id, source_system, entity, mode, started_at,"
         " finished_at, status, records, written, rejected, quarantined, lag_seconds)"
         " VALUES (%s, %s, %s, %s, now() - %s, CASE WHEN %s = 'running' THEN NULL"
         " ELSE now() - %s + interval '5 seconds' END, %s, %s, %s, %s, %s, 42)",
         run_id, source, entity, mode, ago, status, ago, status, records,
         records - rejected - quarantined, rejected, quarantined)
    return run_id


def _drift(seeded: Seeded, source: str, entity: str, field: str, kind: str = "added") -> None:
    _sql(seeded,
         "INSERT INTO staging.schema_field (source_system, entity, field, drift, drift_at,"
         " drift_count, raw_ref) VALUES (%s, %s, %s, %s, now(), 3, 'bronze/secret-ref')",
         source, entity, field, kind)


def _row(body: dict[str, Any], source: str, entity: str) -> dict[str, Any]:
    (row,) = [i for i in body["items"] if i["source_system"] == source and i["entity"] == entity]
    found: dict[str, Any] = row
    return found


@pytest.fixture(scope="module")
async def customer_with_permission(seeded: Seeded, principals: Principals) -> UUID:
    """A customer whose (customer-class) role was granted connectors.read and
    connectors.manage: the API and the database must refuse it anyway."""
    oid = uuid4()
    role = f"Customer probe {uuid4().hex[:6]}"
    db = Database.create(seeded.app_dsn, pool_size=1, max_overflow=0)
    try:
        async with db.transaction(TenantScope.for_resolution("setup:conn")) as conn:
            act = {"a": principals.admin}
            await conn.execute(text("SELECT access.define_role(:a, :r, 'customer', 'probe')"),
                               {**act, "r": role})
            for permission in ("connectors.read", "connectors.manage"):
                await conn.execute(text("SELECT access.grant_permission(:a, :r, :p)"),
                                   {**act, "r": role, "p": permission})
            await conn.execute(text(
                "SELECT access.set_principal(:a, :o, 'customer', 'probe', :r, false)"),
                {**act, "o": oid, "r": role})
            await conn.execute(text("SELECT access.grant_tenant(:a, :o, :t)"),
                               {**act, "o": oid, "t": seeded.a.id})
    finally:
        await db.dispose()
    return oid


# --- who reaches it ------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), [
    ("GET", HEALTH, None),
    ("GET", DRIFT, None),
    ("POST", ACK, {"source": "servicenow", "entity": "incident", "field": "x"}),
])
async def test_customers_and_roles_without_the_permission_are_refused(
    api: Api, principals: Principals, customer_with_permission: UUID,
    method: str, path: str, body: Any,
) -> None:
    for oid in (principals.customer_a, principals.customer_c_limited, principals.finops_all,
                principals.tickets_only, customer_with_permission):
        response = await _send(api, method, path, oid, body)
        assert response.status_code == 403, (oid, path, response.text)
        assert response.json()["error"]["code"] == "forbidden"


async def test_the_database_refuses_a_customer_too(
    seeded: Seeded, customer_with_permission: UUID,
) -> None:
    """The functions check again: holding the permission is not enough for a customer."""
    db = Database.create(seeded.app_dsn, pool_size=1, max_overflow=0)
    try:
        for fn in ("connector_health(:o)", "schema_drift(:o)",
                   "acknowledge_drift(:o, 'servicenow', 'incident', 'x')"):
            with pytest.raises(Exception, match="not permitted"):
                async with db.transaction(TenantScope([seeded.a.id], Role.ENGINEER,
                                                      "probe")) as conn:
                    await conn.execute(text(f"SELECT * FROM access.{fn}"),
                                       {"o": customer_with_permission})
    finally:
        await db.dispose()


async def test_an_engineer_reads_health_and_drift_but_cannot_acknowledge(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    source = _source()
    _schedule(seeded, source, "item", "5 minutes")
    _drift(seeded, source, "item", "newField")
    health = await _send(api, "GET", HEALTH, principals.engineer_a)
    assert health.status_code == 200, health.text
    assert _row(health.json(), source, "item")["drift_open"]["value"] == 1
    drift = await _send(api, "GET", DRIFT, principals.engineer_a)
    assert drift.status_code == 200, drift.text
    assert any(d["source_system"] == source for d in drift.json()["items"])
    refused = await _send(api, "POST", ACK, principals.engineer_a,
                          {"source": source, "entity": "item", "field": "newField"})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    still = (await _send(api, "GET", DRIFT, principals.engineer_a)).json()
    assert any(d["source_system"] == source for d in still["items"])


# --- the band ------------------------------------------------------------------------


async def test_bands_from_the_run_log(api: Api, principals: Principals, seeded: Seeded) -> None:
    source = _source()
    for entity in ("fresh", "late", "outage", "never", "failing", "nosuccess", "replayonly"):
        _schedule(seeded, source, entity, "5 minutes")
    _run(seeded, source, "fresh", timedelta(minutes=2))
    _run(seeded, source, "late", timedelta(minutes=8))  # 1.6 intervals
    _run(seeded, source, "outage", timedelta(minutes=16))  # 3.2 intervals: the P2 page
    _run(seeded, source, "failing", timedelta(minutes=1), records=20, rejected=2,
         quarantined=3)
    _run(seeded, source, "failing", timedelta(minutes=3), "failed", records=0)
    _run(seeded, source, "failing", timedelta(minutes=4), "dead_lettered", records=0)
    _run(seeded, source, "nosuccess", timedelta(minutes=1), "failed", records=0)
    # A replay is not the schedule: it never makes a connector look on time.
    _run(seeded, source, "replayonly", timedelta(minutes=1), mode="replay")

    response = await _send(api, "GET", HEALTH, principals.admin)
    assert response.status_code == 200, response.text
    body = response.json()

    fresh = _row(body, source, "fresh")
    assert (fresh["band"], fresh["timeliness"], fresh["reasons"]) == ("healthy", "on_time", [])
    assert fresh["expected_every"]["value"] == 300
    assert fresh["expected_every"]["unit"] == "seconds"
    assert 100 <= fresh["last_success_age"]["value"] <= 200
    assert fresh["last_success_age"]["source_system"] == "cloudops_ingest"
    assert fresh["failed_run_rate_24h"]["value"] == "0.0000"
    assert fresh["last_lag"]["value"] == 42

    late = _row(body, source, "late")
    assert (late["band"], late["timeliness"], late["reasons"]) == ("at-risk", "late", ["late"])
    assert late["intervals_since_success"]["unit"] == "intervals"

    outage = _row(body, source, "outage")
    assert (outage["band"], outage["timeliness"]) == ("critical", "outage")

    never = _row(body, source, "never")
    assert (never["band"], never["timeliness"], never["reasons"]) == (
        "unknown", "never_run", ["never_run"])
    assert never["last_success_age"] is None
    assert never["failed_run_rate_24h"] is None  # nothing to divide by: absent, not zero
    assert never["runs_24h"]["value"] == 0

    failing = _row(body, source, "failing")
    assert failing["band"] == "at-risk"  # two of three runs failed: at least at risk
    assert failing["timeliness"] == "on_time"
    assert set(failing["reasons"]) == {"failing", "failed_runs", "rejected_records"}
    assert failing["runs_24h"]["value"] == 3
    assert failing["failed_runs_24h"]["value"] == 2
    assert failing["failed_run_rate_24h"]["value"] == "0.6667"
    assert failing["failed_run_rate_24h"]["unit"] == "ratio"
    assert failing["rejected_record_rate_24h"]["value"] == "0.1000"
    assert failing["quarantined_24h"]["value"] == 3

    nosuccess = _row(body, source, "nosuccess")
    assert (nosuccess["band"], nosuccess["timeliness"]) == ("critical", "outage")
    assert "no_success" in nosuccess["reasons"]

    replay = _row(body, source, "replayonly")
    assert replay["timeliness"] == "never_run"

    # Worst first; the summary counts agree with the rows.
    ranks = {"critical": 3, "at-risk": 2, "watch": 1, "healthy": 0, "unknown": -1}
    order = [ranks[i["band"]] for i in body["items"]]
    assert order == sorted(order, reverse=True)
    assert body["outages"]["value"] == sum(1 for i in body["items"] if i["band"] == "critical")
    assert body["never_run"]["value"] == sum(1 for i in body["items"] if i["band"] == "unknown")
    assert body["rules"]["outage_after_intervals"]["value"] == 3
    assert body["rules"]["late_after_intervals"]["value"] == 1
    assert body["rules"]["failing_run_rate"]["value"] == "0.5"


async def test_the_schedule_seeded_by_the_migration_is_listed(
    api: Api, principals: Principals,
) -> None:
    body = (await _send(api, "GET", HEALTH, principals.engineer_a)).json()
    pairs = {(i["source_system"], i["entity"]) for i in body["items"]}
    assert {("servicenow", "incident"), ("qualys", "host_detection"), ("aws", "cur"),
            ("entra", "role_assignment"), ("telemetry", "metrics")} <= pairs


async def test_quarantine_depth_follows_the_callers_scope(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    """Resource quarantine is tenant data: an engineer of tenant a does not count
    tenant b's. Alias quarantine has no tenant and is counted for everyone."""
    source = _source()
    _schedule(seeded, source, "host", "1 day")
    _sql(seeded, "INSERT INTO staging.resource_quarantine (tenant_id, source_system,"
                 " external_key) VALUES (%s, %s, 'b-host-1'), (%s, %s, 'b-host-2')",
         seeded.b.id, source, seeded.b.id, source)
    _sql(seeded, "INSERT INTO staging.tenant_alias_quarantine (source_system, alias,"
                 " occurrence_count, raw_ref) VALUES (%s, %s, 5, 'x')", source, f"Probe {source}")
    admin = _row((await _send(api, "GET", HEALTH, principals.admin)).json(), source, "host")
    engineer = _row((await _send(api, "GET", HEALTH, principals.engineer_a)).json(),
                    source, "host")
    assert admin["resource_quarantine_depth"]["value"] == 2
    assert engineer["resource_quarantine_depth"]["value"] == 0
    assert admin["alias_quarantine_depth"]["value"] == 1
    assert engineer["alias_quarantine_depth"]["value"] == 1


# --- acknowledging drift -------------------------------------------------------------


async def test_admin_acknowledges_drift_and_it_is_gone(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    source = _source()
    _schedule(seeded, source, "item", "1 hour")
    _run(seeded, source, "item", timedelta(minutes=5))
    _drift(seeded, source, "item", "u_new_field")
    _drift(seeded, source, "item", "short_description", "missing")

    before = _row((await _send(api, "GET", HEALTH, principals.admin)).json(), source, "item")
    assert before["drift_open"]["value"] == 2
    assert before["band"] == "watch"
    assert "schema_drift" in before["reasons"]

    drift = (await _send(api, "GET", DRIFT, principals.admin)).json()
    mine = [d for d in drift["items"] if d["source_system"] == source]
    assert {(d["field"], d["drift"]) for d in mine} == {
        ("u_new_field", "added"), ("short_description", "missing")}
    assert all(d["occurrences"]["value"] == 3 for d in mine)
    assert "bronze/secret-ref" not in str(drift)  # never the payload reference

    ack = await _send(api, "POST", ACK, principals.admin,
                      {"source": source, "entity": "item", "field": "u_new_field"})
    assert ack.status_code == 204, ack.text
    assert ack.content == b""
    again = await _send(api, "POST", ACK, principals.admin,
                        {"source": source, "entity": "item", "field": "u_new_field"})
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "not_found"
    unknown = await _send(api, "POST", ACK, principals.admin,
                          {"source": source, "entity": "item", "field": "never_seen"})
    assert unknown.status_code == 404

    after = (await _send(api, "GET", DRIFT, principals.admin)).json()
    assert {d["field"] for d in after["items"] if d["source_system"] == source} == {
        "short_description"}
    row = _row((await _send(api, "GET", HEALTH, principals.admin)).json(), source, "item")
    assert row["drift_open"]["value"] == 1

    with psycopg.connect(seeded.admin_dsn) as cx:
        by = cx.execute("SELECT acknowledged_by FROM staging.schema_field WHERE source_system"
                        " = %s AND field = 'u_new_field'", (source,)).fetchone()
    assert by == (principals.admin,)


@pytest.mark.parametrize("body", [
    {"source": "Not Valid", "entity": "item", "field": "x"},
    {"source": "servicenow", "entity": "item", "field": ""},
    {"source": "servicenow", "entity": "item"},
    {"source": "servicenow", "entity": "item", "field": "x", "extra": 1},
])
async def test_a_malformed_acknowledgement_is_invalid(
    api: Api, principals: Principals, body: dict[str, Any],
) -> None:
    response = await _send(api, "POST", ACK, principals.admin, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


# --- what the responses never carry --------------------------------------------------


async def test_responses_name_no_tenant_and_no_customer(
    api: Api, principals: Principals, seeded: Seeded,
) -> None:
    source = _source()
    _schedule(seeded, source, "item", "5 minutes")
    _run(seeded, source, "item", timedelta(minutes=1), records=5, quarantined=2)
    _sql(seeded, "INSERT INTO staging.resource_quarantine (tenant_id, source_system,"
                 " external_key, name) VALUES (%s, %s, 'k', %s)",
         seeded.a.id, source, seeded.a.name)
    for oid in (principals.admin, principals.engineer_a):
        for path in (HEALTH, DRIFT):
            response = await _send(api, "GET", path, oid)
            assert response.status_code == 200, response.text
            for tenant in seeded.tenants:
                assert str(tenant.id) not in response.text
                assert tenant.name not in response.text
                assert tenant.account_native_id not in response.text
    failed = await _send(api, "POST", ACK, principals.admin,
                         {"source": source, "entity": "item", "field": seeded.a.name[:200]})
    assert failed.status_code in (404, 422)
    assert seeded.a.name not in failed.text
