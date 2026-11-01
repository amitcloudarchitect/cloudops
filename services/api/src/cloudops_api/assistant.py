"""The AMARA assistant's API (ADR 0029).

    GET  /v1/assistant/session                     what the panel offers on this screen
    POST /v1/assistant/questions                   ask; 202 with the turn to poll
    GET  /v1/assistant/turns/{turn_id}             a turn's stage, then its answer
    GET  /v1/assistant/conversations               the caller's own conversations
    GET  /v1/assistant/conversations/{id}          one of them, with its turns
    POST /v1/assistant/conversations/{id}/delete   delete one, and forget its prompts
    POST /v1/assistant/tenants/{tenant_id}/enabled switch a customer's assistant on/off

**Scope.** Resolved from the caller's principal like every request. A conversation
belongs to exactly one tenant, chosen once, when it starts:

- a customer: its one tenant, and only once that tenant's assistant is switched on;
- staff: the tenant of the screen, if it is in their scope (otherwise "not found",
  as for any other id), or none: a help-only conversation stored under the internal
  (Getronics) tenant, which reads only the platform help and calls no tool.

What the client says about its screen (a screen name, a ticket number, a resource
id) is a hint. It is shape-checked here, and the record is looked up under the
conversation's scope by the assistant, so an id of another tenant is not found and
is ignored. Conversations are visible to their author only: the database's
``owner_only`` policy, not a ``WHERE`` here.

**Running.** A question is answered in this process, as a background task, and the
panel polls the turn: stages, then the answer. Read-only: the assistant has no write
tool and never acts. No question or answer text is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field
from sqlalchemy import text

from cloudops_agents.assistant import Assistant, Outcome, Question, ScreenHint, Stage
from cloudops_agents.trace import DbTraceSink
from cloudops_api.access import Access
from cloudops_api.pipeline import (
    AUTHENTICATED,
    ApiError,
    AuditedRoute,
    audit_only,
    current_access,
    platform,
    requires,
)
from cloudops_api.provenance import ApiModel
from cloudops_gateway.gateway import CallContext, Gateway
from cloudops_gateway.registry import ToolRegistry
from cloudops_gateway.retrieval import KnowledgeIndex
from cloudops_gateway.tools import TOOLS
from cloudops_tenancy import Database, ScopedConnection, TenantScope

log = logging.getLogger("cloudops.api.assistant")

router = APIRouter(prefix="/v1/assistant", route_class=AuditedRoute)
AccessDep = Annotated[Access, Depends(current_access)]

TURN_CEILING: Final = 20  # LLD: a 20-turn conversation ceiling
TURN_DEADLINE: Final = timedelta(seconds=90)
STALE_AFTER: Final = timedelta(minutes=3)  # a pending turn whose process died
HELP_SUBJECT: Final = "the Getronics CloudOps platform (no customer chosen)"
LIST_LIMIT: Final = 50

Screen = Literal[
    "home", "customers", "customer", "work", "ticket", "resources", "resource", "finops",
    "finops_customer", "margin", "margin_customer", "pricing", "posture", "performance",
    "approvals", "reports", "report", "knowledge", "assurance", "users", "other",
]
RecordType = Literal["ticket", "resource"]
_RECORD_ID: Final = {
    "ticket": re.compile(r"^[A-Za-z]{2,8}[0-9]{3,12}$"),
    "resource": re.compile(r"^[0-9a-fA-F-]{36}$"),
}


# --- views ---------------------------------------------------------------------------


class ScreenContext(ApiModel):
    """Where the panel was opened. A hint, re-checked under the resolved scope."""

    screen: Screen = "other"
    record_type: RecordType | None = None
    record_id: str | None = Field(default=None, max_length=64)


class TenantOption(ApiModel):
    id: UUID
    name: str


class AssistantSession(ApiModel):
    available: bool
    unavailable_reason: Literal["not_enabled", "not_configured"] | None
    mode: Literal["account", "help"]
    tenant: TenantOption | None
    tenants: list[TenantOption]  # what a staff member may choose; empty for a customer
    suggestions: list[str]


class AskRequest(ApiModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None
    tenant_id: UUID | None = None  # staff: the screen's customer; ignored for a customer
    context: ScreenContext = Field(default_factory=ScreenContext)


class SourceView(ApiModel):
    ref: str
    label: str
    source_system: str
    as_of: datetime | None
    is_modelled: bool
    url: str | None


class ClaimView(ApiModel):
    text: str
    sources: list[str]


class AnswerView(ApiModel):
    text: str
    claims: list[ClaimView]
    sources: list[SourceView]


TurnStatus = Literal["pending", "answered", "dont_know", "refused", "failed"]


class TurnView(ApiModel):
    id: UUID
    conversation_id: UUID
    question: str
    status: TurnStatus
    stage: Stage | None
    answer: AnswerView | None
    reason: str | None
    created_at: datetime
    answered_at: datetime | None


class ConversationSummary(ApiModel):
    id: UUID
    title: str
    mode: Literal["account", "help"]
    tenant: TenantOption | None  # None: help-only
    updated_at: datetime


class ConversationView(ConversationSummary):
    turns: list[TurnView]


class EnabledRequest(ApiModel):
    enabled: bool


class EnabledView(ApiModel):
    tenant_id: UUID
    enabled: bool


# --- suggestions -----------------------------------------------------------------------

# (permission or None, question), per screen. Deterministic: no model involved.
_SUGGESTIONS: Final[Mapping[str, tuple[tuple[str | None, str], ...]]] = {
    "ticket": (("tickets.read", "Summarise this ticket and where it stands."),
               ("tickets.read", "Is this ticket at risk of breaching its SLA?"),
               ("inventory.read", "What else is open on the affected resource?"),
               ("tickets.read", "Have there been recent changes related to this?")),
    "work": (("tickets.read", "Which open tickets have breached their SLA?"),
             ("tickets.read", "How is SLA attainment over the last 30 days?"),
             ("tickets.read", "What changes were made in the last two weeks?")),
    "resource": (("inventory.read", "Summarise this resource and its recent tickets."),
                 ("vulnerabilities.read", "What vulnerabilities are open on it?"),
                 ("tickets.read", "Were there recent changes to this resource?")),
    "resources": (("inventory.read", "How do I find a resource and read its context?"),
                  ("vulnerabilities.read", "Which resources have the oldest open findings?")),
    "customer": (("tickets.read", "Give me a summary of service health for this account."),
                 ("tickets.read", "How is SLA attainment this month?"),
                 ("cost.read", "How has cloud spend moved over the last 30 days?"),
                 ("vulnerabilities.read", "What are the most urgent open vulnerabilities?")),
    "customers": ((None, "What do the health bands on this screen mean?"),),
    "finops": (("cost.read", "How has cloud spend moved over the last 30 days?"),
               (None, "How are costs converted between currencies?")),
    "finops_customer": (("cost.read", "How has cloud spend moved over the last 30 days?"),
                        (None, "How are costs converted between currencies?")),
    "margin": ((None, "What does each margin band mean?"),
               ("commercial.read", "Is the margin figure measured or modelled?")),
    "margin_customer": (("commercial.read", "How has margin moved over the last three months?"),
                        ("commercial.read", "Is this margin figure measured or modelled?"),
                        (None, "What does each margin band mean?")),
    "posture": (("vulnerabilities.read", "What are the most urgent open vulnerabilities?"),
                ("vulnerabilities.read", "How many findings are open by severity?"),
                (None, "What do the severity levels mean?")),
    "performance": (("tickets.read", "How is SLA attainment over the last 90 days?"),
                    (None, "How is SLA attainment calculated?")),
    "reports": ((None, "How does the monthly report pack work?"),),
    "report": ((None, "How does the monthly report pack work?"),),
    "approvals": ((None, "What does this approvals screen show?"),),
}
_GENERAL: Final = ((None, "What can AMARA help me with?"),
                   (None, "How do I read the sources and as-of times on an answer?"))
_HELP_ONLY: Final = ((None, "What can AMARA help me with?"),
                     (None, "What do the health bands mean?"),
                     (None, "How does customer onboarding work?"))


def suggestions(screen: str, permissions: frozenset[str], mode: str) -> list[str]:
    pool = _HELP_ONLY if mode == "help" else _SUGGESTIONS.get(screen, ()) + _GENERAL
    return [q for perm, q in pool if perm is None or perm in permissions][:4]


# --- the runtime -----------------------------------------------------------------------


@dataclass
class AssistantRuntime:
    """The engine, and the turns it is answering in this process."""

    db: Database
    engine: Assistant
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def start(self, job: _Job) -> None:
        task = asyncio.create_task(_run_turn(self.db, self.engine, job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


@dataclass(frozen=True)
class _Job:
    turn_id: UUID
    question: Question


def _runtime(request: Request) -> AssistantRuntime | None:
    value = getattr(request.app.state, "assistant", None)
    return value if isinstance(value, AssistantRuntime) else None


_SET_STAGE: Final = text(
    "UPDATE cloudops.assistant_turn SET stage = :stage WHERE id = :id AND status = 'pending'")
_FINISH: Final = text(
    "UPDATE cloudops.assistant_turn SET status = :status, stage = NULL,"
    " answer = CAST(:answer AS jsonb), reason = :reason,"
    " agent_run_ids = CAST(:runs AS uuid[]), answered_at = now()"
    " WHERE id = :id AND status = 'pending'")
_TOUCH: Final = text(
    "UPDATE cloudops.assistant_conversation SET updated_at = now()"
    " WHERE id = (SELECT conversation_id FROM cloudops.assistant_turn WHERE id = :id)")


def _answer_json(outcome: Outcome) -> str:
    return json.dumps({
        "text": outcome.text, "claims": outcome.claims,
        "sources": [{"ref": s.ref, "label": s.label, "source_system": s.source_system,
                     "as_of": None if s.as_of is None else s.as_of.isoformat(),
                     "is_modelled": s.is_modelled, "url": s.url} for s in outcome.sources],
    })


async def _run_turn(db: Database, engine: Assistant, job: _Job) -> None:
    scope = job.question.ctx.scope

    async def stage(name: Stage) -> None:
        async with db.transaction(scope) as conn:
            await conn.execute(_SET_STAGE, {"stage": name, "id": job.turn_id})

    try:
        outcome = await asyncio.wait_for(engine.answer(job.question, stage),
                                         TURN_DEADLINE.total_seconds())
    except TimeoutError:
        outcome = Outcome.refusal("unavailable", "failed")
        outcome.reason = "deadline"
    except asyncio.CancelledError:
        outcome = Outcome.refusal("unavailable", "failed")
        outcome.reason = "interrupted"
    except Exception:
        # The type only: never the question, the answer or a value (ADR 0022).
        log.exception("assistant turn failed")
        outcome = Outcome.refusal("unavailable", "failed")
    async with db.transaction(scope) as conn:
        await conn.execute(_FINISH, {
            "status": outcome.status, "answer": _answer_json(outcome), "reason": outcome.reason,
            "runs": "{" + ",".join(outcome.agent_run_ids) + "}", "id": job.turn_id})
        await conn.execute(_TOUCH, {"id": job.turn_id})


# --- scope -----------------------------------------------------------------------------

_INTERNAL: Final = text("SELECT cloudops.internal_tenant()")


async def _internal_tenant(db: Database, access: Access) -> UUID | None:
    async with db.transaction(TenantScope.for_resolution(access.scope.principal_id)) as conn:
        value = await conn.scalar(_INTERNAL)
    return None if value is None else UUID(str(value))


def _with(scope: TenantScope, tenant: UUID | None) -> TenantScope:
    """The caller's scope, plus the internal tenant for its own help-only
    conversations. Only ever used on the assistant's tables, under owner_only."""
    if tenant is None or tenant in scope.tenant_ids:
        return scope
    return TenantScope(scope.tenant_ids | {tenant}, scope.role, scope.principal_id,
                       account_ids=scope.account_ids)


