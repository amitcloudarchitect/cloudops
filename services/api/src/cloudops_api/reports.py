"""Monthly report pack endpoints (step 17, ADR 0020; HLD M19).

    POST /v1/reports/packs                          generate a month (a new version)
    GET  /v1/reports/packs, /v1/reports/packs/{id}  read
    POST /v1/reports/packs/{id}/narratives/{section}        a person's text
    POST /v1/reports/packs/{id}/narratives/{section}/draft  ask for a new AI draft
    POST /v1/reports/packs/{id}/approve, /publish   release, by a person only
    GET  /v1/reports/packs/{id}/pdf                 the pack as printed
    GET  /v1/reports/packs/{id}/text                the same lines as text, for the acceptance diff
    GET  /v1/reports/portfolio                      consolidation in a reporting currency

**Never auto-published.** Generation stores the data snapshot and asks the
drafting worker for narrative. A draft is shown to the editor and never
printed; only a person's text is. A pack is approved and published by an
active staff principal holding ``reports.publish``, and only once every
narrative section has a person's text: the API checks, and a trigger in the
database refuses otherwise.

**Currency.** A pack is in the customer's contract currency, from the SOW in
force for the month (so generating one needs commercial visibility; with no
SOW the currency is "not captured" and nothing is generated). The portfolio is
in the reporting currency the viewer picks, from the same cost rows.

**Access.** ``reports.read`` covers reading, and customers see only published
packs (row-level security, migration 0013). The customer roles CXO and VP or
director hold it (migration 0027, ADR 0030): a customer scope runs as
``cloudops_customer``, granted the pack's snapshot, its price and a section's
published text, so its queries name only those columns. Never the commercial
sections, the AI drafts, or who generated, approved or edited a pack. The
portfolio is staff work: a customer is refused it. A pack outside scope is a
404. Report packs are tenant-wide, so a subscription-limited scope sees none.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field
from sqlalchemy import TextClause, text

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
from cloudops_domain.reporting import portfolio as consolidation
from cloudops_domain.reporting import sections
from cloudops_domain.reporting.pdf import render_pdf
from cloudops_domain.reporting.render import Line, render_lines, text_lines
from cloudops_tenancy import ScopedConnection, TenantScope

router = APIRouter(prefix="/v1/reports", route_class=AuditedRoute)

AccessDep = Annotated[Access, Depends(current_access)]
Status = Literal["draft", "approved", "published", "superseded"]
DraftState = Literal["none", "requested", "drafting", "drafted", "declined", "failed"]
PERIOD: Final = r"^\d{4}-(0[1-9]|1[0-2])$"
CURRENCY: Final = r"^[A-Z]{3}$"


def _period(value: str) -> date:
    year, month = value.split("-")
    return date(int(year), int(month), 1)


def _narrow(access: Access, tenant: UUID) -> TenantScope:
    # Tenant-wide objects: a subscription-limited scope sees none, so it cannot
    # create one either (migration 0013's account_scope policy).
    if tenant not in access.scope.tenant_ids or access.scope.account_ids is not None:
        raise ApiError(404, "not_found")
    return TenantScope([tenant], access.scope.role, access.scope.principal_id,
                       account_ids=access.scope.account_ids)


# --- views ------------------------------------------------------------------------------


class GenerateRequest(ApiModel):
    tenant_id: UUID
    period: str = Field(pattern=PERIOD)


class NarrativeView(ApiModel):
    section: str
    draft_state: DraftState | None  # None for a customer: drafts are staff-only
    draft_text: str | None
    drafted_at: datetime | None
    draft_note: str | None  # why a draft was declined, as a code
    text: str | None
    edited_by: UUID | None
    edited_at: datetime | None


class ReportPackSummary(ApiModel):
    id: UUID
    tenant: TenantView
    period: str
    version: str  # a label ("2"), not a measure
    status: Status
    contract_currency: str
    generated_at: datetime
    approved_at: datetime | None
    published_at: datetime | None


FigureKind = Literal["count", "percent", "days", "hours", "money", "text"]


class FigureSnapshot(ApiModel):
    """A figure as generated: the value is unrounded text (an int, or a Decimal
    in ``unit`` for money); presentation rounds it once."""

    key: str
    label: str
    value: str
    kind: FigureKind
    unit: str
    source_system: str
    query_ref: str
    note: str | None


class TableSnapshot(ApiModel):
    key: str
    title: str
    columns: list[str]
    kinds: list[FigureKind]
    rows: list[list[str]]


class SectionSnapshot(ApiModel):
    key: str
    title: str
    captured: bool
    gap: str | None
    figures: list[FigureSnapshot]
    tables: list[TableSnapshot]


class ReportPackView(ReportPackSummary):
    data_as_of: datetime
    sections: list[SectionSnapshot]  # the generated snapshot, in pack order
    narratives: list[NarrativeView]
    releasable: bool  # every narrative has a person's text


class SectionNarrativeEdit(ApiModel):
    text: str = Field(min_length=1, max_length=6000)


class PortfolioLine(ApiModel):
    tenant: TenantView
    consumption: Metric[Decimal] | None
    gap: str | None
    incidents_opened: Metric[int]
    open_high_vulnerabilities: Metric[int]


class PortfolioView(ApiModel):
    period: str
    reporting_currency: str
    complete: bool
    total_consumption: Metric[Decimal]
    incidents_opened: Metric[int]
    open_high_vulnerabilities: Metric[int]
    customers: list[PortfolioLine]


# --- queries ----------------------------------------------------------------------------

_PRICED: Final = (
    "p.id, p.tenant_id, t.canonical_name, p.period, p.version, p.status, p.contract_currency,"
    " p.generated_at, p.approved_at, p.published_at, p.data_as_of,"
    # The customer's price joins in where price_visibility lets the scope read it
    # (commercial roles; a customer once published, migration 0017), and the
    # commercial sections where commercial_visibility does (0013), each section by
    # section over the one before. Anyone else gets the pack without them.
    " p.sections || coalesce((SELECT r.sections FROM cloudops.report_pack_price r"
    "  WHERE r.pack_id = p.id AND r.tenant_id = p.tenant_id), '{}'::jsonb)"
)
_COMMERCIAL: Final = (
    " || coalesce((SELECT c.sections FROM cloudops.report_pack_commercial c"
    "  WHERE c.pack_id = p.id AND c.tenant_id = p.tenant_id), '{}'::jsonb)"
)
# A customer scope is granted nothing on report_pack_commercial (migration 0027):
# naming it would fail, so its query never does.
_PACK_COLS: Final = _PRICED + _COMMERCIAL


def _pack_cols(staff: bool) -> str:
    return _PACK_COLS if staff else _PRICED


def _pack_sql(staff: bool) -> TextClause:
    return text(
        f"SELECT {_pack_cols(staff)} FROM cloudops.report_pack p"  # noqa: S608 - constants
        " JOIN cloudops.tenant t ON t.id = p.tenant_id WHERE p.id = :id"
    )


_PACK: Final = {True: _pack_sql(True), False: _pack_sql(False)}
_NARRATIVES: Final = {
    True: text(
        "SELECT section, draft_state, draft_text, drafted_at, text, edited_by, edited_at,"
        " draft_note FROM cloudops.report_narrative WHERE pack_id = :id ORDER BY section"),
    # The drafts, their state and who edited a section are not granted to a customer.
    False: text(
        "SELECT section, NULL::text, NULL::text, NULL::timestamptz, text, NULL::uuid,"
        " edited_at, NULL::text FROM cloudops.report_narrative WHERE pack_id = :id"
        " ORDER BY section"),
}
_CONTRACT_CURRENCY: Final = text(
    "SELECT currency FROM cloudops.sow WHERE tenant_id = :t AND effective_from < :e"
    " AND (effective_to IS NULL OR effective_to >= :s) ORDER BY effective_from DESC LIMIT 1"
)


def _summary(row: tuple[Any, ...]) -> ReportPackSummary:
    return ReportPackSummary(
        id=row[0], tenant=TenantView(id=row[1], name=row[2]), period=f"{row[3]:%Y-%m}",
        version=str(row[4]), status=row[5], contract_currency=str(row[6]).strip(),
        generated_at=row[7], approved_at=row[8], published_at=row[9],
    )


async def _load(conn: ScopedConnection, pack: UUID, staff: bool = True) -> tuple[Any, ...]:
    found = (await conn.execute(_PACK[staff], {"id": pack})).one_or_none()
    if found is None:
        raise ApiError(404, "not_found")
    return tuple(found)


async def _view(conn: ScopedConnection, row: tuple[Any, ...], staff: bool) -> ReportPackView:
    narratives = [tuple(r) for r in await conn.execute(_NARRATIVES[staff], {"id": row[0]})]
    return ReportPackView(
        **_summary(row).model_dump(), data_as_of=row[10],
        sections=[SectionSnapshot.model_validate(row[11][k])
                  for k in sections.SECTION_ORDER if k in row[11]],
        narratives=[NarrativeView(
            section=n[0], draft_state=n[1] if staff else None,
            draft_text=n[2] if staff else None, drafted_at=n[3] if staff else None,
            draft_note=n[7] if staff else None,
            text=n[4], edited_by=n[5] if staff else None, edited_at=n[6],
        ) for n in narratives],
        releasable=all(n[4] is not None for n in narratives),
    )


# --- generation -------------------------------------------------------------------------


@router.post("/packs", response_model=ReportPackView, status_code=201)
@requires("reports.manage")
async def generate_pack(request: Request, body: GenerateRequest, access: AccessDep
                        ) -> ReportPackView:
    if access.kind != "staff":
        raise ApiError(404, "not_found")
    scope = _narrow(access, body.tenant_id)
    period = _period(body.period)
    if period > datetime.now(UTC).date():
        raise ApiError(422, "period_in_future")
    start, end = sections.month_bounds(period)
    p = platform(request)
    now = datetime.now(UTC)
    async with p.db.transaction(scope) as conn:
        name = await conn.scalar(text("SELECT canonical_name FROM cloudops.tenant WHERE id = :t"),
                                 {"t": body.tenant_id})
        if name is None:
            raise ApiError(404, "not_found")
        currency = await conn.scalar(_CONTRACT_CURRENCY, {"t": body.tenant_id,
                                                          "s": start.date(), "e": end.date()})
        if currency is None:
            # No SOW in force (or no commercial visibility): "not captured", never a guess.
            raise ApiError(409, "contract_currency_not_captured")
        content = await sections.generate(
            conn, scope, tenant_id=body.tenant_id, tenant_name=str(name), period=period,
            contract_currency=str(currency).strip(), now=now,
            availability_source=p.availability, financial=p.financial)
        # Margin, SOW revenue and Getronics' cloud cost are stored apart, where only
        # commercial roles read them (ADR 0020); the customer's price apart again,
        # where only they and the customer do (ADR 0021): neither in what every
        # reports.read holder reads.
        stored = sections.for_storage(content)
        # A new version supersedes the unpublished ones; a published pack stays
        # published until its successor is.
        await conn.execute(text(
            "UPDATE cloudops.report_pack SET status = 'superseded' WHERE tenant_id = :t"
            " AND period = :p AND status IN ('draft', 'approved')"),
            {"t": body.tenant_id, "p": period})
        version = await conn.scalar(text(
            "SELECT coalesce(max(version), 0) + 1 FROM cloudops.report_pack"
            " WHERE tenant_id = :t AND period = :p"), {"t": body.tenant_id, "p": period})
        pack = await conn.scalar(text(
            "INSERT INTO cloudops.report_pack (tenant_id, period, version, contract_currency,"
            " sections, data_as_of, generated_by) VALUES (:t, :p, :v, :c, CAST(:s AS jsonb),"
            " :at, :by) RETURNING id"),
            {"t": body.tenant_id, "p": period, "v": version, "c": content.contract_currency,
             "s": json.dumps(stored.public), "at": now, "by": access.identity.oid})
        for table, part in (("report_pack_price", stored.priced),
                            ("report_pack_commercial", stored.commercial)):
            await conn.execute(text(
                f"INSERT INTO cloudops.{table} (pack_id, tenant_id, sections)"  # noqa: S608
                " VALUES (:p, :t, CAST(:s AS jsonb))"),
                {"p": pack, "t": body.tenant_id, "s": json.dumps(part)})
        wanted = sections.narrative_sections(content)
        if wanted:
            await conn.execute(text(
                "INSERT INTO cloudops.report_narrative (tenant_id, pack_id, section)"
                " VALUES (:t, :p, :s)"), [{"t": body.tenant_id, "p": pack, "s": s}
                                          for s in wanted])
        view = await _view(conn, await _load(conn, pack), True)
    audit_only(request, [body.tenant_id])
    return view


@router.get("/packs", response_model=list[ReportPackSummary])
@requires("reports.read")
async def list_packs(
    request: Request, access: AccessDep,
    tenant_id: Annotated[UUID | None, Query()] = None,
    period: Annotated[str | None, Query(pattern=PERIOD)] = None,
) -> list[ReportPackSummary]:
    where: list[str] = ["true"]
    params: dict[str, Any] = {}
    if tenant_id is not None:
        where.append("p.tenant_id = :t")
        params["t"] = tenant_id
    if period is not None:
        where.append("p.period = :p")
        params["p"] = _period(period)
    async with platform(request).db.transaction(access.scope) as conn:
        rows = [tuple(r) for r in await conn.execute(text(
            f"SELECT {_pack_cols(access.kind == 'staff')} FROM cloudops.report_pack p"  # noqa: S608
            " JOIN cloudops.tenant t ON t.id = p.tenant_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY p.period DESC, t.canonical_name, p.version DESC LIMIT 500"), params)]
    return [_summary(r) for r in rows]


@router.get("/packs/{pack_id}", response_model=ReportPackView)
@requires("reports.read")
async def get_pack(request: Request, pack_id: UUID, access: AccessDep) -> ReportPackView:
    staff = access.kind == "staff"
    async with platform(request).db.transaction(access.scope) as conn:
        row = await _load(conn, pack_id, staff)
        view = await _view(conn, row, staff)
    audit_only(request, [row[1]])
    return view


# --- narrative --------------------------------------------------------------------------


async def _draft_pack(conn: ScopedConnection, pack: UUID) -> tuple[Any, ...]:
    row = await _load(conn, pack)
    if row[5] != "draft":
        raise ApiError(409, "pack_not_editable")
    return row


@router.post("/packs/{pack_id}/narratives/{section}", response_model=ReportPackView)
@requires("reports.manage")
async def edit_narrative(request: Request, pack_id: UUID, section: str, body: SectionNarrativeEdit,
                         access: AccessDep) -> ReportPackView:
    """A person's text for a section: written, or the draft accepted as it stands."""
    if access.kind != "staff":
        raise ApiError(404, "not_found")
    async with platform(request).db.transaction(access.scope) as conn:
        row = await _draft_pack(conn, pack_id)
        updated = await conn.scalar(text(
            "UPDATE cloudops.report_narrative SET text = :x, edited_by = :by, edited_at = now()"
            " WHERE pack_id = :p AND section = :s RETURNING id"),
            {"x": body.text.strip(), "by": access.identity.oid, "p": pack_id, "s": section})
        if updated is None:
            raise ApiError(404, "not_found")
        view = await _view(conn, row, True)
    audit_only(request, [row[1]])
    return view


