"""The SecOps workspace (HLD M13, ADR 0036): /v1/secops/vulnerabilities, /coverage,
/posture-score and /findings/{id}.

Against their own database, seeded like ``seeded`` and then given findings either
side of every modelled SLA boundary, scannable resources in each coverage state,
Qualys hosts in the resource quarantine and tickets on a finding's resource, so no
other suite's counts move. The clock is fixed, so a boundary is exact.

Each endpoint is checked for the figures it returns, for what another tenant's
rows do to them (nothing, unless the caller's scope includes that tenant), and for
refusals: a role without ``vulnerabilities.read``, and a customer, which holds it
but never reaches the workspace. The customer selector's ``tenant_id`` narrows the
three lists to one customer and never widens them (ADR 0032). The quarantine
grant (migration 0034) is checked at the database too: tenant-isolated, no blob
path, nothing for a customer role.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from cloudops_tenancy import Database

from .conftest import Api, Seeded, _access, _database, _seeded, onboard_portal
from .support import raw_scope

pytestmark = [pytest.mark.integration]

# The fixed clock every request in this module runs at.
NOW = datetime.now(UTC).replace(microsecond=0)
DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)
HOST = "VM-ALPHA-04.alpha.local"  # FQDN-first, as Qualys names hosts
ENDPOINTS = ("/v1/secops/vulnerabilities", "/v1/secops/coverage", "/v1/secops/posture-score")


@dataclass(frozen=True)
class SecOps:
    seeded: Seeded
    api: Api
    admin: UUID  # platform administrator, all tenants
    engineer_a: UUID  # Engineer, tenant a: vulnerabilities.read
    finops_all: UUID  # FinOps, all tenants: no vulnerabilities.read
    security_a: UUID  # customer Security officer of a: holds vulnerabilities.read
    empty: UUID  # a tenant with no resources and no findings
    findings: dict[str, UUID]  # by label


def _finding(cx: psycopg.Connection[Any], tenant: UUID, label: str, severity: int,
             first: datetime, *, asset: str = HOST, resource: UUID | None = None,
             remediated: datetime | None = None) -> UUID:
    (fid,) = cx.execute(
        "INSERT INTO cloudops.vulnerability (tenant_id, qid, asset_name, asset_ipv4,"
        " resource_id, scope, title, severity, first_detected, last_detected, remediated_at,"
        " solution, status) VALUES (%s, %s, %s, '10.0.0.4', %s, 'os', %s, %s, %s, %s, %s,"
        " 'Apply the vendor patch.', %s) RETURNING id",
        (tenant, 100000 + severity, asset, resource, label, severity, first, NOW - DAY,
         remediated, "Fixed" if remediated else "Active"),
    ).fetchone() or (None,)
    assert isinstance(fid, UUID)
    return fid


def _populate(s: Seeded, empty: UUID) -> dict[str, UUID]:
    a, b = s.a, s.b
    out: dict[str, UUID] = {}
    with psycopg.connect(s.admin_dsn, autocommit=True) as cx:
        cx.execute("INSERT INTO cloudops.tenant (id, canonical_name, short_code)"
                   " VALUES (%s, 'delta', 'DELTA')", (empty,))
        # Severity 5: 15-day target, at risk from 11.25 days (11 days 6 hours).
        target, risk = 15 * DAY, timedelta(days=11, hours=6)
        out["s5_breached"] = _finding(cx, a.id, "S5 past target", 5, NOW - target - SECOND)
        out["s5_at_target"] = _finding(cx, a.id, "S5 at target", 5, NOW - target)
        out["s5_at_risk"] = _finding(cx, a.id, "S5 at risk edge", 5, NOW - risk)
        out["s5_within"] = _finding(cx, a.id, "S5 within edge", 5, NOW - risk + SECOND)
        out["s1"] = _finding(cx, a.id, "S1 old, no target", 1, NOW - 400 * DAY)
        out["s3_fixed"] = _finding(cx, a.id, "S3 remediated", 3, NOW - 40 * DAY,
                                   remediated=NOW - 10 * DAY)
        # Linked to vm-alpha-01, past its 30-day target.
        out["s4_linked"] = _finding(cx, a.id, "S4 linked", 4, NOW - 35 * DAY,
                                    asset="vm-alpha-01.alpha.local", resource=a.resource_id)
        out["b_s5"] = _finding(cx, b.id, "B S5", 5, NOW - 60 * DAY, asset="vm-bravo-09")
        # Tickets on vm-alpha-01 (the base seed adds INC-alpha, open).
        for number, opened, closed in (("INC-SEC-OPEN", NOW - 5 * DAY, None),
                                       ("INC-SEC-RECENT", NOW - 20 * DAY, NOW - 19 * DAY),
                                       ("INC-SEC-OLD", NOW - 200 * DAY, NOW - 199 * DAY)):
            cx.execute(
                "INSERT INTO cloudops.work_item (tenant_id, kind, number, company_raw,"
                " opened_at, closed_at, resource_id, short_description)"
                " VALUES (%s, 'incident', %s, 'ALPHA', %s, %s, %s, 'patching')",
                (a.id, number, opened, closed, a.resource_id))
        # Scannable resources: one with a Qualys asset id and nothing linked, one with
        # neither; a storage account is not scannable.
        for name, kind, asset_id, ips in (
            ("vm-alpha-03", "compute.vm", "12345678", None),
            ("vm-alpha-04", "compute.vm", None, ["10.0.0.4"]),
            ("stalpha01", "storage.account", None, None),
        ):
            cx.execute(
                "INSERT INTO cloudops.resource (tenant_id, account_id, native_id, name,"
                " type_native, type_canonical, qualys_asset_id, ip_addresses)"
                " VALUES (%s, %s, %s, %s, 'x', %s, %s, CAST(%s AS inet[]))",
                (a.id, a.account_id, f"/x/{name}", name, kind, asset_id, ips))
        # The resource quarantine: a's Qualys host, a ServiceNow CI (not the
        # scanner's), and b's Qualys host.
        for tenant, source, key, name, ip, count in (
            (a.id, "qualys", "qualys:900001", HOST, "10.0.0.4", 3),
            (a.id, "servicenow", "ci:sql-legacy", "sql-legacy", None, 7),
            (b.id, "qualys", "qualys:900002", "bravo-shadow-it", "10.9.0.7", 2),
        ):
            cx.execute(
                "INSERT INTO staging.resource_quarantine (tenant_id, source_system,"
                " external_key, name, ip_address, occurrence_count, raw_ref)"
                " VALUES (%s, %s, %s, %s, CAST(%s AS inet), %s, %s)",
                (tenant, source, key, name, ip, count, f"{source}/raw/{key}"))
    return out


@pytest.fixture(scope="module")
async def secops(admin_server: str) -> AsyncIterator[SecOps]:
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from cloudops_api.app import create_app
    from cloudops_api.auth import StaticKeys, TokenValidator
    from cloudops_config import AuthConfig
    from cloudops_devdata.tokens import new_signing_key
    from cloudops_domain.assurance.pack import Ed25519Signer
    from cloudops_tenancy import TenantScope

    with _database(admin_server, "secops") as provisioned:
        seeded = _seeded(provisioned)
        empty = uuid4()
        findings = _populate(seeded, empty)
        admin, engineer_a, finops_all, cxo_a, security_a = (uuid4() for _ in range(5))
        db = Database.create(seeded.app_dsn, pool_size=4, max_overflow=0)
        a = seeded.a.id
        async with db.transaction(TenantScope.for_resolution("setup:secops")) as conn:
            await _access(conn, "SELECT access.bootstrap_admin(:o, 'Admin')", o=admin)
            for oid, role, everyone in ((engineer_a, "Engineer", False),
                                        (finops_all, "FinOps", True)):
                await _access(conn, "SELECT access.set_principal(:a, :o, 'staff', :n, :r, :all)",
                              a=admin, o=oid, n=f"{role} {oid}", r=role, all=everyone)
            await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)",
                          a=admin, o=engineer_a, t=a)
            await onboard_portal(conn, admin, a, cxo_a)
            await _access(conn, "SELECT access.set_principal(:a, :o, 'customer', 'Security',"
                          " 'Security officer', false)", a=admin, o=security_a)
            await _access(conn, "SELECT access.grant_tenant(:a, :o, :t)",
                          a=admin, o=security_a, t=a)
        auth = AuthConfig(
            issuer="https://login.example.test/77777777-7777-4777-8777-777777777777/v2.0",
            audience="api://cloudops-secops", tenant_id="77777777-7777-4777-8777-777777777777",
            required_scope="access_as_user", jwks_url=None, jwks_secret=None,
        )
        private, public = new_signing_key("secops")
        app = create_app(db=db, validator=TokenValidator(auth, StaticKeys(public)),
                         signer=Ed25519Signer(Ed25519PrivateKey.generate()))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://api") as client:
                yield SecOps(seeded, Api(client, private, auth), admin, engineer_a, finops_all,
                             security_a, empty, findings)
        finally:
            await db.dispose()


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from cloudops_api import secops

    monkeypatch.setattr(secops, "_now", lambda: NOW)


async def _get(s: SecOps, path: str, oid: UUID, status: int = 200,
               params: dict[str, str] | None = None) -> Any:
    response = await s.api.get(path, oid, params=params)
    assert response.status_code == status, (path, response.text)
    return response.json()


def _tenants(body: Any) -> set[str]:
    """Every tenant id anywhere in a response."""
    found: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if set(v) == {"id", "name"}:
                found.add(v["id"])
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(body)
    return found


def _value(metric: Any) -> Any:
    return None if metric is None else metric["value"]


# --- vulnerabilities and ageing --------------------------------------------------------


async def test_ageing_bands_at_their_boundaries(secops: SecOps) -> None:
    body = await _get(secops, "/v1/secops/vulnerabilities", secops.engineer_a)
    band = {f["id"]: f["sla"] for f in body["findings"]}
    f = secops.findings
    assert band[str(f["s5_breached"])] == "breached"  # one second past 15 days
    assert band[str(f["s5_at_target"])] == "at_risk"  # exactly 15 days: not yet breached
    assert band[str(f["s5_at_risk"])] == "at_risk"  # exactly 75% of the target
    assert band[str(f["s5_within"])] == "within"  # one second short of 75%
    assert band[str(f["s1"])] == "no_target"  # severity 1 has no target, however old
    assert band[str(f["s4_linked"])] == "breached"  # 35 days against 30
    assert str(f["s3_fixed"]) not in band  # remediated: not open


async def test_ageing_by_severity_is_modelled_and_says_when_there_is_no_target(
    secops: SecOps,
) -> None:
    body = await _get(secops, "/v1/secops/vulnerabilities", secops.engineer_a)
    assert _tenants(body) == {str(secops.seeded.a.id)}
    sev = {s["severity"]: s for s in body["by_severity"]}
    assert list(sev) == [5, 4, 3, 2, 1]
    s5 = sev[5]
    assert (_value(s5["open"]), _value(s5["breached"]), _value(s5["at_risk"]),
            _value(s5["within"])) == (4, 1, 2, 1)
    assert s5["target_days"] == {**s5["target_days"], "value": 15, "is_modelled": True,
                                 "source_system": "cloudops_policy"}
    assert s5["breached"]["is_modelled"] is True
    assert s5["open"]["is_modelled"] is False
    assert s5["open"]["source_system"] == "qualys"
    # The base seed's 3-day severity 4 is within; the linked one is breached.
    assert (_value(sev[4]["open"]), _value(sev[4]["breached"]), _value(sev[4]["within"])) \
        == (2, 1, 1)
    assert {a["band"]: _value(a["open"]) for a in sev[4]["by_age"]} == \
        {"0-30": 1, "31-90": 1, "91+": 0}
    assert _value(sev[4]["oldest_days"]) == 35
    # No target: "—", never zero.
    assert sev[1]["target_days"] is None
    assert sev[1]["breached"] is None
    assert sev[1]["at_risk"] is None
    assert _value(sev[1]["open"]) == 1
    assert {a["band"]: _value(a["open"]) for a in sev[1]["by_age"]}["91+"] == 1
    # Nothing open at severity 3 (the one there is remediated) or 2.
    assert _value(sev[3]["open"]) == 0
    assert sev[3]["oldest_days"] is None
    assert (_value(body["open_total"]), _value(body["breached_total"]),
            _value(body["at_risk_total"])) == (7, 2, 2)
    assert body["at_risk_fraction"]["value"] == "0.75"


async def test_findings_most_overdue_first_and_filtered(secops: SecOps) -> None:
    f = secops.findings
    body = await _get(secops, "/v1/secops/vulnerabilities", secops.engineer_a)
    order = [x["id"] for x in body["findings"]]
    assert order[:4] == [str(f["s5_breached"]), str(f["s4_linked"]), str(f["s5_at_target"]),
                         str(f["s5_at_risk"])]
    assert order[-1] == str(f["s1"])
    assert _value(body["listed"]) == 7
    first = body["findings"][0]
    assert _value(first["age"]) == 15
    assert first["target_days"]["value"] == 15
    breached = await _get(secops, "/v1/secops/vulnerabilities", secops.engineer_a,
                          params={"sla": "breached"})
    assert {x["id"] for x in breached["findings"]} == {str(f["s5_breached"]),
                                                       str(f["s4_linked"])}
    # The filter narrows the list, never the summary.
    assert _value(breached["open_total"]) == 7
    sev5 = await _get(secops, "/v1/secops/vulnerabilities", secops.engineer_a,
                      params={"severity": "5"})
    assert len(sev5["findings"]) == 4
    await _get(secops, "/v1/secops/vulnerabilities", secops.engineer_a, 422,
               params={"severity": "6"})
    await _get(secops, "/v1/secops/vulnerabilities", secops.engineer_a, 422,
               params={"sla": "late"})


async def test_all_tenants_see_each_customer_once(secops: SecOps) -> None:
    s = secops.seeded
    body = await _get(secops, "/v1/secops/vulnerabilities", secops.admin)
    customers = {c["tenant"]["name"]: c for c in body["by_customer"]}
    assert {"alpha", "bravo", "charlie", "delta"} <= set(customers)
    assert (_value(customers["alpha"]["open"]), _value(customers["alpha"]["breached"]),
            _value(customers["alpha"]["urgent_or_critical"])) == (7, 2, 6)
    assert (_value(customers["bravo"]["open"]), _value(customers["bravo"]["breached"])) == (2, 1)
    assert _value(customers["delta"]["open"]) == 0
    assert {str(t.id) for t in s.tenants} <= _tenants(body)


# --- coverage --------------------------------------------------------------------------


async def test_coverage_counts_scannable_resources_and_the_qualys_quarantine(
    secops: SecOps,
) -> None:
    body = await _get(secops, "/v1/secops/coverage", secops.engineer_a)
    assert _tenants(body) == {str(secops.seeded.a.id)}
    assert body["scannable_types"] == ["compute.vm"]
    [alpha] = body["by_customer"]
    # vm-alpha-01 has a linked finding, vm-alpha-03 a Qualys asset id, vm-alpha-04
    # neither; the storage account is not scannable.
    assert (_value(alpha["scannable"]), _value(alpha["detected"]),
            _value(alpha["asset_id_only"]), _value(alpha["unmatched"])) == (3, 1, 1, 1)
    assert alpha["coverage_percent"]["value"] == "66.7"
    assert alpha["coverage_percent"]["source_system"] == "platform_inventory"
    # HOST and the base seed's unknown-host; the ServiceNow CI is not the scanner's.
    assert _value(alpha["quarantined_hosts"]) == 2
    # The base seed's finding on "vm-alpha-01" and the five open on HOST: unlinked.
    assert _value(alpha["unlinked_open_findings"]) == 6
    [resource] = body["unmatched_resources"]
    assert (resource["name"], resource["ip_addresses"]) == ("vm-alpha-04", ["10.0.0.4"])
    assert _value(resource["possible_hosts"]) == 1  # HOST: same name_norm and address
    hosts = {h["external_key"]: h for h in body["quarantined_hosts"]}
    assert list(hosts) == ["qualys:900001", "q-alpha"]  # most sighted first
    host = hosts["qualys:900001"]
    assert (host["name"], host["ip_address"], host["external_key"]) == \
        (HOST, "10.0.0.4", "qualys:900001")
    assert (_value(host["occurrences"]), _value(host["open_findings"]),
            _value(host["possible_resources"])) == (3, 5, 1)
    assert "raw_ref" not in host
    assert (_value(body["scannable_total"]), _value(body["unmatched_total"]),
            _value(body["quarantined_total"])) == (3, 1, 2)


async def test_coverage_across_tenants(secops: SecOps) -> None:
    body = await _get(secops, "/v1/secops/coverage", secops.admin)
    customers = {c["tenant"]["name"]: c for c in body["by_customer"]}
    assert customers["delta"]["coverage_percent"] is None  # nothing scannable: not 0%
    assert customers["charlie"]["coverage_percent"]["value"] == "0.0"  # two VMs, no match
    assert {h["external_key"] for h in body["quarantined_hosts"]} == \
        {"qualys:900001", "qualys:900002", "q-alpha", "q-bravo", "q-charlie"}


# --- posture score ---------------------------------------------------------------------


async def test_posture_score_is_modelled_with_its_parts(secops: SecOps) -> None:
    body = await _get(secops, "/v1/secops/posture-score", secops.engineer_a)
    assert _tenants(body) == {str(secops.seeded.a.id)}
    assert "Weighted mean" in body["formula"]
    assert {w["part"]: w["weight"]["value"] for w in body["weights"]} == \
        {"sla_compliance": "0.50", "coverage": "0.25", "exposure": "0.25"}
    [alpha] = body["customers"]
    # Hosts: vm-alpha-01 by name (the base seed's unlinked finding), the resource,
    # and HOST. Points: four severity 5 (40) and two severity 4 (10) over 3 hosts.
    assert _value(alpha["hosts"]) == 3
    assert alpha["sla_compliance"]["value"] == "66.7"  # 4 of 6 with a target not breached
    assert alpha["coverage"]["value"] == "66.7"
    assert alpha["exposure"]["value"] == "83.3"
    assert alpha["score"] == {**alpha["score"], "value": "70.8", "is_modelled": True,
                              "source_system": "cloudops_policy", "unit": "points"}


async def test_posture_score_leaves_out_what_cannot_be_computed(secops: SecOps) -> None:
    body = await _get(secops, "/v1/secops/posture-score", secops.admin)
    customers = {c["tenant"]["name"]: c for c in body["customers"]}
    delta = customers["delta"]
    assert (delta["score"], delta["sla_compliance"], delta["coverage"], delta["exposure"]) == \
        (None, None, None, None)
    # charlie: one severity 4 within its target on one host, two VMs and no match.
    charlie = customers["charlie"]
    assert (charlie["sla_compliance"]["value"], charlie["coverage"]["value"],
            charlie["exposure"]["value"], charlie["score"]["value"]) == \
        ("100.0", "0.0", "95.0", "73.8")


def test_posture_score_rescales_to_the_parts_present() -> None:
    from cloudops_api.secops import posture_score

    assert posture_score({"sla_compliance": None, "coverage": None, "exposure": None}) is None
    assert posture_score({"sla_compliance": Decimal(80), "coverage": None,
                          "exposure": Decimal(40)}) == Decimal(200) / 3  # (0.5*80+0.25*40)/0.75
    assert posture_score({"sla_compliance": None, "coverage": Decimal(50),
                          "exposure": None}) == Decimal(50)


# --- one finding -----------------------------------------------------------------------


async def test_finding_drills_down_to_its_resource_and_tickets(secops: SecOps) -> None:
    fid = secops.findings["s4_linked"]
    body = await _get(secops, f"/v1/secops/findings/{fid}", secops.engineer_a)
    assert body["finding"]["sla"] == "breached"
    assert body["resource"]["id"] == str(secops.seeded.a.resource_id)
    assert body["resource"]["name"] == "vm-alpha-01"
    assert body["solution"] == "Apply the vendor patch."
    # Open ones first, then the recently closed; not the one closed 199 days ago.
    assert [t["number"] for t in body["tickets"]] == ["INC-alpha", "INC-SEC-OPEN",
                                                     "INC-SEC-RECENT"]
    assert _value(body["other_open_on_asset"]) == 0


async def test_unlinked_finding_has_no_resource_and_says_why(secops: SecOps) -> None:
    fid = secops.findings["s5_at_target"]
    body = await _get(secops, f"/v1/secops/findings/{fid}", secops.engineer_a)
    assert body["resource"] is None
    assert body["tickets"] is None  # nothing to link through: not "no tickets"
    assert _value(body["other_open_on_asset"]) == 4  # three severity 5 and the severity 1


async def test_remediated_finding_is_aged_to_its_fix(secops: SecOps) -> None:
    fid = secops.findings["s3_fixed"]
    body = await _get(secops, f"/v1/secops/findings/{fid}", secops.engineer_a)
    assert body["remediated_at"] is not None
    assert _value(body["finding"]["age"]) == 30
    assert body["finding"]["sla"] == "within"  # fixed in 30 of 90 days


async def test_another_tenants_finding_is_not_found(secops: SecOps) -> None:
    foreign = await secops.api.get(f"/v1/secops/findings/{secops.findings['b_s5']}",
                                   secops.engineer_a)
    missing = await secops.api.get(f"/v1/secops/findings/{uuid4()}", secops.engineer_a)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"
    seen = await _get(secops, f"/v1/secops/findings/{secops.findings['b_s5']}", secops.admin)
    assert seen["finding"]["tenant"]["name"] == "bravo"


# --- narrowing (the customer selector, ADR 0032) -------------------------------------


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_tenant_id_narrows_to_one_customer(secops: SecOps, path: str) -> None:
    b = str(secops.seeded.b.id)
    body = await _get(secops, path, secops.admin, params={"tenant_id": b})
    assert _tenants(body) == {b}


async def test_narrowed_figures_are_that_customers_own(secops: SecOps) -> None:
    b = str(secops.seeded.b.id)
    body = await _get(secops, "/v1/secops/vulnerabilities", secops.admin,
                      params={"tenant_id": b})
    assert (_value(body["open_total"]), _value(body["breached_total"])) == (2, 1)
    coverage = await _get(secops, "/v1/secops/coverage", secops.admin,
                          params={"tenant_id": b})
    assert {h["external_key"] for h in coverage["quarantined_hosts"]} == \
        {"qualys:900002", "q-bravo"}


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_tenant_id_never_widens(secops: SecOps, path: str) -> None:
    """Another tenant, and one that does not exist, are the same 404."""
    foreign = await _get(secops, path, secops.engineer_a, 404,
                         params={"tenant_id": str(secops.seeded.b.id)})
    missing = await _get(secops, path, secops.engineer_a, 404,
                         params={"tenant_id": str(uuid4())})
    assert foreign == {**missing, "error": {**missing["error"],
                                            "request_id": foreign["error"]["request_id"]}}


# --- refusals --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [*ENDPOINTS, "/v1/secops/findings/{fid}"])
async def test_a_customer_never_reaches_the_workspace(secops: SecOps, path: str) -> None:
    """The security officer holds vulnerabilities.read and reads /v1/posture; the
    staff workspace is refused all the same."""
    url = path.format(fid=secops.findings["s5_breached"])
    body = await _get(secops, url, secops.security_a, 403)
    assert body["error"]["code"] == "forbidden"
    posture = await _get(secops, "/v1/posture", secops.security_a)
    assert posture["open_total"]["value"] == 7


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_a_role_without_the_permission_is_refused(secops: SecOps, path: str) -> None:
    await _get(secops, path, secops.finops_all, 403)


async def test_the_customer_posture_view_is_unchanged(secops: SecOps) -> None:
    """The shared age bands give the customer's view the same figures as before."""
    body = await _get(secops, "/v1/posture", secops.security_a)
    ages = {a["band"]: a["open"]["value"] for a in body["by_age"]}
    # 0-30: four severity 5 and the base seed's; 31-90: the linked one; 91+: severity 1.
    assert ages == {"0-30": 5, "31-90": 1, "91+": 1}
    assert {s["severity"]: s["open"]["value"] for s in body["by_severity"]} == \
        {5: 4, 4: 2, 3: 0, 2: 0, 1: 1}


