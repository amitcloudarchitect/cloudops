"""Temporal orchestration: the connector is a workflow, not a cron job.

``ServiceNowIngest`` (one entity, one run):

    start run -> read watermark -> since = watermark - 5 min (or initial_since)
    loop: fetch page (raw to bronze) -> process page (normalise, resolve, commit)
    finish run (the health report)

``ServiceNowReplay`` reprocesses bronze pages for a date range through the same
``process_page``, never touching the source and never moving the watermark.

``ServiceNowReprocess`` is that replay narrowed to one approved name (ADR 0015).
It runs on a schedule and takes the oldest waiting request a person made in the
console, if any: it re-checks the request under its tenant's scope (the mapping
is still approved, the name still resolves exactly to that tenant), replays the
name's bronze days through ``reprocess_page`` for every entity, and closes the
request with how many records moved. A name that stops resolving to the tenant
mid-run ends the request refused; a page that keeps failing is dead-lettered
and ends it failed. Nothing starts it but a request.

Retries belong to Temporal (CLAUDE.md rule 3): each activity has exponential
backoff and five attempts (LLD). A page that still fails is dead-lettered with
its bronze reference, the run stops there and is marked ``dead_lettered``,
and the watermark stays before that page, so nothing after it is skipped.
The next run starts from the same place; once fixed, replay repairs history.

Workflow code is deterministic: time and ids come from ``workflow``, and every
side effect is an activity.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from sqlalchemy import text

    from cloudops_connectors.core import state
    from cloudops_connectors.core.raw import RawStore, day_prefixes, page_ref, sha256
    from cloudops_connectors.servicenow.client import ServiceNowClient
    from cloudops_connectors.servicenow.mapping import ENTITIES, SOURCE
    from cloudops_connectors.servicenow.pipeline import (
        PRINCIPAL,
        ReprocessCounts,
        process_page,
        reprocess_page,
    )
    from cloudops_tenancy import Database, Role, TenantScope

TASK_QUEUE: Final = "connectors-servicenow"
OVERLAP: Final = timedelta(minutes=5)  # LLD: fetch with a five-minute overlap

RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
    non_retryable_error_types=["RawConflictError", "ValueError", "ReprocessRefusedError"],
)


# --- messages (plain dataclasses: Temporal serialises them) ---------------------


@dataclass
class IngestRequest:
    entity: str
    initial_since: str  # ISO 8601, used only when no watermark exists yet


@dataclass
class ReplayRequest:
    entity: str
    start: str  # ISO date, inclusive
    end: str


@dataclass
class FetchRequest:
    run_id: str
    entity: str
    since: str
    offset: int
    page: int


@dataclass
class FetchResult:
    raw_ref: str
    records: int
    has_more: bool


@dataclass
class ProcessRequest:
    entity: str
    raw_ref: str
    advance_watermark: bool


@dataclass
class RunUpdate:
    run_id: str
    entity: str
    mode: str
    at: str
    status: str = "running"
    counts: state.RunCounts = field(default_factory=state.RunCounts)


@dataclass
class DeadLetter:
    run_id: str
    entity: str
    raw_ref: str
    error: str


@dataclass
class RunReport:
    run_id: str
    entity: str
    mode: str
    status: str
    counts: state.RunCounts
    watermark: str | None


@dataclass
class ReprocessClaim:
    id: str
    tenant_id: str
    proposal_id: str
    alias: str
    start: str  # ISO date, inclusive
    end: str


@dataclass
class ReprocessPageRequest:
    claim: ReprocessClaim
    entity: str
    raw_ref: str


@dataclass
class ReprocessFinish:
    claim: ReprocessClaim
    status: str  # succeeded | refused | failed
    moved: int | None
    pages: int
    outcome: str | None  # our own code, never payload text


@dataclass
class ReprocessReport:
    request_id: str | None  # None: nothing was waiting
    status: str
    moved: int
    pages: int


def _scope() -> TenantScope:
    return TenantScope((), Role.INGEST, PRINCIPAL)


def _tenant_scope(claim: ReprocessClaim) -> TenantScope:
    return TenantScope([UUID(claim.tenant_id)], Role.INGEST, PRINCIPAL)


# --- activities ------------------------------------------------------------------


class ServiceNowActivities:
    """Every side effect of the connector. Dependencies are injected so the
    same activities run against fixtures, Azurite and a real instance."""

    def __init__(self, db: Database, raw: RawStore, client: ServiceNowClient) -> None:
        self._db = db
        self._raw = raw
        self._client = client
        # Watermarks are per source, entity and instance.
        self.scope_key = client.base_url

    @activity.defn
    async def read_watermark(self, entity: str) -> str | None:
        async with self._db.transaction(_scope()) as conn:
            mark = await state.read_watermark(conn, SOURCE, entity, self.scope_key)
        return None if mark is None else mark.isoformat()

    @activity.defn
    async def fetch_page(self, req: FetchRequest) -> FetchResult:
        since = datetime.fromisoformat(req.since)
        body = await self._client.fetch(req.entity, since, req.offset)
        fetched_at = datetime.now(UTC)
        ref = page_ref(SOURCE, req.entity, fetched_at, req.run_id, req.page)
        # Bronze first: stored before a single byte is parsed.
        await self._raw.put(ref, body, {
            "source": SOURCE, "entity": req.entity, "run_id": req.run_id,
            "since": req.since, "offset": str(req.offset), "sha256": sha256(body),
            "fetched_at": fetched_at.isoformat(), "instance": self.scope_key,
        })
        records = len(json.loads(body)["result"])
        return FetchResult(ref, records, records >= self._client.page_size)

    @activity.defn
    async def process(self, req: ProcessRequest) -> state.RunCounts:
        return await process_page(
            self._db, self._raw, self.scope_key, req.entity, req.raw_ref,
            advance_watermark=req.advance_watermark,
        )

    @activity.defn
    async def list_raw(self, req: ReplayRequest) -> list[str]:
        refs: list[str] = []
        for prefix in day_prefixes(SOURCE, req.entity, date.fromisoformat(req.start),
                                   date.fromisoformat(req.end)):
            refs.extend(await self._raw.list(prefix))
        return refs

    @activity.defn
    async def claim_reprocess(self) -> ReprocessClaim | None:
        async with self._db.transaction(_scope()) as conn:
            found = (await conn.execute(
                text("SELECT id, tenant_id, proposal_id, alias, window_start, window_end"
                     " FROM cloudops.claim_reprocess(:source)"), {"source": SOURCE})).one_or_none()
        if found is None:
            return None
        rid, tenant, proposal, alias, start, end = tuple(found)
        return ReprocessClaim(str(rid), str(tenant), str(proposal), str(alias),
                              start.isoformat(), end.isoformat())

    @activity.defn
    async def check_reprocess(self, claim: ReprocessClaim) -> str | None:
        """At run time, not only at request time: why not to run it, or None."""
        async with self._db.transaction(_tenant_scope(claim)) as conn:
            decision = await conn.scalar(
                text("SELECT decision FROM cloudops.alias_decision WHERE proposal_id = :p"),
                {"p": UUID(claim.proposal_id)})
        if decision != "approve":
            return "not_approved"
        async with self._db.transaction(_scope()) as conn:
            resolved = (await state.resolve_aliases(conn, SOURCE, [claim.alias]))[claim.alias]
        if resolved.outcome != "exact" or str(resolved.tenant_id) != claim.tenant_id:
            return "mapping_changed"
        return None

    @activity.defn
    async def reprocess(self, req: ReprocessPageRequest) -> ReprocessCounts:
        return await reprocess_page(self._db, self._raw, req.entity, req.raw_ref,
                                    req.claim.alias, UUID(req.claim.tenant_id))

    @activity.defn
    async def finish_reprocess(self, fin: ReprocessFinish) -> None:
        async with self._db.transaction(_tenant_scope(fin.claim)) as conn:
            await conn.execute(
                text("SELECT cloudops.finish_reprocess(:id, :status, :moved, :pages, :outcome)"),
                {"id": UUID(fin.claim.id), "status": fin.status, "moved": fin.moved,
                 "pages": fin.pages, "outcome": fin.outcome})

    @activity.defn
    async def record_run(self, update: RunUpdate) -> None:
        at = datetime.fromisoformat(update.at)
        async with self._db.transaction(_scope()) as conn:
            if update.status == "running":
                await state.start_run(conn, UUID(update.run_id), SOURCE, update.entity,
                                      update.mode, at)
                return
            mark = await state.read_watermark(conn, SOURCE, update.entity, self.scope_key)
            await state.finish_run(conn, UUID(update.run_id), update.status, update.counts,
                                   at, mark)
        state.report_run(SOURCE, update.entity, update.mode, update.status, update.counts)

    @activity.defn
    async def record_dead_letter(self, letter: DeadLetter) -> None:
        async with self._db.transaction(_scope()) as conn:
            await state.dead_letter(conn, UUID(letter.run_id), SOURCE, letter.entity,
                                    letter.raw_ref, letter.error)


# --- workflows --------------------------------------------------------------------

_SHORT: Final = timedelta(minutes=1)
_PAGE: Final = timedelta(minutes=5)


def _error(exc: ActivityError) -> str:
    """Exception type and first line only: never a payload value."""
    cause = exc.cause or exc
    first = str(cause).splitlines()[0] if str(cause) else ""
    return f"{type(cause).__name__}: {first}"[:500]


@workflow.defn
class ServiceNowIngest:
    @workflow.run
    async def run(self, req: IngestRequest) -> RunReport:
        if req.entity not in ENTITIES:
            raise ValueError(f"unknown entity {req.entity!r}")
        run_id = str(workflow.uuid4())
        acts = ServiceNowActivities
        await workflow.execute_activity_method(
            acts.record_run,
            RunUpdate(run_id, req.entity, "incremental", workflow.now().isoformat()),
            start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        mark = await workflow.execute_activity_method(
            acts.read_watermark, req.entity, start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        since = (datetime.fromisoformat(mark) - OVERLAP) if mark else datetime.fromisoformat(
            req.initial_since)
        counts = state.RunCounts()
        status = "succeeded"
        offset = page = 0
        while True:
            try:
                fetched = await workflow.execute_activity_method(
                    acts.fetch_page,
                    FetchRequest(run_id, req.entity, since.isoformat(), offset, page),
                    start_to_close_timeout=_PAGE, retry_policy=RETRY,
                )
            except ActivityError:
                status = "failed"  # nothing reached bronze; the next run starts here
                break
            try:
                done = await workflow.execute_activity_method(
                    acts.process, ProcessRequest(req.entity, fetched.raw_ref, True),
                    start_to_close_timeout=_PAGE, retry_policy=RETRY,
                )
            except ActivityError as exc:
                await workflow.execute_activity_method(
                    acts.record_dead_letter,
                    DeadLetter(run_id, req.entity, fetched.raw_ref, _error(exc)),
                    start_to_close_timeout=_SHORT, retry_policy=RETRY,
                )
                status = "dead_lettered"
                break
            counts.add(done)
            if not fetched.has_more:
                break
            offset += fetched.records
            page += 1
        await workflow.execute_activity_method(
            acts.record_run,
            RunUpdate(
                run_id, req.entity, "incremental", workflow.now().isoformat(), status, counts
            ),
            start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        mark = await workflow.execute_activity_method(
            acts.read_watermark, req.entity, start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        return RunReport(run_id, req.entity, "incremental", status, counts, mark)


@workflow.defn
class ServiceNowReplay:
    @workflow.run
    async def run(self, req: ReplayRequest) -> RunReport:
        if req.entity not in ENTITIES:
            raise ValueError(f"unknown entity {req.entity!r}")
        run_id = str(workflow.uuid4())
        acts = ServiceNowActivities
        await workflow.execute_activity_method(
            acts.record_run, RunUpdate(run_id, req.entity, "replay", workflow.now().isoformat()),
            start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        refs = await workflow.execute_activity_method(
            acts.list_raw, req, start_to_close_timeout=_PAGE, retry_policy=RETRY,
        )
        counts = state.RunCounts()
        status = "succeeded"
        for ref in refs:
            try:
                counts.add(await workflow.execute_activity_method(
                    acts.process, ProcessRequest(req.entity, ref, False),
                    start_to_close_timeout=_PAGE, retry_policy=RETRY,
                ))
            except ActivityError as exc:
                await workflow.execute_activity_method(
                    acts.record_dead_letter, DeadLetter(run_id, req.entity, ref, _error(exc)),
                    start_to_close_timeout=_SHORT, retry_policy=RETRY,
                )
                status = "dead_lettered"
                break
        await workflow.execute_activity_method(
            acts.record_run,
            RunUpdate(run_id, req.entity, "replay", workflow.now().isoformat(), status, counts),
            start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        mark = await workflow.execute_activity_method(
            acts.read_watermark, req.entity, start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        return RunReport(run_id, req.entity, "replay", status, counts, mark)


# How a re-process request's end reads in the run log.
_RUN_STATUS: Final = {"succeeded": "succeeded", "failed": "dead_lettered", "refused": "failed"}


def _refused(exc: ActivityError) -> bool:
    cause = exc.cause
    return isinstance(cause, ApplicationError) and cause.type == "ReprocessRefusedError"


@workflow.defn
class ServiceNowReprocess:
    @workflow.run
    async def run(self) -> ReprocessReport:
        acts = ServiceNowActivities
        claim = await workflow.execute_activity_method(
            acts.claim_reprocess, start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        if claim is None:
            return ReprocessReport(None, "idle", 0, 0)
        refusal = await workflow.execute_activity_method(
            acts.check_reprocess, claim, start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        moved: set[str] = set()
        pages = 0
        status, outcome = ("refused", refusal) if refusal else ("succeeded", None)
        for entity in sorted(ENTITIES) if refusal is None else []:
            run_id = str(workflow.uuid4())
            await workflow.execute_activity_method(
                acts.record_run, RunUpdate(run_id, entity, "replay", workflow.now().isoformat()),
                start_to_close_timeout=_SHORT, retry_policy=RETRY,
            )
            refs = await workflow.execute_activity_method(
                acts.list_raw, ReplayRequest(entity, claim.start, claim.end),
                start_to_close_timeout=_PAGE, retry_policy=RETRY,
            )
            counts = state.RunCounts()
            for ref in refs:
                try:
                    done = await workflow.execute_activity_method(
                        acts.reprocess, ReprocessPageRequest(claim, entity, ref),
                        start_to_close_timeout=_PAGE, retry_policy=RETRY,
                    )
                except ActivityError as exc:
                    if _refused(exc):
                        status, outcome = "refused", "mapping_changed"
                    else:
                        await workflow.execute_activity_method(
                            acts.record_dead_letter, DeadLetter(run_id, entity, ref, _error(exc)),
                            start_to_close_timeout=_SHORT, retry_policy=RETRY,
                        )
                        status, outcome = "failed", "page_dead_lettered"
                    break
                pages += 1
                counts.pages += 1
                counts.records += done.matched
                counts.written += len(set(done.inserted) - moved)
                moved.update(done.inserted)
            await workflow.execute_activity_method(
                acts.record_run,
                RunUpdate(run_id, entity, "replay", workflow.now().isoformat(),
                          _RUN_STATUS[status], counts),
                start_to_close_timeout=_SHORT, retry_policy=RETRY,
            )
            if status != "succeeded":
                break
        await workflow.execute_activity_method(
            acts.finish_reprocess,
            ReprocessFinish(claim, status, len(moved) if status != "refused" else None,
                            pages, outcome),
            start_to_close_timeout=_SHORT, retry_policy=RETRY,
        )
        return ReprocessReport(claim.id, status, len(moved), pages)


def activities(acts: ServiceNowActivities) -> list[Callable[..., Any]]:
    return [acts.read_watermark, acts.fetch_page, acts.process, acts.list_raw,
            acts.record_run, acts.record_dead_letter, acts.claim_reprocess,
            acts.check_reprocess, acts.reprocess, acts.finish_reprocess]


WORKFLOWS: Final = [ServiceNowIngest, ServiceNowReplay, ServiceNowReprocess]