@router.post("/packs/{pack_id}/narratives/{section}/draft", response_model=ReportPackView,
             status_code=202)
@requires("reports.manage")
async def request_draft(request: Request, pack_id: UUID, section: str, access: AccessDep
                        ) -> ReportPackView:
    if access.kind != "staff":
        raise ApiError(404, "not_found")
    async with platform(request).db.transaction(access.scope) as conn:
        row = await _draft_pack(conn, pack_id)
        updated = await conn.scalar(text(
            "UPDATE cloudops.report_narrative SET draft_state = 'requested'"
            " WHERE pack_id = :p AND section = :s"
            " AND draft_state IN ('none', 'drafted', 'declined', 'failed') RETURNING id"),
            {"p": pack_id, "s": section})
        if updated is None:
            raise ApiError(409, "draft_in_progress")
        view = await _view(conn, row, True)
    audit_only(request, [row[1]])
    return view


# --- release ----------------------------------------------------------------------------


@router.post("/packs/{pack_id}/approve", response_model=ReportPackView)
@requires("reports.publish")
async def approve_pack(request: Request, pack_id: UUID, access: AccessDep) -> ReportPackView:
    if access.kind != "staff":
        raise ApiError(404, "not_found")
    async with platform(request).db.transaction(access.scope) as conn:
        row = await _draft_pack(conn, pack_id)
        unedited = await conn.scalar(text(
            "SELECT count(*) FROM cloudops.report_narrative WHERE pack_id = :p"
            " AND edited_by IS NULL"), {"p": pack_id})
        if unedited:
            raise ApiError(409, "narrative_not_reviewed")
        await conn.execute(text(
            "UPDATE cloudops.report_pack SET status = 'approved', approved_by = :by,"
            " approved_at = now() WHERE id = :p"), {"by": access.identity.oid, "p": pack_id})
        view = await _view(conn, await _load(conn, pack_id), True)
    audit_only(request, [row[1]])
    return view