async def _history_scope(request: Request, access: Access) -> TenantScope:
    if access.kind == "customer":
        return access.scope
    return _with(access.scope, await _internal_tenant(platform(request).db, access))


async def _tenant_names(conn: ScopedConnection) -> dict[UUID, tuple[str, bool]]:
    rows = await conn.execute(text(
        "SELECT id, canonical_name, assistant_enabled FROM cloudops.tenant ORDER BY 2"))
    out: dict[UUID, tuple[str, bool]] = {}
    for row in rows:
        tid, name, enabled = tuple(row)
        out[UUID(str(tid))] = (str(name), bool(enabled))
    return out


@dataclass(frozen=True)
class _Binding:
    mode: Literal["account", "help"]
    tenant: UUID | None  # None: help-only, under the internal tenant
    name: str | None
    available: bool
    reason: Literal["not_enabled", "not_configured"] | None
    visible: dict[UUID, tuple[str, bool]]


async def _bind(request: Request, access: Access, hint: UUID | None) -> _Binding:
    """The one tenant a new conversation is about. ``hint`` is the screen's
    customer: honoured only if it is in the caller's resolved scope."""
    async with platform(request).db.transaction(access.scope) as conn:
        visible = await _tenant_names(conn)
    configured = _runtime(request) is not None
    if access.kind == "customer":
        ((tid, (name, enabled)),) = visible.items()  # exactly one (ADR 0002 D7)
        reason: Literal["not_enabled", "not_configured"] | None = (
            "not_configured" if not configured else None if enabled else "not_enabled")
        return _Binding("account", tid, name, reason is None, reason, visible)
    reason = None if configured else "not_configured"
    if hint is not None:
        if hint not in visible:
            raise ApiError(404, "not_found")  # another tenant's id looks like no tenant
        return _Binding("account", hint, visible[hint][0], configured, reason, visible)
    return _Binding("help", None, None, configured, reason, visible)


