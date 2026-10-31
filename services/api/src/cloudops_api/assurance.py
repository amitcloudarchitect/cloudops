"""Assurance endpoints: the control library, assessments, evidence, approval and
the signed evidence pack (step 17, ADR 0020; HLD M14, M15).

The order of things, and who may do each:

1. ``assurance.library`` loads a framework's control library (validated by
   ``cloudops_domain.assurance.library``, written by ``access.load_controls``).
2. ``assurance.manage`` opens an assessment for one customer: one response per
   active control. It collects evidence for the automated controls (each
   query, raw value and timestamp is stored; a result is *proposed*), and
   requests drafts for the narrative ones (the drafting worker writes them).
   It edits a response's narrative, starting from the draft or not.
3. ``assurance.approve`` approves a result. Only then does it score. Editing
   an approved narrative withdraws the approval. A compliant result needs a
   narrative and either collected evidence or a citation: nobody, model or
   person, marks a control compliant on nothing.
4. ``assurance.manage`` exports the signed evidence pack.

Staff only. Assessments are tenant-wide (migration 0013): a subscription-limited
scope sees none. An assessment outside scope is a 404, like one that never
existed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from cloudops_api.access import Access
from cloudops_api.bff import TenantView
from cloudops_api.pipeline import (
    ApiError,
    AuditedRoute,
    audit_only,
    current_access,
    platform,
    requires,
)
from cloudops_api.provenance import ApiModel, Metric, query_ref
from cloudops_domain.assurance import evidence as routing
from cloudops_domain.assurance.library import LOAD, ControlLibrary
from cloudops_domain.assurance.pack import SigningUnavailableError, build_manifest
from cloudops_domain.assurance.scoring import Score, ScoredResponse, score
from cloudops_tenancy import ScopedConnection, TenantScope

router = APIRouter(prefix="/v1/assurance", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]
Result = Literal["compliant", "partially_compliant", "non_compliant", "not_applicable", "manual"]
AssessmentKind = Literal["automated", "narrative", "manual"]
DraftState = Literal["none", "requested", "drafting", "drafted", "declined", "failed"]
SOURCE: Final = "cloudops_assurance"
# A manual control is answered by a person outside the platform and never
# scores; the others score once approved.
ALLOWED_RESULTS: Final = {
    "manual": frozenset({"manual", "not_applicable"}),
    "automated": frozenset({"compliant", "partially_compliant", "non_compliant",
                            "not_applicable"}),
    "narrative": frozenset({"compliant", "partially_compliant", "non_compliant",
                            "not_applicable"}),
}
COLLECTOR: Final = "service:evidence-collector"


def _staff(access: Access) -> None:
    if access.kind != "staff":
        raise ApiError(404, "not_found")


def _narrow(access: Access, tenant: UUID) -> TenantScope:
    # Tenant-wide objects: a subscription-limited scope sees none, so it cannot
    # create one either (migration 0013's account_scope policy).
    if tenant not in access.scope.tenant_ids or access.scope.account_ids is not None:
        raise ApiError(404, "not_found")
    return TenantScope([tenant], access.scope.role, access.scope.principal_id,
                       account_ids=access.scope.account_ids)


def _metric_int(value: int, unit: str, name: str, at: datetime, **p: object) -> Metric[int]:
    return Metric[int](value=value, unit=unit, source_system=SOURCE, as_of=at,
                       query_ref=query_ref(name, 1, **p), is_modelled=False)


# --- views ------------------------------------------------------------------------------


class ControlView(ApiModel):
    code: str
    framework: str
    domain: str
    title: str
    requirement: str
    assessment: AssessmentKind
    evidence_source: str
    evidence_query: str | None
    library_version: str


class FrameworkView(ApiModel):
    framework: str
    library_version: str
    controls: Metric[int]
    automated: Metric[int]
    narrative: Metric[int]
    manual: Metric[int]


class LoadResult(ApiModel):
    framework: str
    library_version: str
    loaded: Metric[int]


class RoutingView(ApiModel):
    name: str
    source_system: str
    description: str
    direction: str
    parameters: list[str]


class ScoreView(ApiModel):
    percent: Metric[Decimal] | None  # None when nothing has been approved to score
    scorable: Metric[int]
    pending: Metric[int]
    excluded: Metric[int]
    counts: dict[str, Metric[int]]


class AssessmentSummary(ApiModel):
    id: UUID
    tenant: TenantView
    framework: str
    library_version: str
    title: str
    status: Literal["open", "closed"]
    created_at: datetime
    score: ScoreView


class EvidenceView(ApiModel):
    id: UUID
    source_system: str
    query_name: str
    query_ref: str
    query_text: str
    parameters: dict[str, Any]
    raw_value: dict[str, Any]
    retrieved_at: datetime
    retrieved_by: str
    sha256: str


class ResponseView(ApiModel):
    control: ControlView
    proposed_result: Literal["compliant", "partially_compliant", "non_compliant"] | None
    proposed_basis: str | None
    draft_state: DraftState
    draft_narrative: str | None
    draft_citations: list[dict[str, Any]]
    draft_sufficiency: Literal["sufficient", "partial", "insufficient"] | None
    drafted_at: datetime | None
    draft_note: str | None  # why a draft was declined, as a code
    narrative: str | None
    citations: list[dict[str, Any]]
    result: Result | None
    approved_by: UUID | None
    approved_at: datetime | None
    approval_note: str | None
    evidence: list[EvidenceView]


class AssessmentDetail(AssessmentSummary):
    responses: list[ResponseView]


class AssessmentRequest(ApiModel):
    tenant_id: UUID
    framework: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    title: str = Field(min_length=1, max_length=200)


class CollectedView(ApiModel):
    control_code: str
    query_name: str
    proposed_result: Literal["compliant", "partially_compliant", "non_compliant"] | None
    basis: str
    evidence_id: UUID


class ControlNarrativeEdit(ApiModel):
    narrative: str = Field(min_length=1, max_length=8000)
    # Keep the draft's citations (the person checked them), or none.
    keep_draft_citations: bool = False


class Approval(ApiModel):
    result: Result
    # The narrative the approver read (null: none). If it has changed since, the
    # approval is refused: nobody approves text they never saw.
    narrative_seen: str | None = Field(max_length=8000)
    note: str | None = Field(default=None, max_length=2000)


class EvidencePackSummary(ApiModel):
    id: UUID
    framework: str
    manifest_sha256: str
    signature_alg: str
    key_id: str
    created_by: UUID
    created_at: datetime


# --- queries -----------------------------------------------------------------------------

_CONTROL_COLS: Final = ("code, framework, domain, title, requirement, assessment,"
                        " evidence_source, evidence_query, library_version")
_ASSESSMENT: Final = text(
    "SELECT a.id, a.tenant_id, t.canonical_name, a.framework, a.library_version, a.title,"
    " a.status, a.created_at FROM cloudops.assessment a"
    " JOIN cloudops.tenant t ON t.id = a.tenant_id WHERE a.id = :id"
)
_RESPONSES: Final = text(
    "SELECT r.control_code, r.proposed_result, r.proposed_basis, r.draft_state,"
    " r.draft_narrative, r.draft_citations, r.draft_sufficiency, r.drafted_at, r.narrative,"
    " r.citations, r.result, r.approved_by, r.approved_at, r.approval_note,"
    " c.code, c.framework, c.domain, c.title, c.requirement, c.assessment, c.evidence_source,"
    " c.evidence_query, c.library_version, r.draft_note"
    " FROM cloudops.control_response r JOIN cloudops.control c ON c.id = r.control_id"
    " WHERE r.assessment_id = :id ORDER BY c.domain, c.code"
)
_EVIDENCE: Final = text(
    "SELECT id, control_code, source_system, query_name, query_ref, query_text, parameters,"
    " raw_value, retrieved_at, retrieved_by, sha256 FROM cloudops.evidence"
    " WHERE assessment_id = :id ORDER BY control_code, retrieved_at DESC, id"
)


def _control(row: tuple[Any, ...]) -> ControlView:
    return ControlView(code=row[0], framework=row[1], domain=row[2], title=row[3],
                       requirement=row[4], assessment=row[5], evidence_source=row[6],
                       evidence_query=row[7], library_version=row[8])


def _score_view(s: Score, at: datetime, assessment: UUID) -> ScoreView:
    def m(v: int, unit: str, name: str) -> Metric[int]:
        return _metric_int(v, unit, f"assurance.{name}", at, assessment=str(assessment))

    return ScoreView(
        percent=None if s.percent is None else Metric[Decimal](
            value=s.percent, unit="percent", source_system=SOURCE, as_of=at,
            query_ref=query_ref("assurance.score", 1, assessment=str(assessment)),
            is_modelled=False),
        scorable=m(s.scorable, "controls", "scorable"),
        pending=m(s.pending, "controls", "pending"),
        excluded=m(s.excluded, "controls", "excluded"),
        counts={k: m(v, "controls", f"count.{k}") for k, v in s.counts.items()},
    )


async def _score(conn: ScopedConnection, assessment: UUID) -> Score:
    rows = [tuple(r) for r in await conn.execute(text(
        "SELECT control_code, result, approved_at IS NOT NULL FROM cloudops.control_response"
        " WHERE assessment_id = :id"), {"id": assessment})]
    return score(ScoredResponse(r[0], r[1], bool(r[2])) for r in rows)


async def _load_assessment(conn: ScopedConnection, assessment: UUID) -> tuple[Any, ...]:
    found = (await conn.execute(_ASSESSMENT, {"id": assessment})).one_or_none()
    if found is None:
        raise ApiError(404, "not_found")
    return tuple(found)


def _summary(row: tuple[Any, ...], s: Score, at: datetime) -> AssessmentSummary:
    return AssessmentSummary(
        id=row[0], tenant=TenantView(id=row[1], name=row[2]), framework=row[3],
        library_version=row[4], title=row[5], status=row[6], created_at=row[7],
        score=_score_view(s, at, row[0]),
    )


# --- the library -----------------------------------------------------------------------


@router.get("/frameworks", response_model=list[FrameworkView])
@requires("assurance.read")
async def list_frameworks(request: Request, access: AccessDep) -> list[FrameworkView]:
    _staff(access)
    now = datetime.now(UTC)
    async with platform(request).db.transaction(access.scope) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT framework, max(library_version), count(*),"
            " count(*) FILTER (WHERE assessment = 'automated'),"
            " count(*) FILTER (WHERE assessment = 'narrative'),"
            " count(*) FILTER (WHERE assessment = 'manual')"
            " FROM cloudops.control WHERE retired_at IS NULL GROUP BY framework ORDER BY 1"))]
    return [FrameworkView(
        framework=r[0], library_version=r[1],
        controls=_metric_int(int(r[2]), "controls", "assurance.controls", now, f=r[0]),
        automated=_metric_int(int(r[3]), "controls", "assurance.automated", now, f=r[0]),
        narrative=_metric_int(int(r[4]), "controls", "assurance.narrative", now, f=r[0]),
        manual=_metric_int(int(r[5]), "controls", "assurance.manual", now, f=r[0]),
    ) for r in rows]


@router.get("/frameworks/{framework}/controls", response_model=list[ControlView])
@requires("assurance.read")
async def list_controls(request: Request, framework: str, access: AccessDep
                        ) -> list[ControlView]:
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            f"SELECT {_CONTROL_COLS} FROM cloudops.control"  # noqa: S608 - module constant
            " WHERE framework = :f AND retired_at IS NULL ORDER BY domain, code"),
            {"f": framework})]
    return [_control(r) for r in rows]


@router.post("/library", response_model=LoadResult, status_code=201)
@requires("assurance.library")
async def load_library(request: Request, body: ControlLibrary, access: AccessDep
                       ) -> LoadResult:
    _staff(access)
    try:
        async with platform(request).db.transaction(access.scope) as conn:
            loaded = await conn.scalar(LOAD, body.load_params(access.identity.oid))
    except DBAPIError as exc:
        # A version's text is fixed: other text under a loaded version is refused.
        if str(getattr(exc.orig, "sqlstate", "")) == "23505":
            raise ApiError(409, "library_version_differs") from None
        raise
    return LoadResult(framework=body.framework, library_version=body.version,
                      loaded=_metric_int(int(loaded or 0), "controls", "assurance.loaded",
                                         datetime.now(UTC), f=body.framework))


@router.get("/evidence-routing", response_model=list[RoutingView])
@requires("assurance.read")
async def evidence_routing(request: Request, access: AccessDep) -> list[RoutingView]:
    _staff(access)
    return [RoutingView(**q) for q in routing.describe()]


# --- assessments -----------------------------------------------------------------------


@router.post("/assessments", response_model=AssessmentSummary, status_code=201)
@requires("assurance.manage")
async def open_assessment(request: Request, body: AssessmentRequest, access: AccessDep
                          ) -> AssessmentSummary:
    _staff(access)
    scope = _narrow(access, body.tenant_id)
    async with platform(request).db.transaction(scope) as conn:
        version = await conn.scalar(text(
            # The framework's current version: load_controls keeps exactly one.
            "SELECT max(library_version) FROM cloudops.control"
            " WHERE framework = :f AND retired_at IS NULL"), {"f": body.framework})
        if version is None:
            raise ApiError(404, "framework_not_loaded")
        created = await conn.scalar(text(
            "INSERT INTO cloudops.assessment (tenant_id, framework, library_version, title,"
            " created_by) VALUES (:t, :f, :v, :title, :by) RETURNING id"),
            {"t": body.tenant_id, "f": body.framework, "v": version, "title": body.title,
             "by": access.identity.oid})
        await conn.execute(text(
            "INSERT INTO cloudops.control_response (tenant_id, assessment_id, control_id,"
            " control_code) SELECT :t, :a, id, code FROM cloudops.control"
            " WHERE framework = :f AND library_version = :v"),
            {"t": body.tenant_id, "a": created, "f": body.framework, "v": version})
        row = await _load_assessment(conn, created)
        s = await _score(conn, created)
    audit_only(request, [body.tenant_id])
    return _summary(row, s, datetime.now(UTC))


@router.get("/assessments", response_model=list[AssessmentSummary])
@requires("assurance.read")
async def list_assessments(request: Request, access: AccessDep) -> list[AssessmentSummary]:
    _staff(access)
    now = datetime.now(UTC)
    async with platform(request).db.transaction(access.scope) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT a.id, a.tenant_id, t.canonical_name, a.framework, a.library_version,"
            " a.title, a.status, a.created_at FROM cloudops.assessment a"
            " JOIN cloudops.tenant t ON t.id = a.tenant_id"
            " ORDER BY a.created_at DESC LIMIT 200"))]
        scores = {r[0]: await _score(conn, r[0]) for r in rows}
    return [_summary(r, scores[r[0]], now) for r in rows]


@router.get("/assessments/{assessment_id}", response_model=AssessmentDetail)
@requires("assurance.read")
async def get_assessment(request: Request, assessment_id: UUID, access: AccessDep
                         ) -> AssessmentDetail:
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        row = await _load_assessment(conn, assessment_id)
        responses = [tuple(r) for r in await conn.execute(_RESPONSES, {"id": assessment_id})]
        evidence = [tuple(r) for r in await conn.execute(_EVIDENCE, {"id": assessment_id})]
        s = await _score(conn, assessment_id)
    audit_only(request, [row[1]])
    by_control: dict[str, list[EvidenceView]] = {}
    for e in evidence:
        by_control.setdefault(e[1], []).append(EvidenceView(
            id=e[0], source_system=e[2], query_name=e[3], query_ref=e[4], query_text=e[5],
            parameters=e[6], raw_value=e[7], retrieved_at=e[8], retrieved_by=e[9],
            sha256=e[10]))
    summary = _summary(row, s, datetime.now(UTC))
    return AssessmentDetail(**summary.model_dump(), responses=[ResponseView(
        control=_control(r[14:23]), proposed_result=r[1], proposed_basis=r[2], draft_state=r[3],
        draft_narrative=r[4], draft_citations=list(r[5] or []), draft_sufficiency=r[6],
        drafted_at=r[7], draft_note=r[23], narrative=r[8], citations=list(r[9] or []), result=r[10],
        approved_by=r[11], approved_at=r[12], approval_note=r[13],
        evidence=by_control.get(r[0], []),
    ) for r in responses])


async def _open(conn: ScopedConnection, assessment: UUID) -> UUID:
    row = await _load_assessment(conn, assessment)
    if row[6] != "open":
        raise ApiError(409, "assessment_closed")
    return UUID(str(row[1]))


@router.post("/assessments/{assessment_id}/collect", response_model=list[CollectedView])
@requires("assurance.manage")
async def collect_evidence(request: Request, assessment_id: UUID, access: AccessDep
                           ) -> list[CollectedView]:
    """Run every automated control's evidence query now, and store what came back."""
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        tenant = await _open(conn, assessment_id)
    scope = _narrow(access, tenant)
    now = datetime.now(UTC)
    out: list[CollectedView] = []
    async with platform(request).db.transaction(scope) as conn:
        controls = [tuple(r) for r in await conn.execute(text(
            "SELECT c.code, c.evidence_query, c.parameters FROM cloudops.control_response r"
            " JOIN cloudops.control c ON c.id = r.control_id"
            " WHERE r.assessment_id = :a AND c.assessment = 'automated' ORDER BY c.code"),
            {"a": assessment_id})]
        for code, query_name, parameters in controls:
            got = await routing.collect(
                conn, tenant_id=tenant, assessment_id=assessment_id, control_code=code,
                query_name=query_name, parameters=parameters, retrieved_at=now,
                retrieved_by=f"{COLLECTOR}:{access.identity.principal_id}")
            out.append(CollectedView(control_code=code, query_name=query_name,
                                     proposed_result=got.proposed, basis=got.basis,
                                     evidence_id=got.evidence_id))
    audit_only(request, [tenant])
    return out