@router.post("/packs/{pack_id}/publish", response_model=ReportPackView)
@requires("reports.publish")
async def publish_pack(request: Request, pack_id: UUID, access: AccessDep) -> ReportPackView:
    if access.kind != "staff":
        raise ApiError(404, "not_found")
    async with platform(request).db.transaction(access.scope) as conn:
        row = await _load(conn, pack_id)
        if row[5] != "approved":
            raise ApiError(409, "pack_not_approved")
        await conn.execute(text(
            "UPDATE cloudops.report_pack SET status = 'superseded' WHERE tenant_id = :t"
            " AND period = :period AND status = 'published' AND id <> :p"),
            {"t": row[1], "period": row[3], "p": pack_id})
        await conn.execute(text(
            "UPDATE cloudops.report_pack SET status = 'published', published_by = :by,"
            " published_at = now() WHERE id = :p"), {"by": access.identity.oid, "p": pack_id})
        view = await _view(conn, await _load(conn, pack_id), True)
    audit_only(request, [row[1]])
    return view


async def _lines(request: Request, pack_id: UUID, access: Access
                 ) -> tuple[tuple[Any, ...], list[Line]]:
    staff = access.kind == "staff"
    async with platform(request).db.transaction(access.scope) as conn:
        row = await _load(conn, pack_id, staff)
        narratives = {str(r[0]): str(r[4]) for r in
                      (tuple(x) for x in await conn.execute(_NARRATIVES[staff],
                                                            {"id": pack_id}))
                      if r[4] is not None}
    audit_only(request, [row[1]])
    content = sections.PackContent.from_json(row[1], {
        "tenant_name": row[2], "period": row[3].isoformat(),
        "contract_currency": str(row[6]).strip(), "data_as_of": row[10].isoformat(),
        "sections": row[11]})
    return row, render_lines(content, narratives, released=row[5] == "published", version=row[4])


