"""Knowledge and document intelligence endpoints (step 17, ADR 0020; HLD M20).

- ``GET /v1/knowledge/documents``  current design, handover, runbook and policy
  documents, each with whether the live estate has superseded it, and why
- ``GET /v1/knowledge/gaps``       customers with no current design document or
  handover pack
- ``GET /v1/knowledge/chunks/{id}/citation``  the source document and page of a
  retrieved passage

Staff only, under ``knowledge.read``: a customer's view of its own documents is
the portal's decision (step 18). Everything runs under the caller's scope, so
row-level security decides which customers exist; the gap list names only
customers in scope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from cloudops_api.access import Access
from cloudops_api.bff import TenantView
from cloudops_api.pipeline import ApiError, AuditedRoute, current_access, platform, requires
from cloudops_api.provenance import ApiModel, Metric, query_ref
from cloudops_domain import knowledge

router = APIRouter(prefix="/v1/knowledge", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]
DocKind = Literal["design", "handover", "runbook", "policy"]
Coverage = Literal["current", "stale", "missing"]
StaleReason = Literal["removed_resources", "estate_changed", "not_updated"]


def _staff(access: Access) -> None:
    if access.kind != "staff":
        raise ApiError(404, "not_found")


def _count(value: int, unit: str, name: str, at: datetime, **params: object) -> Metric[int]:
    return Metric[int](value=value, unit=unit, source_system="sharepoint", as_of=at,
                       query_ref=query_ref(name, 1, **params), is_modelled=False)


class DocumentView(ApiModel):
    id: UUID
    tenant: TenantView
    name: str
    doc_kind: DocKind
    web_url: str | None
    modified_at: datetime | None
    stale: bool
    reasons: list[StaleReason]
    removed_resources: list[str]
    resources_named: Metric[int]
    resources_added_since: Metric[int]


class DocumentList(ApiModel):
    items: list[DocumentView]
    stale: Metric[int]
    rules: dict[str, str]


class CoverageView(ApiModel):
    tenant: TenantView
    design: Coverage
    handover: Coverage
    has_gap: bool


class CitationView(ApiModel):
    chunk_id: UUID
    label: str
    source_kind: str
    source_ref: str | None
    document_id: UUID | None
    document_name: str | None
    web_url: str | None
    page: str | None  # a label ("3"), not a measure


RULES = {
    "removed_resources": "names a resource removed from the estate since it was last modified",
    "estate_changed": f"the estate grew by at least "
                      f"{int(knowledge.ESTATE_DRIFT_FRACTION * 100)}% since it was last modified",
    "not_updated": f"not modified for {knowledge.STALE_AFTER_DAYS} days",
}


@router.get("/documents", response_model=DocumentList)
@requires("knowledge.read")
async def list_documents(
    request: Request, access: AccessDep,
    kind: Annotated[DocKind | None, Query()] = None,
    stale: Annotated[bool | None, Query()] = None,
) -> DocumentList:
    _staff(access)
    now = datetime.now(UTC)
    async with platform(request).db.transaction(access.scope) as conn:
        statuses = await knowledge.document_statuses(
            conn, now, (kind,) if kind else knowledge.ASSESSED_KINDS)
    if stale is not None:
        statuses = [s for s in statuses if s.stale == stale]
    items = [DocumentView(
        id=s.document_id, tenant=TenantView(id=s.tenant_id, name=s.tenant_name), name=s.name,
        doc_kind=s.doc_kind,  # type: ignore[arg-type]  # one of ASSESSED_KINDS by the query
        web_url=s.web_url, modified_at=s.modified_at, stale=s.stale,
        reasons=list(s.reasons),  # type: ignore[arg-type]  # from staleness_reasons
        removed_resources=list(s.removed_resources),
        resources_named=_count(s.resources_named, "resources", "knowledge.named", now,
                               document=str(s.document_id)),
        resources_added_since=_count(s.resources_added_since, "resources",
                                     "knowledge.added_since", now,
                                     document=str(s.document_id)),
    ) for s in statuses]
    return DocumentList(
        items=items,
        stale=_count(sum(1 for s in statuses if s.stale), "documents", "knowledge.stale", now,
                     kind=kind),
        rules=RULES,
    )


@router.get("/gaps", response_model=list[CoverageView])
@requires("knowledge.read")
async def coverage_gaps(request: Request, access: AccessDep) -> list[CoverageView]:
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        gaps = await knowledge.coverage_gaps(conn, datetime.now(UTC))
    return [CoverageView(tenant=TenantView(id=g.tenant_id, name=g.tenant_name),
                         design=g.coverage["design"], handover=g.coverage["handover"],
                         has_gap=g.has_gap) for g in gaps]


@router.get("/chunks/{chunk_id}/citation", response_model=CitationView)
@requires("knowledge.read")
async def chunk_citation(request: Request, chunk_id: UUID, access: AccessDep) -> CitationView:
    _staff(access)
    async with platform(request).db.transaction(access.scope) as conn:
        found = (await knowledge.citations(conn, [chunk_id])).get(chunk_id)
    if found is None:
        raise ApiError(404, "not_found")
    return CitationView(
        chunk_id=found.chunk_id, label=found.label(), source_kind=found.source_kind,
        source_ref=found.source_ref, document_id=found.document_id,
        document_name=found.document_name, web_url=found.web_url,
        page=None if found.page is None else str(found.page),
    )