_REQUEST_DRAFT: Final = text(
    "UPDATE cloudops.control_response r SET draft_state = 'requested'"
    " FROM cloudops.control c WHERE c.id = r.control_id AND r.assessment_id = :a"
    " AND c.assessment = 'narrative' AND r.approved_at IS NULL"
    " AND r.draft_state IN ('none', 'drafted', 'declined', 'failed')"
    " AND (CAST(:code AS text) IS NULL OR r.control_code = :code)"
)


@router.post("/assessments/{assessment_id}/drafts", response_model=Metric[int],
             status_code=202)
@requires("assurance.manage")
async def request_drafts(request: Request, assessment_id: UUID, access: AccessDep,
                         control: str | None = None) -> Metric[int]:
    """Ask the drafting worker for drafts of the narrative controls (all, or one)."""
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        tenant = await _open(conn, assessment_id)
        requested = (await conn.execute(_REQUEST_DRAFT, {"a": assessment_id, "code": control}
                                        )).rowcount
    audit_only(request, [tenant])
    return _metric_int(int(requested), "drafts", "assurance.drafts_requested",
                       datetime.now(UTC), assessment=str(assessment_id))


@router.post("/assessments/{assessment_id}/responses/{code}/narrative",
             response_model=ResponseView)
@requires("assurance.manage")
async def edit_narrative(request: Request, assessment_id: UUID, code: str,
                         body: ControlNarrativeEdit, access: AccessDep) -> ResponseView:
    """A person's narrative. Editing an approved response withdraws its approval."""
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        tenant = await _open(conn, assessment_id)
        updated = await conn.scalar(text(
            "UPDATE cloudops.control_response SET narrative = :n,"
            " citations = CASE WHEN :keep THEN coalesce(draft_citations, '[]'::jsonb)"
            "                  ELSE '[]'::jsonb END,"
            " result = NULL, approved_by = NULL, approved_at = NULL, approval_note = NULL"
            " WHERE assessment_id = :a AND control_code = :c RETURNING id"),
            {"n": body.narrative.strip(), "keep": body.keep_draft_citations,
             "a": assessment_id, "c": code})
        if updated is None:
            raise ApiError(404, "not_found")
    audit_only(request, [tenant])
    return await _one_response(request, access, assessment_id, code)