# --- endpoints -------------------------------------------------------------------------


@router.get("/session", response_model=AssistantSession)
@requires(AUTHENTICATED)
async def session(
    request: Request, access: AccessDep,
    screen: Annotated[Screen, Query()] = "other",
    tenant_id: Annotated[UUID | None, Query()] = None,
) -> AssistantSession:
    """What the panel offers here: whether it is available, which account it is
    about, and suggested questions for the screen and the caller's permissions."""
    bound = await _bind(request, access, tenant_id)
    staff = access.kind == "staff"
    return AssistantSession(
        available=bound.available, unavailable_reason=bound.reason, mode=bound.mode,
        tenant=None if bound.tenant is None or bound.name is None
        else TenantOption(id=bound.tenant, name=bound.name),
        tenants=[TenantOption(id=t, name=n) for t, (n, _) in bound.visible.items()]
        if staff else [],
        suggestions=suggestions(screen, access.permissions, bound.mode)
        if bound.available else [],
    )


_CONVERSATION: Final = text(
    "SELECT c.id, c.tenant_id, c.mode, c.title, c.updated_at,"
    " (SELECT count(*) FROM cloudops.assistant_turn t WHERE t.conversation_id = c.id),"
    " (SELECT count(*) FROM cloudops.assistant_turn t WHERE t.conversation_id = c.id"
    "    AND t.status = 'pending' AND t.created_at > now() - CAST(:stale AS interval))"
    " FROM cloudops.assistant_conversation c WHERE c.id = :id")