# --- the quarantine grant (migration 0034) --------------------------------------------


@pytest.mark.isolation
def test_quarantine_read_is_tenant_isolated(secops: SecOps) -> None:
    s = secops.seeded
    with raw_scope(s.app_dsn, str(s.a.id), "engineer") as cx:
        keys = {r[0] for r in cx.execute(
            "SELECT external_key FROM staging.resource_quarantine").fetchall()}
    assert keys == {"qualys:900001", "ci:sql-legacy", "q-alpha"}
    with raw_scope(s.app_dsn, "", "engineer") as cx:
        assert cx.execute("SELECT count(*) FROM staging.resource_quarantine").fetchone() == (0,)


@pytest.mark.isolation
def test_quarantine_blob_path_and_writes_are_refused(secops: SecOps) -> None:
    s = secops.seeded
    with raw_scope(s.app_dsn, str(s.a.id), "engineer") as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute("SELECT raw_ref FROM staging.resource_quarantine")
    with raw_scope(s.app_dsn, str(s.a.id), "engineer") as cx, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cx.execute("DELETE FROM staging.resource_quarantine")


@pytest.mark.isolation
def test_quarantine_is_out_of_a_customer_roles_reach(secops: SecOps) -> None:
    s = secops.seeded
    with raw_scope(s.app_dsn, str(s.a.id), "customer") as cx:
        cx.execute("SET LOCAL ROLE cloudops_customer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cx.execute("SELECT external_key FROM staging.resource_quarantine")