@router.post("/assessments/{assessment_id}/responses/{code}/approve",
             response_model=ResponseView)
@requires("assurance.approve")
async def approve_response(request: Request, assessment_id: UUID, code: str, body: Approval,
                           access: AccessDep) -> ResponseView:
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        tenant = await _open(conn, assessment_id)
        found = (await conn.execute(text(
            "SELECT c.assessment, r.narrative, jsonb_array_length(r.citations),"
            " (SELECT count(*) FROM cloudops.evidence e WHERE e.assessment_id = r.assessment_id"
            "   AND e.control_code = r.control_code)"
            " FROM cloudops.control_response r JOIN cloudops.control c ON c.id = r.control_id"
            " WHERE r.assessment_id = :a AND r.control_code = :c FOR UPDATE OF r"),
            {"a": assessment_id, "c": code})).one_or_none()
        if found is None:
            raise ApiError(404, "not_found")
        kind, narrative, cited, collected = tuple(found)
        if (narrative or "").strip() != (body.narrative_seen or "").strip():
            raise ApiError(409, "response_changed")
        if body.result not in ALLOWED_RESULTS[kind]:
            raise ApiError(422, "result_not_allowed_for_control")
        if body.result == "compliant" and (not (narrative or "").strip()
                                           or (int(cited) == 0 and int(collected) == 0)):
            # Nobody marks a control compliant on nothing: a narrative, and
            # collected evidence or a citation behind it.
            raise ApiError(422, "compliant_needs_narrative_and_evidence")
        await conn.execute(text(
            "UPDATE cloudops.control_response SET result = :r, approved_by = :by,"
            " approved_at = now(), approval_note = :note"
            " WHERE assessment_id = :a AND control_code = :c"),
            {"r": body.result, "by": access.identity.oid, "note": body.note,
             "a": assessment_id, "c": code})
    audit_only(request, [tenant])
    return await _one_response(request, access, assessment_id, code)


