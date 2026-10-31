"""Evidence routing: the queries an automated control can name, and the store.

Each ``EvidenceQuery`` is a fixed, parameterised statement over the platform's
own copy of one source system (the connectors keep it current), plus a
deterministic rule turning its measure into a *proposed* result:

- ``at_most``: compliant when the measure is at most ``compliant_max``,
  partially compliant when at most ``partial_max`` (if given), else not;
- ``at_least``: the same with ``compliant_min`` and ``partial_min``.

Thresholds come from the control's ``parameters`` in the library, never from
here: what counts as compliant is Security's decision, not the platform's.

``collect`` runs a control's query under the caller's scope, with the tenant
predicate inside the SQL as well, and appends one row to the evidence store
(``cloudops.evidence``): the query text, its parameters, the raw value, the
retrieval timestamp and a digest of all three. The proposal is written to the
response's ``proposed_result``. It never scores: only an approved ``result``
does (``scoring``), and only a person can approve one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import text

from cloudops_tenancy import ScopedConnection

Direction = Literal["at_most", "at_least"]
Proposed = Literal["compliant", "partially_compliant", "non_compliant"]


class EvidenceParameterError(ValueError):
    pass


def _count(key: str) -> Callable[[Mapping[str, Any]], Decimal | None]:
    def measure(row: Mapping[str, Any]) -> Decimal | None:
        return Decimal(int(row[key]))
    return measure


def _share(part: str, whole: str) -> Callable[[Mapping[str, Any]], Decimal | None]:
    def measure(row: Mapping[str, Any]) -> Decimal | None:
        total = int(row[whole])
        return None if total == 0 else Decimal(int(row[part])) / Decimal(total)
    return measure


@dataclass(frozen=True)
class EvidenceQuery:
    name: str
    version: int
    source_system: str
    description: str
    sql: str
    params: Mapping[str, type]  # what the statement binds, from the control's parameters
    measure: Callable[[Mapping[str, Any]], Decimal | None]
    direction: Direction

    @property
    def query_text(self) -> str:
        return re.sub(r"\s+", " ", self.sql).strip()

    def check_parameters(self, parameters: Mapping[str, Any], code: str) -> None:
        for key, kind in self.params.items():
            value = parameters.get(key)
            if kind is list:
                ok = (isinstance(value, list) and bool(value)
                      and all(isinstance(v, str) and v for v in value))
            elif kind is int:
                ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else:
                ok = isinstance(value, str) and bool(value.strip())
            if not ok:
                raise EvidenceParameterError(f"{code}: {self.name} needs parameter {key!r}")
        first, second = (("compliant_max", "partial_max") if self.direction == "at_most"
                         else ("compliant_min", "partial_min"))
        for key in (first, second):
            value = parameters.get(key)
            if key == first and value is None:
                raise EvidenceParameterError(f"{code}: {self.name} needs threshold {key!r}")
            if value is not None and (isinstance(value, bool)
                                      or not isinstance(value, int | float | str)):
                raise EvidenceParameterError(f"{code}: threshold {key!r} must be a number")
        allowed = {*self.params, first, second}
        unknown = sorted(set(parameters) - allowed)
        if unknown:
            raise EvidenceParameterError(f"{code}: unknown parameters {', '.join(unknown)}")

    def evaluate(self, measure: Decimal | None, parameters: Mapping[str, Any]
                 ) -> tuple[Proposed | None, str]:
        if measure is None:
            return None, "nothing to measure"
        if self.direction == "at_most":
            full, part = parameters["compliant_max"], parameters.get("partial_max")
            if measure <= Decimal(str(full)):
                return "compliant", f"{measure} is at most {full}"
            if part is not None and measure <= Decimal(str(part)):
                return "partially_compliant", f"{measure} is above {full}, at most {part}"
            return "non_compliant", f"{measure} is above {part if part is not None else full}"
        full, part = parameters["compliant_min"], parameters.get("partial_min")
        if measure >= Decimal(str(full)):
            return "compliant", f"{measure} is at least {full}"
        if part is not None and measure >= Decimal(str(part)):
            return "partially_compliant", f"{measure} is below {full}, at least {part}"
        return "non_compliant", f"{measure} is below {part if part is not None else full}"


def _tags_share(provider: str) -> EvidenceQuery:
    return EvidenceQuery(
        name=f"{provider}.required_tags_share", version=1, source_system=provider,
        description=f"Share of live {provider} resources carrying every required tag",
        sql=f"""
            SELECT count(*) AS live,
                   count(*) FILTER (WHERE r.tags ?& CAST(:required_tags AS text[])) AS tagged
            FROM cloudops.resource r
            JOIN cloudops.cloud_account a ON a.id = r.account_id
            WHERE r.tenant_id = :tenant AND r.deleted_at IS NULL AND a.provider = '{provider}'
        """,  # noqa: S608 - provider is one of two module constants
        params={"required_tags": list}, measure=_share("tagged", "live"), direction="at_least",
    )


_QUERY_LIST: Final = (
    EvidenceQuery(
        name="qualys.open_vulnerabilities_over_age", version=1, source_system="qualys",
        description="Open vulnerabilities at or above a severity, older than an age",
        sql="""
            SELECT count(*) AS open_over_age, min(v.first_detected) AS oldest
            FROM cloudops.vulnerability v
            WHERE v.tenant_id = :tenant AND v.remediated_at IS NULL
              AND v.severity >= :min_severity
              AND v.first_detected < CAST(:as_of AS timestamptz)
                                     - make_interval(days => :max_age_days)
        """,
        params={"min_severity": int, "max_age_days": int},
        measure=_count("open_over_age"), direction="at_most",
    ),
    EvidenceQuery(
        name="entra.role_holders", version=1, source_system="entra",
        description="Active assignments of a directory role",
        sql="""
            SELECT count(*) AS holders, count(DISTINCT a.directory_id) AS directories
            FROM cloudops.directory_role_assignment a
            WHERE a.tenant_id = :tenant AND a.removed_at IS NULL AND a.role_name = :role_name
        """,
        params={"role_name": str}, measure=_count("holders"), direction="at_most",
    ),
    _tags_share("azure"),
    _tags_share("aws"),
    EvidenceQuery(
        name="servicenow.incident_sla_attainment", version=1, source_system="servicenow",
        description="Share of incidents closed in a window that met their SLA",
        sql="""
            SELECT count(*) FILTER (WHERE w.made_sla) AS met,
                   count(*) FILTER (WHERE w.made_sla IS NOT NULL) AS measured
            FROM cloudops.work_item w
            WHERE w.tenant_id = :tenant AND w.kind = 'incident'
              AND w.closed_at >= CAST(:as_of AS timestamptz) - make_interval(days => :window_days)
              AND w.closed_at < CAST(:as_of AS timestamptz)
        """,
        params={"window_days": int}, measure=_share("met", "measured"), direction="at_least",
    ),
    EvidenceQuery(
        name="sharepoint.current_documents", version=1, source_system="sharepoint",
        description="Current documents of a kind, modified within an age",
        sql="""
            SELECT count(*) AS documents,
                   max(coalesce(d.modified_at, d.ingested_at)) AS latest
            FROM cloudops.document d
            WHERE d.tenant_id = :tenant AND d.deleted_at IS NULL AND d.doc_kind = :doc_kind
              AND coalesce(d.modified_at, d.ingested_at)
                  > CAST(:as_of AS timestamptz) - make_interval(days => :max_age_days)
        """,
        params={"doc_kind": str, "max_age_days": int},
        measure=_count("documents"), direction="at_least",
    ),
    EvidenceQuery(
        name="corestack.open_findings", version=1, source_system="corestack",
        description="Open CoreStack findings in the given categories",
        sql="""
            SELECT count(*) AS open_findings
            FROM cloudops.cost_finding f
            WHERE f.tenant_id = :tenant AND f.status = 'open'
              AND f.category = ANY(CAST(:categories AS text[]))
        """,
        params={"categories": list}, measure=_count("open_findings"), direction="at_most",
    ),
)
QUERIES: Final[Mapping[str, EvidenceQuery]] = {q.name: q for q in _QUERY_LIST}


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=_json_value)


@dataclass(frozen=True)
class Collected:
    evidence_id: UUID
    control_code: str
    query_name: str
    raw_value: dict[str, Any]
    proposed: Proposed | None
    basis: str
    retrieved_at: datetime


_INSERT_EVIDENCE: Final = text(
    "INSERT INTO cloudops.evidence (tenant_id, assessment_id, control_code, source_system,"
    " query_name, query_ref, query_text, parameters, raw_value, retrieved_at, retrieved_by,"
    " sha256) VALUES (:tenant, :assessment, :code, :source, :name, :ref, :text,"
    " CAST(:params AS jsonb), CAST(:raw AS jsonb), :at, :by, :sha) RETURNING id"
)
_PROPOSE: Final = text(
    "UPDATE cloudops.control_response SET proposed_result = :proposed, proposed_basis = :basis"
    " WHERE assessment_id = :assessment AND control_code = :code"
)


async def collect(conn: ScopedConnection, *, tenant_id: UUID, assessment_id: UUID,
                  control_code: str, query_name: str, parameters: Mapping[str, Any],
                  retrieved_at: datetime, retrieved_by: str) -> Collected:
    """Run one control's query, store what came back, and propose a result."""
    query = QUERIES[query_name]
    query.check_parameters(parameters, control_code)
    binds: dict[str, Any] = {k: parameters[k] for k in query.params}
    binds.update(tenant=tenant_id, as_of=retrieved_at)
    found: Mapping[str, Any] = dict((await conn.execute(text(query.sql), binds)).mappings().one())
    raw = {k: _json_value(v) for k, v in found.items()}
    measure = query.measure(found)
    proposed, basis = query.evaluate(measure, parameters)
    stored_params = {k: parameters[k] for k in sorted(parameters)}
    ref = f"{query.name}.v{query.version}:" + hashlib.sha256(
        canonical(stored_params).encode()).hexdigest()[:16]
    digest = hashlib.sha256(canonical({
        "query_ref": ref, "query_text": query.query_text, "parameters": stored_params,
        "raw_value": raw, "retrieved_at": retrieved_at,
    }).encode()).hexdigest()
    evidence_id = await conn.scalar(_INSERT_EVIDENCE, {
        "tenant": tenant_id, "assessment": assessment_id, "code": control_code,
        "source": query.source_system, "name": query.name, "ref": ref, "text": query.query_text,
        "params": canonical(stored_params), "raw": canonical(raw), "at": retrieved_at,
        "by": retrieved_by, "sha": digest,
    })
    await conn.execute(_PROPOSE, {"proposed": proposed, "basis": basis,
                                  "assessment": assessment_id, "code": control_code})
    return Collected(evidence_id, control_code, query.name, raw, proposed, basis, retrieved_at)


def describe(queries: Sequence[EvidenceQuery] = _QUERY_LIST) -> list[dict[str, Any]]:
    """The routing table, for documentation and the console."""
    return [{"name": q.name, "source_system": q.source_system, "description": q.description,
             "direction": q.direction, "parameters": sorted(q.params)} for q in queries]