_HISTORY: Final = text(
    "SELECT question, answer->>'text' FROM cloudops.assistant_turn"
    " WHERE conversation_id = :c AND status = 'answered' ORDER BY seq DESC LIMIT 4")


def _hint(ctx: ScreenContext) -> ScreenHint:
    record_type, record_id = ctx.record_type, ctx.record_id
    if record_type is None or record_id is None or not _RECORD_ID[record_type].fullmatch(
            record_id):
        record_type = record_id = None  # malformed: dropped, never guessed at
    return ScreenHint(ctx.screen, record_type, record_id)


def _stale() -> str:
    return f"{int(STALE_AFTER.total_seconds())} seconds"


@router.post("/questions", response_model=TurnView, status_code=202)
@requires(AUTHENTICATED)
async def ask(request: Request, body: AskRequest, access: AccessDep) -> TurnView:
    """Ask a question, in a new conversation or a running one. Answered in the
    background; poll the turn."""
    runtime = _runtime(request)
    db = platform(request).db
    internal = None if access.kind == "customer" else await _internal_tenant(db, access)
    history_scope = _with(access.scope, internal)
    if body.conversation_id is not None:
        async with db.transaction(history_scope) as conn:
            found = (await conn.execute(_CONVERSATION, {
                "id": body.conversation_id, "stale": _stale()})).one_or_none()
            visible = await _tenant_names(conn)
        if found is None:
            raise ApiError(404, "not_found")
        cid, tenant, mode, _, _, turns, pending = tuple(found)
        if pending:
            raise ApiError(409, "turn_in_progress")
        if turns >= TURN_CEILING:
            raise ApiError(409, "conversation_turn_ceiling")
        name = None if mode == "help" else visible.get(UUID(str(tenant)), ("", False))[0]
        if access.kind == "customer" and not visible.get(UUID(str(tenant)), ("", False))[1]:
            raise ApiError(403, "assistant_not_enabled")
        bound = _Binding(mode, None if mode == "help" else UUID(str(tenant)), name, True,
                         None, visible)
        seq = int(turns) + 1
    else:
        bound = await _bind(request, access, body.tenant_id)
        if bound.reason == "not_enabled":
            raise ApiError(403, "assistant_not_enabled")
        cid, seq = None, 1
    if runtime is None:
        raise ApiError(503, "assistant_unavailable")
    tenant = bound.tenant if bound.tenant is not None else internal
    if tenant is None:
        raise ApiError(503, "assistant_unavailable")  # no internal tenant for help mode

    async with db.transaction(history_scope) as conn:
        if cid is None:
            cid = await conn.scalar(text(
                "INSERT INTO cloudops.assistant_conversation (tenant_id, principal, mode, title)"
                " VALUES (:t, :p, :m, :title) RETURNING id"),
                {"t": tenant, "p": access.scope.principal_id, "m": bound.mode,
                 "title": body.question.strip()[:120] or "Question"})
        history = [(str(q), str(a)) for q, a in reversed(list(
            await conn.execute(_HISTORY, {"c": cid})))]
        row = (await conn.execute(text(
            "INSERT INTO cloudops.assistant_turn (tenant_id, conversation_id, principal, seq,"
            " question, context) VALUES (:t, :c, :p, :seq, :q, CAST(:ctx AS jsonb))"
            " RETURNING id, created_at"),
            {"t": tenant, "c": cid, "p": access.scope.principal_id, "seq": seq,
             "q": body.question, "ctx": body.context.model_dump_json()})).one()
    audit_only(request, [tenant])
    turn_id, created = tuple(row)

    scope = _with(access.scope, tenant)
    ctx = CallContext.for_tenant(
        scope, tenant, access.permissions if bound.mode == "account" else frozenset(),
        conversation_id=UUID(str(cid)))
    runtime.start(_Job(UUID(str(turn_id)), Question(
        ctx=ctx, mode=bound.mode, text=body.question, audience=access.kind,
        subject=bound.name if bound.mode == "account" and bound.name else HELP_SUBJECT,
        other_tenant_names=[n for t, (n, _) in bound.visible.items() if t != bound.tenant],
        hint=_hint(body.context) if bound.mode == "account" else None,
        history=history)))
    return TurnView(id=turn_id, conversation_id=cid, question=body.question,
                    status="pending", stage=None, answer=None, reason=None,
                    created_at=created, answered_at=None)