async def _one_response(request: Request, access: Access, assessment: UUID, code: str
                        ) -> ResponseView:
    detail = await get_assessment(request, assessment, access)
    for r in detail.responses:
        if r.control.code == code:
            return r
    raise ApiError(404, "not_found")


# --- the evidence pack ------------------------------------------------------------------


@router.post("/assessments/{assessment_id}/evidence-packs", response_model=dict[str, Any],
             status_code=201)
@requires("assurance.manage")
async def export_pack(request: Request, assessment_id: UUID, access: AccessDep
                      ) -> dict[str, Any]:
    """Build, sign and record the evidence pack. Returns the signed document."""
    _staff(access)
    signer = platform(request).signer
    now = datetime.now(UTC)
    async with platform(request).db.transaction(access.scope) as conn:
        row = await _load_assessment(conn, assessment_id)
        responses = [tuple(r) for r in await conn.execute(_RESPONSES, {"id": assessment_id})]
        evidence = [tuple(r) for r in await conn.execute(_EVIDENCE, {"id": assessment_id})]
        s = await _score(conn, assessment_id)
        manifest = build_manifest(
            assessment={"id": str(row[0]), "customer": row[2], "framework": row[3],
                        "library_version": row[4], "title": row[5], "status": row[6],
                        "created_at": row[7].isoformat()},
            responses=[{
                "control_code": r[0], "library_version": r[22], "title": r[17],
                "requirement": r[18], "assessment": r[19],
                "evidence_source": r[20], "result": r[10],
                "approved_by": None if r[11] is None else str(r[11]),
                "approved_at": None if r[12] is None else r[12].isoformat(),
                "approval_note": r[13], "narrative": r[8], "citations": r[9] or [],
                "proposed_result": r[1], "proposed_basis": r[2],
            } for r in responses],
            evidence=[{
                "id": str(e[0]), "control_code": e[1], "source_system": e[2],
                "query_name": e[3], "query_ref": e[4], "query_text": e[5], "parameters": e[6],
                "raw_value": e[7], "retrieved_at": e[8].isoformat(), "retrieved_by": e[9],
                "sha256": e[10],
            } for e in evidence],
            score=s, exported_at=now, exported_by=access.identity.oid,
        )
        try:
            signed = signer.sign(manifest)
        except SigningUnavailableError:
            raise ApiError(503, "signing_unavailable") from None
        await conn.execute(text(
            "INSERT INTO cloudops.evidence_pack (tenant_id, assessment_id, framework, manifest,"
            " manifest_sha256, signature, signature_alg, key_id, created_by) VALUES"
            " (:t, :a, :f, CAST(:m AS jsonb), :sha, :sig, :alg, :key, :by)"),
            {"t": row[1], "a": assessment_id, "f": row[3], "m": json.dumps(signed.manifest),
             "sha": signed.manifest_sha256, "sig": signed.signature, "alg": signed.algorithm,
             "key": signed.key_id, "by": access.identity.oid})
    audit_only(request, [row[1]])
    return signed.as_document()