@router.get("/packs/{pack_id}/pdf", response_class=Response, response_model=None,
            responses={200: {"content": {"application/pdf": {}}}})
@requires("reports.read")
async def pack_pdf(request: Request, pack_id: UUID, access: AccessDep) -> Response:
    """The pack as printed: a person's narrative only, and a draft banner until
    it is published."""
    row, lines = await _lines(request, pack_id, access)
    title = f"{row[2]} {row[3]:%Y-%m} v{row[4]}"
    return Response(render_pdf(lines, title), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="report-{row[3]:%Y-%m}-v{row[4]}.pdf"'})


@router.get("/packs/{pack_id}/text", response_class=Response, response_model=None,
            responses={200: {"content": {"text/plain": {}}}})
@requires("reports.read")
async def pack_text(request: Request, pack_id: UUID, access: AccessDep) -> Response:
    """The PDF's lines as text, one per printed line: what the acceptance diff
    compares against the manually assembled pack."""
    _, lines = await _lines(request, pack_id, access)
    return Response("\n".join(text_lines(lines)) + "\n", media_type="text/plain; charset=utf-8")


# --- portfolio --------------------------------------------------------------------------


@router.get("/portfolio", response_model=PortfolioView)
@requires("reports.read")
async def portfolio(
    request: Request, access: AccessDep,
    period: Annotated[str, Query(pattern=PERIOD)],
    currency: Annotated[str, Query(pattern=CURRENCY)],
) -> PortfolioView:
    if access.kind != "staff":
        # The portfolio consolidates Getronics' cost across customers: staff work.
        raise ApiError(403, "forbidden")
    if access.scope.account_ids is not None:
        raise ApiError(404, "not_found")
    month = _period(period)
    async with platform(request).db.transaction(access.scope) as conn:
        result = await consolidation.consolidate(conn, month, currency)
    now = datetime.now(UTC)

    def money(value: Decimal, name: str, **params: object) -> Metric[Decimal]:
        return Metric[Decimal](value=value, unit=currency, source_system="cloud_billing",
                               as_of=now, query_ref=query_ref(name, 1, period=period,
                                                              currency=currency, **params),
                               is_modelled=False)

    def count(value: int, unit: str, source: str, name: str, **params: object) -> Metric[int]:
        return Metric[int](value=value, unit=unit, source_system=source, as_of=now,
                           query_ref=query_ref(name, 1, period=period, **params),
                           is_modelled=False)

    return PortfolioView(
        period=period, reporting_currency=currency, complete=result.complete,
        total_consumption=money(result.total_consumption, "portfolio.consumption"),
        incidents_opened=count(result.incidents_opened, "incidents", "servicenow",
                               "portfolio.incidents"),
        open_high_vulnerabilities=count(result.open_high_vulnerabilities, "vulnerabilities",
                                        "qualys", "portfolio.vulnerabilities"),
        customers=[PortfolioLine(
            tenant=TenantView(id=c.tenant_id, name=c.tenant_name),
            consumption=None if c.consumption is None else money(
                c.consumption, "portfolio.customer_consumption", tenant=str(c.tenant_id)),
            gap=c.gap,
            incidents_opened=count(c.incidents_opened, "incidents", "servicenow",
                                   "portfolio.customer_incidents", tenant=str(c.tenant_id)),
            open_high_vulnerabilities=count(
                c.open_high_vulnerabilities, "vulnerabilities", "qualys",
                "portfolio.customer_vulnerabilities", tenant=str(c.tenant_id)),
        ) for c in result.customers],
    )