_TURN_COLUMNS: Final = (
    "t.id, t.conversation_id, t.question, t.status, t.stage, t.answer, t.reason,"
    " t.created_at, t.answered_at")


def _turn(row: tuple[Any, ...]) -> TurnView:
    tid, cid, question, status, stage, answer, reason, created, answered = row
    if status == "pending" and created < datetime.now(UTC) - STALE_AFTER:
        status, reason = "failed", "interrupted"  # its process went away
    view = None
    if answer is not None:
        view = AnswerView(
            text=str(answer.get("text", "")),
            claims=[ClaimView(**c) for c in answer.get("claims", [])],
            sources=[SourceView(**s) for s in answer.get("sources", [])])
    return TurnView(id=tid, conversation_id=cid, question=question, status=status,
                    stage=stage if status == "pending" else None, answer=view, reason=reason,
                    created_at=created, answered_at=answered)


@router.get("/turns/{turn_id}", response_model=TurnView)
@requires(AUTHENTICATED)
async def get_turn(request: Request, turn_id: UUID, access: AccessDep) -> TurnView:
    scope = await _history_scope(request, access)
    async with platform(request).db.transaction(scope) as conn:
        found = (await conn.execute(text(
            f"SELECT {_TURN_COLUMNS}, t.tenant_id FROM cloudops.assistant_turn t"  # noqa: S608
            " WHERE t.id = :id"), {"id": turn_id})).one_or_none()
    if found is None:
        raise ApiError(404, "not_found")  # someone else's turn looks like no turn
    row = tuple(found)
    audit_only(request, [row[-1]])
    return _turn(row[:-1])