@router.get("/assessments/{assessment_id}/evidence-packs", response_model=list[EvidencePackSummary])
@requires("assurance.read")
async def list_packs(request: Request, assessment_id: UUID, access: AccessDep
                     ) -> list[EvidencePackSummary]:
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        row = await _load_assessment(conn, assessment_id)
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT id, framework, manifest_sha256, signature_alg, key_id, created_by, created_at"
            " FROM cloudops.evidence_pack WHERE assessment_id = :a ORDER BY created_at DESC"),
            {"a": assessment_id})]
    audit_only(request, [row[1]])
    return [EvidencePackSummary(id=r[0], framework=r[1], manifest_sha256=r[2], signature_alg=r[3],
                        key_id=r[4], created_by=r[5], created_at=r[6]) for r in rows]


@router.get("/evidence-packs/{pack_id}", response_model=dict[str, Any])
@requires("assurance.read")
async def get_pack(request: Request, pack_id: UUID, access: AccessDep) -> dict[str, Any]:
    """The pack exactly as signed: verifiable with the signing key's public half."""
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        found = (await conn.execute(text(
            "SELECT tenant_id, manifest, manifest_sha256, signature, signature_alg, key_id"
            " FROM cloudops.evidence_pack WHERE id = :id"), {"id": pack_id})).one_or_none()
    if found is None:
        raise ApiError(404, "not_found")
    tenant, manifest, digest, signature, alg, key = tuple(found)
    audit_only(request, [tenant])
    return {"manifest": manifest, "manifest_sha256": digest,
            "signature": {"algorithm": alg, "key_id": key, "value": signature}}
