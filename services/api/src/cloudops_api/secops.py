"""The SecOps workspace (HLD M13, ADR 0036): staff only, ``vulnerabilities.read``.

- ``/v1/secops/vulnerabilities``: open findings by severity and age, each aged
  against its severity's remediation target; per customer; the findings, most
  overdue first.
- ``/v1/secops/coverage``: scanner coverage. Scannable resources in inventory with
  no Qualys host match, and Qualys hosts with no resource match (the resource
  quarantine, LLD Identity resolution).
- ``/v1/secops/posture-score``: a posture score per customer, with its parts.
- ``/v1/secops/findings/{id}``: one finding, its resource and the tickets linked
  to that resource.

Same rules as ``bff``: queries under the caller's resolved scope, narrowed to one
customer by ``tenant_id`` on the three lists (``ScopeDep``, ADR 0032), so row-level
security decides what exists, and every number a ``Metric``. A customer is refused
every endpoint here, whatever its permissions: it has the security posture view
(``/v1/posture``), which this module's age bands are shared with.

**The remediation targets and the score are modelled** (ADR 0036). The HLD asks
for ageing against SLA but neither it nor the LLD sets a target, so
``SLA_TARGET_DAYS`` is the platform's policy, not a contract: every figure derived
from it is ``is_modelled`` and names the policy as its source. A finding is
``breached`` once more than its target has elapsed since Qualys first detected it,
``at_risk`` from ``AT_RISK_FRACTION`` of the target, and severity 1 has none.

Coverage counts resources of the ``SCANNABLE_TYPES`` only. The connector reads
host detections, so a host Qualys scanned clean is indistinguishable from one it
never scanned; a resource whose inventory record carries a Qualys asset id counts
as matched for that reason, and is reported apart.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text

from cloudops_api.access import Access
from cloudops_api.bff import (
    _RESOURCE_COLUMNS,
    _RESOURCE_FROM,
    ResourceStaffView,
    TenantView,
    _resource,
)
from cloudops_api.operations import WorkItemSummary, _item_columns, _summary
from cloudops_api.pipeline import (
    ApiError,
    AuditedRoute,
    ScopeDep,
    audit_only,
    current_access,
    platform,
    requires,
)
from cloudops_api.portal import AGE_BAND, AGE_BANDS, AGE_DAYS, AgeBand, Severity
from cloudops_api.provenance import ApiModel, Metric, query_ref

router = APIRouter(prefix="/v1/secops", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]

PERMISSION: Final = "vulnerabilities.read"
SCANNER: Final = "qualys"
INVENTORY: Final = "platform_inventory"
POLICY_SOURCE: Final = "cloudops_policy"
MAX_LIST: Final = 500
MAX_TICKETS: Final = 50
TICKET_DAYS: Final = 90

# Modelled remediation targets by Qualys severity, in days since first detection
# (ADR 0036). Severity 1 (minimal) has none.
SLA_TARGET_DAYS: Final[dict[int, int]] = {5: 15, 4: 30, 3: 90, 2: 180}
AT_RISK_FRACTION: Final = Decimal("0.75")  # not money: elapsed over target
SEVERITIES: Final[tuple[Severity, ...]] = (5, 4, 3, 2, 1)
SCANNABLE_TYPES: Final = ("compute.vm",)

# The posture score (ADR 0036): a weighted mean of three parts, each 0 to 100.
# A part that cannot be computed is left out and the others' weights rescale.
Part = Literal["sla_compliance", "coverage", "exposure"]
SCORE_WEIGHTS: Final[dict[Part, Decimal]] = {
    "sla_compliance": Decimal("0.50"),  # not money: a score weight
    "coverage": Decimal("0.25"),  # not money: a score weight
    "exposure": Decimal("0.25"),  # not money: a score weight
}
# Points an open finding costs its host, by severity, for the exposure part.
EXPOSURE_POINTS: Final[dict[int, int]] = {5: 10, 4: 5, 3: 2, 2: 1, 1: 0}
HUNDRED: Final = Decimal(100)  # not money: a percentage scale
ONE_PLACE: Final = Decimal("0.1")  # not money: a score is shown to one place
SCORE_FORMULA: Final = (
    "Weighted mean of the parts that can be computed, rescaled to their weights."
    " SLA compliance (50%): open findings with a target that have not passed it, as a"
    " percentage of open findings with a target. Coverage (25%): scannable resources"
    " with a Qualys match, as a percentage of scannable resources. Exposure (25%): 100"
    " less the average points per host Qualys has reported, never below 0; an open"
    " finding costs 10 points at severity 5, 5 at 4, 2 at 3, 1 at 2 and none at 1."
    " SLA compliance and exposure need at least one reported host; coverage needs a"
    " scannable resource.")

SlaBand = Literal["breached", "at_risk", "within", "no_target"]
SLA_BANDS: Final[tuple[SlaBand, ...]] = ("breached", "at_risk", "within", "no_target")


def _now() -> datetime:
    return datetime.now(UTC)


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


def _count(value: Any, unit: str, source: str, as_of: datetime, name: str, *,
           modelled: bool = False, **params: Any) -> Metric[int]:
    return Metric[int](value=int(value or 0), unit=unit, source_system=source, as_of=as_of,
                       query_ref=query_ref(name, 1, **params), is_modelled=modelled)


def _score(value: Decimal | None, as_of: datetime, name: str, **params: Any
           ) -> Metric[Decimal] | None:
    if value is None:
        return None
    return Metric[Decimal](value=value.quantize(ONE_PLACE), unit="points", source_system=
                           POLICY_SOURCE, as_of=as_of, query_ref=query_ref(name, 1, **params),
                           is_modelled=True)


def _staff(access: Access) -> None:
    if access.kind != "staff":
        raise ApiError(403, "forbidden")


def _target(severity: int, as_of: datetime) -> Metric[int] | None:
    days = SLA_TARGET_DAYS.get(severity)
    if days is None:
        return None
    return _count(days, "days", POLICY_SOURCE, as_of, "secops.sla_target", modelled=True,
                  severity=severity)


# The modelled target per severity, joined in from the policy above.
_TARGETS: Final = (
    "LEFT JOIN unnest(CAST(:sev AS int[]), CAST(:days AS int[])) AS tg(sev, days)"
    " ON tg.sev = v.severity"
)
# Until remediation, or now: a remediated finding is aged by how long its fix took.
_ELAPSED: Final = (
    "extract(epoch FROM (coalesce(v.remediated_at, CAST(:now AS timestamptz))"
    " - v.first_detected))"
)
_SLA: Final = (
    "CASE WHEN tg.days IS NULL THEN 'no_target'"
    f" WHEN {_ELAPSED} > tg.days * 86400 THEN 'breached'"
    f" WHEN {_ELAPSED} >= tg.days * 86400 * CAST(:frac AS numeric) THEN 'at_risk'"
    " ELSE 'within' END"
)
_SLA_RANK: Final = (
    "CASE " + " ".join(f"WHEN {_SLA} = '{b}' THEN {i}" for i, b in enumerate(SLA_BANDS))
    + " END"
)


def _policy(now: datetime) -> dict[str, Any]:
    return {"sev": list(SLA_TARGET_DAYS), "days": list(SLA_TARGET_DAYS.values()),
            "frac": AT_RISK_FRACTION, "now": now}


# Open findings per tenant, severity, SLA band and age band, with the oldest age.
_AGEING: Final = text(
    f"SELECT v.tenant_id, v.severity, {_SLA}, {AGE_BAND}, count(*), max({AGE_DAYS})"  # noqa: S608 - constants
    f" FROM cloudops.vulnerability v {_TARGETS}"
    " WHERE v.remediated_at IS NULL GROUP BY 1, 2, 3, 4"
)
_FINDING_COLUMNS: Final = (
    "v.id, t.id, t.canonical_name, v.qid, v.title, v.severity, v.asset_name, v.asset_ipv4,"
    f" v.resource_id, v.first_detected, v.last_detected, v.status, {_SLA}"
)
_FINDINGS: Final = text(
    f"SELECT {_FINDING_COLUMNS}"  # noqa: S608 - constants
    f" FROM cloudops.vulnerability v JOIN cloudops.tenant t ON t.id = v.tenant_id {_TARGETS}"
    " WHERE v.remediated_at IS NULL"
    "   AND (CAST(:severity AS int) IS NULL OR v.severity = CAST(:severity AS int))"
    f"  AND (CAST(:band AS text) IS NULL OR {_SLA} = CAST(:band AS text))"
    # Most overdue first: breached, at risk, within, no target; then severity, then age.
    f" ORDER BY {_SLA_RANK}, v.severity DESC, v.first_detected, v.id LIMIT :limit"
)
_TENANTS: Final = text("SELECT id, canonical_name FROM cloudops.tenant ORDER BY canonical_name")

# Scannable resources per tenant: detected (a finding is linked), a Qualys asset id
# on record but no finding linked, or neither.
_LINKED: Final = (
    "LEFT JOIN (SELECT DISTINCT resource_id FROM cloudops.vulnerability"
    "           WHERE resource_id IS NOT NULL) d ON d.resource_id = r.id"
)
_COVERAGE: Final = text(
    "SELECT r.tenant_id, count(*), count(d.resource_id),"  # noqa: S608 - constants
    "   count(*) FILTER (WHERE d.resource_id IS NULL AND r.qualys_asset_id IS NOT NULL)"
    f" FROM cloudops.resource r {_LINKED}"
    " WHERE r.deleted_at IS NULL AND r.type_canonical = ANY(CAST(:types AS text[]))"
    " GROUP BY 1"
)
# A possible match is a hint for a person, never a link: same tenant, and the same
# name_norm (the host part of an FQDN, lower case) or an address in common.
_NAME_NORM: Final = "lower(split_part(q.name, '.', 1))"
_UNMATCHED: Final = text(
    "SELECT r.id, t.id, t.canonical_name, r.name, r.fqdn, r.type_canonical, r.region,"  # noqa: S608 - constants
    "   coalesce(array(SELECT host(a) FROM unnest(r.ip_addresses) a), '{}'), r.last_seen,"
    "   (SELECT count(*) FROM staging.resource_quarantine q"
    "    WHERE q.tenant_id = r.tenant_id AND q.source_system = :source"
    f"     AND ({_NAME_NORM} = r.name_norm OR q.ip_address = ANY(r.ip_addresses)))"
    f" FROM cloudops.resource r JOIN cloudops.tenant t ON t.id = r.tenant_id {_LINKED}"
    " WHERE r.deleted_at IS NULL AND r.type_canonical = ANY(CAST(:types AS text[]))"
    "   AND d.resource_id IS NULL AND r.qualys_asset_id IS NULL"
    " ORDER BY t.canonical_name, r.name, r.id LIMIT :limit"
)
_QUARANTINE: Final = text(
    "SELECT q.id, t.id, t.canonical_name, q.external_key, q.name, host(q.ip_address),"  # noqa: S608 - constants
    "   q.first_seen, q.last_seen, q.occurrence_count,"
    "   (SELECT count(*) FROM cloudops.vulnerability v WHERE v.tenant_id = q.tenant_id"
    "    AND v.resource_id IS NULL AND v.remediated_at IS NULL AND v.asset_name = q.name),"
    "   (SELECT count(*) FROM cloudops.resource r WHERE r.tenant_id = q.tenant_id"
    "    AND r.deleted_at IS NULL"
    f"   AND (r.name_norm = {_NAME_NORM} OR q.ip_address = ANY(r.ip_addresses)))"
    " FROM staging.resource_quarantine q JOIN cloudops.tenant t ON t.id = q.tenant_id"
    " WHERE q.source_system = :source"
    " ORDER BY q.occurrence_count DESC, q.last_seen DESC, q.id LIMIT :limit"
)
_QUARANTINE_BY_TENANT: Final = text(
    "SELECT tenant_id, count(*) FROM staging.resource_quarantine"
    " WHERE source_system = :source GROUP BY 1"
)
_UNLINKED_OPEN: Final = text(
    "SELECT tenant_id, count(*) FROM cloudops.vulnerability"
    " WHERE resource_id IS NULL AND remediated_at IS NULL GROUP BY 1"
)
# Hosts Qualys has reported a finding on, ever: a linked resource, or an unlinked
# asset by its name_norm.
_HOSTS: Final = text(
    "SELECT tenant_id, count(DISTINCT coalesce(resource_id::text,"
    "   'name:' || lower(split_part(asset_name, '.', 1))))"
    " FROM cloudops.vulnerability GROUP BY 1"
)


# --- vulnerabilities -----------------------------------------------------------------


class SeverityAgeing(ApiModel):
    severity: Severity
    target_days: Metric[int] | None  # modelled; None: no target for this severity
    open: Metric[int]
    # Modelled. None where the severity has no target: never zero.
    within: Metric[int] | None
    at_risk: Metric[int] | None
    breached: Metric[int] | None
    by_age: list[AgeBand]
    oldest_days: Metric[int] | None  # None: nothing open


class CustomerAgeing(ApiModel):
    tenant: TenantView
    open: Metric[int]
    urgent_or_critical: Metric[int]  # severity 5 or 4
    at_risk: Metric[int]
    breached: Metric[int]


class SecOpsFinding(ApiModel):
    id: UUID
    tenant: TenantView
    qid: str  # Qualys identifier, not a quantity
    title: str
    severity: Severity
    asset_name: str
    asset_ipv4: str | None
    resource_id: UUID | None  # None: matched no resource (see coverage)
    first_detected: datetime
    last_detected: datetime | None
    status: str | None
    age: Metric[int]
    sla: SlaBand  # modelled, against target_days
    target_days: Metric[int] | None


class SecOpsVulnerabilities(ApiModel):
    at_risk_fraction: Metric[Decimal]  # modelled: share of the target that makes a finding at risk
    open_total: Metric[int]
    breached_total: Metric[int]
    at_risk_total: Metric[int]
    by_severity: list[SeverityAgeing]
    by_customer: list[CustomerAgeing]
    findings: list[SecOpsFinding]
    listed: Metric[int]  # findings returned: at most MAX_LIST of those matching the filter


def _finding(row: tuple[Any, ...], now: datetime) -> SecOpsFinding:
    (fid, tid, tname, qid, title, severity, asset, ipv4, rid, first, last, status, sla) = row[:13]
    until = row[18] if len(row) > 18 and row[18] is not None else now  # remediated_at
    return SecOpsFinding(
        id=fid, tenant=TenantView(id=tid, name=tname), qid=str(qid), title=title,
        severity=severity, asset_name=asset, asset_ipv4=ipv4, resource_id=rid,
        first_detected=first, last_detected=last, status=status,
        age=_count(max((until - first).days, 0), "days", SCANNER, now, "secops.finding_age",
                   finding=str(fid)),
        sla=sla, target_days=_target(severity, now))


@router.get("/vulnerabilities", response_model=SecOpsVulnerabilities)
@requires(PERMISSION)
async def vulnerabilities(
    request: Request, access: AccessDep, scope: ScopeDep,
    severity: Annotated[int | None, Query(ge=1, le=5)] = None,
    sla: SlaBand | None = None,
) -> SecOpsVulnerabilities:
    _staff(access)
    now = _now()
    policy = _policy(now)
    async with platform(request).db.transaction(scope) as conn:
        tenants = _rows(await conn.execute(_TENANTS))
        ageing = _rows(await conn.execute(_AGEING, policy))
        rows = _rows(await conn.execute(_FINDINGS, {**policy, "severity": severity,
                                                    "band": sla, "limit": MAX_LIST}))

    def count(value: int, name: str, **params: Any) -> Metric[int]:
        return _count(value, "vulnerabilities", SCANNER, now, name, **params)

    def modelled(value: int, name: str, **params: Any) -> Metric[int]:
        return _count(value, "vulnerabilities", POLICY_SOURCE, now, name, modelled=True,
                      **params)

    per_sev: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ages: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    oldest: dict[int, int] = {}
    per_tenant: dict[UUID, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for tid, sev, band, age_band, n, max_age in ageing:
        sev, n = int(sev), int(n)
        per_sev[sev][band] += n
        if age_band is not None:
            ages[sev][age_band] += n
        if max_age is not None:
            oldest[sev] = max(oldest.get(sev, 0), int(max_age))
        tally = per_tenant[UUID(str(tid))]
        tally["open"] += n
        tally[band] += n
        if sev >= 4:
            tally["urgent"] += n

    by_severity: list[SeverityAgeing] = []
    for sev in SEVERITIES:
        bands = per_sev.get(sev, {})
        has_target = sev in SLA_TARGET_DAYS
        by_severity.append(SeverityAgeing(
            severity=sev, target_days=_target(sev, now),
            open=count(sum(bands.values()), "secops.open_by_severity", severity=sev),
            within=modelled(bands.get("within", 0), "secops.within_by_severity",
                            severity=sev) if has_target else None,
            at_risk=modelled(bands.get("at_risk", 0), "secops.at_risk_by_severity",
                             severity=sev) if has_target else None,
            breached=modelled(bands.get("breached", 0), "secops.breached_by_severity",
                              severity=sev) if has_target else None,
            by_age=[AgeBand(band=b, open=count(ages.get(sev, {}).get(b, 0),
                                               "secops.open_by_age", severity=sev, band=b))
                    for b, _, _ in AGE_BANDS],
            oldest_days=_count(oldest[sev], "days", SCANNER, now, "secops.oldest_open",
                               severity=sev) if sev in oldest else None))

    totals = {b: sum(t.get(b, 0) for t in per_tenant.values()) for b in ("open", *SLA_BANDS)}
    return SecOpsVulnerabilities(
        at_risk_fraction=Metric[Decimal](
            value=AT_RISK_FRACTION, unit="fraction of target", source_system=POLICY_SOURCE,
            as_of=now, query_ref=query_ref("secops.at_risk_fraction", 1), is_modelled=True),
        open_total=count(totals["open"], "secops.open_total"),
        breached_total=modelled(totals["breached"], "secops.breached_total"),
        at_risk_total=modelled(totals["at_risk"], "secops.at_risk_total"),
        by_severity=by_severity,
        by_customer=[
            CustomerAgeing(
                tenant=TenantView(id=tid, name=name),
                open=count(per_tenant[tid]["open"], "secops.open_by_customer",
                           tenant=str(tid)),
                urgent_or_critical=count(per_tenant[tid]["urgent"],
                                         "secops.urgent_by_customer", tenant=str(tid)),
                at_risk=modelled(per_tenant[tid]["at_risk"], "secops.at_risk_by_customer",
                                 tenant=str(tid)),
                breached=modelled(per_tenant[tid]["breached"], "secops.breached_by_customer",
                                  tenant=str(tid)))
            for tid, name in tenants
        ],
        findings=[_finding(r, now) for r in rows],
        listed=count(len(rows), "secops.findings_listed", severity=severity, sla=sla),
    )


# --- coverage ------------------------------------------------------------------------


class CustomerCoverage(ApiModel):
    tenant: TenantView
    scannable: Metric[int]
    detected: Metric[int]  # a Qualys finding is linked to the resource
    asset_id_only: Metric[int]  # a Qualys asset id on record, no finding linked
    unmatched: Metric[int]  # neither
    coverage_percent: Metric[Decimal] | None  # None: nothing scannable
    quarantined_hosts: Metric[int]  # Qualys hosts that matched no resource
    unlinked_open_findings: Metric[int]  # open findings on those hosts


class UnmatchedResource(ApiModel):
    id: UUID
    tenant: TenantView
    name: str
    fqdn: str | None
    type: str
    region: str | None
    ip_addresses: list[str]
    last_seen: datetime
    possible_hosts: Metric[int]  # quarantined Qualys hosts sharing its name_norm or an IP


class QuarantinedHost(ApiModel):
    id: str  # the quarantine row, not a quantity
    tenant: TenantView
    external_key: str
    name: str | None
    ip_address: str | None
    first_seen: datetime
    last_seen: datetime
    occurrences: Metric[int]
    open_findings: Metric[int]
    possible_resources: Metric[int]  # resources sharing its name_norm or an IP


class SecOpsCoverage(ApiModel):
    scannable_types: list[str]
    scannable_total: Metric[int]
    unmatched_total: Metric[int]
    quarantined_total: Metric[int]
    by_customer: list[CustomerCoverage]
    unmatched_resources: list[UnmatchedResource]
    quarantined_hosts: list[QuarantinedHost]


def _percent(part: int, whole: int) -> Decimal | None:
    return None if whole == 0 else HUNDRED * part / whole


@router.get("/coverage", response_model=SecOpsCoverage)
@requires(PERMISSION)
async def coverage(request: Request, access: AccessDep, scope: ScopeDep) -> SecOpsCoverage:
    _staff(access)
    now = _now()
    types = {"types": list(SCANNABLE_TYPES)}
    async with platform(request).db.transaction(scope) as conn:
        tenants = _rows(await conn.execute(_TENANTS))
        covered = {UUID(str(r[0])): tuple(int(x) for x in r[1:])
                   for r in _rows(await conn.execute(_COVERAGE, types))}
        held = {UUID(str(r[0])): int(r[1]) for r in _rows(
            await conn.execute(_QUARANTINE_BY_TENANT, {"source": SCANNER}))}
        unlinked = {UUID(str(r[0])): int(r[1])
                    for r in _rows(await conn.execute(_UNLINKED_OPEN))}
        unmatched = _rows(await conn.execute(
            _UNMATCHED, {**types, "source": SCANNER, "limit": MAX_LIST}))
        quarantined = _rows(await conn.execute(
            _QUARANTINE, {"source": SCANNER, "limit": MAX_LIST}))

    def inventory(value: int, name: str, **params: Any) -> Metric[int]:
        return _count(value, "resources", INVENTORY, now, name, **params)

    by_customer: list[CustomerCoverage] = []
    for tid, name in tenants:
        scannable, detected, asset_only = covered.get(tid, (0, 0, 0))
        share = _percent(detected + asset_only, scannable)
        by_customer.append(CustomerCoverage(
            tenant=TenantView(id=tid, name=name),
            scannable=inventory(scannable, "secops.scannable", tenant=str(tid)),
            detected=inventory(detected, "secops.detected", tenant=str(tid)),
            asset_id_only=inventory(asset_only, "secops.asset_id_only", tenant=str(tid)),
            unmatched=inventory(scannable - detected - asset_only, "secops.unmatched",
                                tenant=str(tid)),
            coverage_percent=None if share is None else Metric[Decimal](
                value=share.quantize(ONE_PLACE), unit="percent", source_system=INVENTORY,
                as_of=now, query_ref=query_ref("secops.coverage", 1, tenant=str(tid)),
                is_modelled=False),
            quarantined_hosts=_count(held.get(tid, 0), "hosts", SCANNER, now,
                                     "secops.quarantined_hosts", tenant=str(tid)),
            unlinked_open_findings=_count(unlinked.get(tid, 0), "vulnerabilities", SCANNER,
                                          now, "secops.unlinked_open", tenant=str(tid))))
    scannable_total = sum(c.scannable.value for c in by_customer)
    return SecOpsCoverage(
        scannable_types=list(SCANNABLE_TYPES),
        scannable_total=inventory(scannable_total, "secops.scannable_total"),
        unmatched_total=inventory(sum(c.unmatched.value for c in by_customer),
                                  "secops.unmatched_total"),
        quarantined_total=_count(sum(held.values()), "hosts", SCANNER, now,
                                 "secops.quarantined_total"),
        by_customer=by_customer,
        unmatched_resources=[
            UnmatchedResource(
                id=rid, tenant=TenantView(id=tid, name=tname), name=name, fqdn=fqdn,
                type=canonical, region=region, ip_addresses=[str(a) for a in ips],
                last_seen=last,
                possible_hosts=_count(hint, "hosts", SCANNER, now, "secops.possible_hosts",
                                      resource=str(rid)))
            for rid, tid, tname, name, fqdn, canonical, region, ips, last, hint in unmatched
        ],
        quarantined_hosts=[
            QuarantinedHost(
                id=str(qid), tenant=TenantView(id=tid, name=tname), external_key=key,
                name=name, ip_address=ip, first_seen=first, last_seen=last,
                occurrences=_count(seen, "detections", SCANNER, now, "secops.occurrences",
                                   host=str(qid)),
                open_findings=_count(open_, "vulnerabilities", SCANNER, now,
                                     "secops.host_open_findings", host=str(qid)),
                possible_resources=_count(hint, "resources", INVENTORY, now,
                                          "secops.possible_resources", host=str(qid)))
            for qid, tid, tname, key, name, ip, first, last, seen, open_, hint in quarantined
        ],
    )


# --- posture score -------------------------------------------------------------------


class ScoreWeight(ApiModel):
    part: Part
    weight: Metric[Decimal]


class CustomerPostureScore(ApiModel):
    tenant: TenantView
    score: Metric[Decimal] | None  # None: no part can be computed (no scanner data)
    sla_compliance: Metric[Decimal] | None
    coverage: Metric[Decimal] | None
    exposure: Metric[Decimal] | None
    hosts: Metric[int]  # hosts Qualys has reported a finding on
    open_findings: Metric[int]


class SecOpsPostureScores(ApiModel):
    formula: str
    weights: list[ScoreWeight]
    customers: list[CustomerPostureScore]


def posture_score(parts: dict[Part, Decimal | None]) -> Decimal | None:
    """The weighted mean of the parts present, rescaled to their weights."""
    present = {k: v for k, v in parts.items() if v is not None}
    if not present:
        return None
    weight = sum((SCORE_WEIGHTS[k] for k in present), Decimal(0))
    return sum((SCORE_WEIGHTS[k] * v for k, v in present.items()), Decimal(0)) / weight


@router.get("/posture-score", response_model=SecOpsPostureScores)
@requires(PERMISSION)
async def posture_scores(
    request: Request, access: AccessDep, scope: ScopeDep,
) -> SecOpsPostureScores:
    _staff(access)
    now = _now()
    async with platform(request).db.transaction(scope) as conn:
        tenants = _rows(await conn.execute(_TENANTS))
        ageing = _rows(await conn.execute(_AGEING, _policy(now)))
        covered = {UUID(str(r[0])): tuple(int(x) for x in r[1:]) for r in _rows(
            await conn.execute(_COVERAGE, {"types": list(SCANNABLE_TYPES)}))}
        hosts = {UUID(str(r[0])): int(r[1]) for r in _rows(await conn.execute(_HOSTS))}

    open_: dict[UUID, int] = defaultdict(int)
    targeted: dict[UUID, int] = defaultdict(int)
    breached: dict[UUID, int] = defaultdict(int)
    points: dict[UUID, int] = defaultdict(int)
    for tid, sev, band, _, n, _ in ageing:
        key, n = UUID(str(tid)), int(n)
        open_[key] += n
        points[key] += EXPOSURE_POINTS[int(sev)] * n
        if band != "no_target":
            targeted[key] += n
        if band == "breached":
            breached[key] += n

    customers: list[CustomerPostureScore] = []
    for tid, name in tenants:
        reported = hosts.get(tid, 0)
        scannable, detected, asset_only = covered.get(tid, (0, 0, 0))
        parts: dict[Part, Decimal | None] = {
            "sla_compliance": None if reported == 0 else (
                HUNDRED if targeted[tid] == 0
                else HUNDRED * (targeted[tid] - breached[tid]) / targeted[tid]),
            "coverage": _percent(detected + asset_only, scannable),
            "exposure": None if reported == 0
            else max(Decimal(0), HUNDRED - Decimal(points[tid]) / reported),
        }
        t = str(tid)
        customers.append(CustomerPostureScore(
            tenant=TenantView(id=tid, name=name),
            score=_score(posture_score(parts), now, "secops.posture_score", tenant=t),
            sla_compliance=_score(parts["sla_compliance"], now, "secops.sla_compliance",
                                  tenant=t),
            coverage=_score(parts["coverage"], now, "secops.score_coverage", tenant=t),
            exposure=_score(parts["exposure"], now, "secops.exposure", tenant=t),
            hosts=_count(reported, "hosts", SCANNER, now, "secops.reported_hosts", tenant=t),
            open_findings=_count(open_[tid], "vulnerabilities", SCANNER, now,
                                 "secops.score_open", tenant=t)))
    return SecOpsPostureScores(
        formula=SCORE_FORMULA,
        weights=[ScoreWeight(part=part, weight=Metric[Decimal](
            value=w, unit="weight", source_system=POLICY_SOURCE, as_of=now,
            query_ref=query_ref("secops.score_weight", 1, part=part), is_modelled=True))
            for part, w in SCORE_WEIGHTS.items()],
        customers=customers,
    )


# --- one finding ---------------------------------------------------------------------


class FindingDetail(ApiModel):
    finding: SecOpsFinding
    scope: str
    detection_type: str | None
    operating_system: str | None
    threat: str | None
    solution: str | None
    remediated_at: datetime | None
    resource: ResourceStaffView | None  # None: the host matched no resource
    other_open_on_asset: Metric[int]
    # Tickets linked to the finding's resource: open ones, and any opened in the last
    # TICKET_DAYS. None: no resource, so nothing can be linked; [] when none is.
    tickets: list[WorkItemSummary] | None


@router.get("/findings/{finding_id}", response_model=FindingDetail)
@requires(PERMISSION)
async def finding(request: Request, finding_id: UUID, access: AccessDep) -> FindingDetail:
    _staff(access)
    now = _now()
    async with platform(request).db.transaction(access.scope) as conn:
        found = (await conn.execute(text(
            f"SELECT {_FINDING_COLUMNS}, v.scope, v.detection_type,"  # noqa: S608 - constants
            "   v.operating_system, v.threat, v.solution, v.remediated_at"
            " FROM cloudops.vulnerability v JOIN cloudops.tenant t ON t.id = v.tenant_id"
            f" {_TARGETS} WHERE v.id = :id"), {**_policy(now), "id": finding_id})).one_or_none()
        if found is None:
            # Another tenant's finding and one that never existed look the same here,
            # because RLS made them the same (LLD isolation case 1).
            raise ApiError(404, "not_found")
        row: tuple[Any, ...] = tuple(found)
        resource_id, tenant_id = row[8], row[1]
        resource: ResourceStaffView | None = None
        tickets: list[WorkItemSummary] | None = None
        if resource_id is not None:
            res = (await conn.execute(text(
                f"SELECT {_RESOURCE_COLUMNS} FROM {_RESOURCE_FROM} WHERE r.id = :id"),  # noqa: S608
                {"id": resource_id})).one_or_none()
            shown = None if res is None else _resource(tuple(res), access)
            resource = shown if isinstance(shown, ResourceStaffView) else None  # staff only
            tickets = [_summary(tuple(r), True) for r in await conn.execute(text(
                f"SELECT {_item_columns(True)} FROM cloudops.work_item w"  # noqa: S608
                " JOIN cloudops.tenant t ON t.id = w.tenant_id WHERE w.resource_id = :id"
                "   AND (w.closed_at IS NULL OR w.opened_at >= :since)"
                " ORDER BY w.closed_at IS NOT NULL, w.opened_at DESC, w.number LIMIT :limit"),
                {"id": resource_id, "since": now - timedelta(days=TICKET_DAYS),
                 "limit": MAX_TICKETS})]
            others = await conn.scalar(text(
                "SELECT count(*) FROM cloudops.vulnerability WHERE resource_id = :rid"
                " AND remediated_at IS NULL AND id <> :id"), {"rid": resource_id, "id": finding_id})
        else:
            others = await conn.scalar(text(
                "SELECT count(*) FROM cloudops.vulnerability WHERE tenant_id = :t"
                " AND resource_id IS NULL AND asset_name = :asset AND remediated_at IS NULL"
                " AND id <> :id"), {"t": tenant_id, "asset": row[6], "id": finding_id})
    audit_only(request, [tenant_id])
    scope, detection, os_name, threat, solution, remediated = row[13:19]
    return FindingDetail(
        finding=_finding(row, now), scope=scope, detection_type=detection,
        operating_system=os_name, threat=threat, solution=solution, remediated_at=remediated,
        resource=resource,
        other_open_on_asset=_count(others, "vulnerabilities", SCANNER, now,
                                   "secops.other_open_on_asset", finding=str(finding_id)),
        tickets=tickets,
    )