@router.get("/conversations", response_model=list[ConversationSummary])
@requires(AUTHENTICATED)
async def list_conversations(request: Request, access: AccessDep) -> list[ConversationSummary]:
    scope = await _history_scope(request, access)
    async with platform(request).db.transaction(scope) as conn:
        names = await _tenant_names(conn)
        rows = [tuple(r) for r in await conn.execute(text(
            "SELECT id, tenant_id, mode, title, updated_at FROM cloudops.assistant_conversation"
            " ORDER BY updated_at DESC LIMIT :limit"), {"limit": LIST_LIMIT})]
    return [_summary(r, names) for r in rows]


def _summary(r: tuple[Any, ...], names: Mapping[UUID, tuple[str, bool]]
             ) -> ConversationSummary:
    cid, tenant, mode, title, updated = r
    tenant_id = UUID(str(tenant))
    return ConversationSummary(
        id=cid, title=title, mode=mode, updated_at=updated,
        tenant=None if mode == "help" or tenant_id not in names
        else TenantOption(id=tenant_id, name=names[tenant_id][0]))


@router.get("/conversations/{conversation_id}", response_model=ConversationView)
@requires(AUTHENTICATED)
async def get_conversation(request: Request, conversation_id: UUID, access: AccessDep
                           ) -> ConversationView:
    scope = await _history_scope(request, access)
    async with platform(request).db.transaction(scope) as conn:
        names = await _tenant_names(conn)
        found = (await conn.execute(text(
            "SELECT id, tenant_id, mode, title, updated_at FROM cloudops.assistant_conversation"
            " WHERE id = :id"), {"id": conversation_id})).one_or_none()
        turns = [tuple(r) for r in await conn.execute(text(
            f"SELECT {_TURN_COLUMNS} FROM cloudops.assistant_turn t"  # noqa: S608
            " WHERE t.conversation_id = :id ORDER BY t.seq"), {"id": conversation_id})]
    if found is None:
        raise ApiError(404, "not_found")
    row = tuple(found)
    audit_only(request, [row[1]])
    summary = _summary(row, names)
    return ConversationView(**summary.model_dump(), turns=[_turn(t) for t in turns])


@router.post("/conversations/{conversation_id}/delete", status_code=204,
             response_class=Response)
@requires(AUTHENTICATED)
async def delete_conversation(request: Request, conversation_id: UUID, access: AccessDep
                              ) -> Response:
    """Delete one of the caller's conversations, and clear the prompt text and
    agent output the gateway recorded for it."""
    scope = await _history_scope(request, access)
    async with platform(request).db.transaction(scope) as conn:
        tenant = await conn.scalar(text(
            "DELETE FROM cloudops.assistant_conversation WHERE id = :id RETURNING tenant_id"),
            {"id": conversation_id})
        if tenant is None:
            raise ApiError(404, "not_found")
        await conn.execute(text("SELECT cloudops.assistant_forget(:id)"),
                           {"id": conversation_id})
    audit_only(request, [UUID(str(tenant))])
    return Response(status_code=204)


@router.post("/tenants/{tenant_id}/enabled", response_model=EnabledView)
@requires("access.manage")
async def set_enabled(request: Request, tenant_id: UUID, body: EnabledRequest,
                      access: AccessDep) -> EnabledView:
    """Switch the assistant on or off for a customer's portal users. Staff always
    have it. Off by default until the customer-role evaluation cases pass."""
    if access.kind != "staff":
        raise ApiError(403, "forbidden")
    async with platform(request).db.transaction(access.scope) as conn:
        changed = await conn.scalar(text(
            "UPDATE cloudops.tenant SET assistant_enabled = :on WHERE id = :id RETURNING id"),
            {"on": body.enabled, "id": tenant_id})
    if changed is None:
        raise ApiError(404, "not_found")
    audit_only(request, [tenant_id])
    return EnabledView(tenant_id=tenant_id, enabled=body.enabled)


def build_runtime(db: Database, gateway: Gateway) -> AssistantRuntime:
    """The assistant over ``gateway``, with the read-only tools only."""
    registry = ToolRegistry(db)
    for tool in TOOLS:
        registry.register(tool)
    return AssistantRuntime(db, Assistant(gateway, registry, KnowledgeIndex(db, gateway),
                                          DbTraceSink(db)))
