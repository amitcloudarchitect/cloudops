"""The connector contract as one Temporal workflow, for every step 15 source.

ServiceNow (``servicenow/workflows.py``) is the reference. The sources built
after it differ only in how they fetch and how they normalise, so the
orchestration lives here once and each source supplies a ``Source``:

    start run -> scopes -> per scope: read watermark and cursor
      loop: fetch page (raw to bronze, then inspect) -> process page
            (normalise, resolve, quarantine, commit with the watermark)
    finish run (the health report)

- **Scopes** are what a source is read per: a SharePoint site, an Entra
  directory, or the one instance (``""``). Each has its own watermark and
  cursor. A page that fails dead-letters and ends *that scope's* run; the other
  scopes still run, so one customer's bad page does not stall the rest.
- **Watermark and cursor** are read once per scope. ``since`` is the watermark
  minus five minutes (LLD). The cursor is a source's own continuation state (a
  Graph delta link); between pages the workflow carries the next-page cursor,
  and only a committing page persists anything.
- **Raw before parse**: ``fetch_page`` stores the body before it is inspected.
  ``Source.inspect`` may store more raw payloads (a SharePoint file's content)
  before anything parses those.
- **Revocation**: a fetch that fails with ``ScopeRevokedError`` (SharePoint's
  per-site consent withdrawn) is not retried; the source's ``revoke`` runs,
  which removes what that consent covered.
- **Replay** reads bronze for a date range and runs the same ``process`` with
  ``advance=False``: no watermark, cursor or snapshot close moves.

Retries are Temporal's: exponential backoff, five attempts (LLD). Workflow code
is deterministic; every side effect is an activity.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from cloudops_connectors.core import state
    from cloudops_connectors.core.raw import RawStore, day_prefixes, page_ref, sha256
    from cloudops_tenancy import Database, Role, TenantScope

OVERLAP: Final = timedelta(minutes=5)  # LLD: fetch with a five-minute overlap

RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
    non_retryable_error_types=["RawConflictError", "ValueError", "ScopeRevokedError",
                               "MissingRawError"],
)


class ScopeRevokedError(Exception):
    """The source withdrew access to this scope (e.g. a site's consent). Not
    retried: the scope is marked revoked and its derived data removed."""


class MissingRawError(Exception):
    """A payload the page refers to is not in bronze. Not retried."""


def principal(source: str) -> str:
    return f"connector:{source}"


def scope_key(instance: str, scope: str) -> str:
    """Watermarks are per source, entity and instance, and per scope within it."""
    return f"{instance}|{scope}" if scope else instance


# --- what a source provides ---------------------------------------------------------


@dataclass(frozen=True)
class Fetched:
    body: bytes
    suffix: str = ".json"
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PageInfo:
    records: int
    next_cursor: str | None  # None: this was the scope's last page


@dataclass(frozen=True)
class PageContext:
    run_id: str
    scope: str
    scope_key: str
    run_started: datetime
    final: bool  # the scope's last page in this run
    advance: bool  # False on replay
    run_high_water: datetime | None  # highest update time of earlier pages in this run


@dataclass
class PageOutcome:
    counts: state.RunCounts
    high_water: str | None = None  # ISO: the page's highest update time


class Source(ABC):
    """One source system. ``instance`` names the source instance (a base URL);
    watermarks key on it. Implementations hold their own client."""

    source: str
    entities: frozenset[str]
    instance: str

    async def scopes(self, db: Database, entity: str) -> list[str]:
        """What to read this run. Most sources have one scope, the instance."""
        return [""]

    @abstractmethod
    async def fetch(
        self, entity: str, scope: str, since: datetime, cursor: str | None
    ) -> Fetched:
        """One page, as the source sent it. Raise ``ScopeRevokedError`` when the
        source has withdrawn access to ``scope``."""

    @abstractmethod
    async def inspect(
        self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes, cursor: str | None
    ) -> PageInfo:
        """After the body is in bronze: how many records, and the next cursor."""

    @abstractmethod
    async def process(
        self, db: Database, raw: RawStore, entity: str, ref: str, ctx: PageContext
    ) -> PageOutcome:
        """Read the page from bronze, normalise, resolve, commit (with the
        watermark when ``ctx.advance``)."""

    async def revoke(self, db: Database, entity: str, scope: str) -> int:
        """Access to ``scope`` was withdrawn. Returns how many derived rows went."""
        raise NotImplementedError(f"{self.source} has no revocable scopes")


# --- messages ------------------------------------------------------------------------


@dataclass
class IngestRequest:
    entity: str
    initial_since: str  # ISO 8601, used only when a scope has no watermark yet


@dataclass
class ReplayRequest:
    entity: str
    start: str  # ISO date, inclusive
    end: str


@dataclass
class ScopeState:
    watermark: str | None
    cursor: str | None


@dataclass
class ScopeRequest:
    entity: str
    scope: str


@dataclass
class FetchRequest:
    run_id: str
    entity: str
    scope: str
    since: str
    cursor: str | None
    page: int


@dataclass
class FetchResult:
    raw_ref: str
    records: int
    next_cursor: str | None


@dataclass
class ProcessRequest:
    entity: str
    raw_ref: str
    run_id: str
    run_started: str
    advance: bool
    final: bool = False
    scope: str | None = None  # None: read from the page's bronze metadata (replay)
    run_high_water: str | None = None


@dataclass
class RunUpdate:
    run_id: str
    entity: str
    mode: str
    at: str
    status: str = "running"
    counts: state.RunCounts = field(default_factory=state.RunCounts)
    scopes: list[str] = field(default_factory=list)


@dataclass
class DeadLetter:
    run_id: str
    entity: str
    raw_ref: str
    error: str


@dataclass
class ScopeReport:
    scope: str
    status: str  # succeeded | failed | dead_lettered | revoked
    pages: int
    watermark: str | None


@dataclass
class RunReport:
    run_id: str
    source: str
    entity: str
    mode: str
    status: str
    counts: state.RunCounts
    scopes: list[ScopeReport] = field(default_factory=list)


# --- activities ----------------------------------------------------------------------


class ConnectorActivities:
    """Every side effect, with the source injected: the same activities run
    against fixtures, Azurite and (later) a real instance."""

    def __init__(self, db: Database, raw: RawStore, source: Source) -> None:
        self._db = db
        self._raw = raw
        self._source = source

    def _scope(self) -> TenantScope:
        return TenantScope((), Role.INGEST, principal(self._source.source))

    def _key(self, scope: str) -> str:
        return scope_key(self._source.instance, scope)

    @activity.defn
    async def source_name(self) -> str:
        return self._source.source

    @activity.defn
    async def list_scopes(self, entity: str) -> list[str]:
        return await self._source.scopes(self._db, entity)

    @activity.defn
    async def read_state(self, req: ScopeRequest) -> ScopeState:
        key, src = self._key(req.scope), self._source.source
        async with self._db.transaction(self._scope()) as conn:
            mark = await state.read_watermark(conn, src, req.entity, key)
            cursor = await state.read_cursor(conn, src, req.entity, key)
        return ScopeState(None if mark is None else mark.isoformat(), cursor)

    @activity.defn
    async def fetch_page(self, req: FetchRequest) -> FetchResult:
        src = self._source
        try:
            fetched = await src.fetch(
                req.entity, req.scope, datetime.fromisoformat(req.since), req.cursor
            )
        except ScopeRevokedError as exc:
            raise ApplicationError(str(exc) or "scope revoked", type="ScopeRevokedError",
                                   non_retryable=True) from None
        fetched_at = datetime.now(UTC)
        ref = page_ref(src.source, req.entity, fetched_at, req.run_id, req.page, fetched.suffix)
        # Bronze first: stored before a single byte is parsed.
        await self._raw.put(ref, fetched.body, {
            **fetched.metadata,
            "source": src.source, "entity": req.entity, "run_id": req.run_id,
            "scope": req.scope, "since": req.since, "page": str(req.page),
            "sha256": sha256(fetched.body), "fetched_at": fetched_at.isoformat(),
            "instance": src.instance,
        })
        info = await src.inspect(self._raw, req.entity, req.scope, ref, fetched.body, req.cursor)
        return FetchResult(ref, info.records, info.next_cursor)

    @activity.defn
    async def process(self, req: ProcessRequest) -> PageOutcome:
        scope = req.scope
        if scope is None:
            scope = (await self._raw.meta(req.raw_ref)).get("scope", "")
        ctx = PageContext(
            run_id=req.run_id, scope=scope, scope_key=self._key(scope),
            run_started=datetime.fromisoformat(req.run_started), final=req.final,
            advance=req.advance,
            run_high_water=(None if req.run_high_water is None
                            else datetime.fromisoformat(req.run_high_water)),
        )
        return await self._source.process(self._db, self._raw, req.entity, req.raw_ref, ctx)

    @activity.defn
    async def revoke_scope(self, req: ScopeRequest) -> int:
        return await self._source.revoke(self._db, req.entity, req.scope)

    @activity.defn
    async def list_raw(self, req: ReplayRequest) -> list[str]:
        refs: list[str] = []
        for prefix in day_prefixes(self._source.source, req.entity,
                                   date.fromisoformat(req.start), date.fromisoformat(req.end)):
            refs.extend(await self._raw.list(prefix))
        return refs

    @activity.defn
    async def record_run(self, update: RunUpdate) -> str | None:
        """Start or finish the run's row; on finish, the latest watermark of the
        run's scopes, for the lag figure."""
        at = datetime.fromisoformat(update.at)
        src = self._source.source
        async with self._db.transaction(self._scope()) as conn:
            if update.status == "running":
                await state.start_run(conn, UUID(update.run_id), src, update.entity,
                                      update.mode, at)
                return None
            marks = [m for m in [await state.read_watermark(conn, src, update.entity,
                                                            self._key(s))
                                 for s in update.scopes] if m is not None]
            mark = min(marks) if marks else None  # the most lagging scope
            await state.finish_run(conn, UUID(update.run_id), update.status, update.counts,
                                   at, mark)
        state.report_run(src, update.entity, update.mode, update.status, update.counts)
        return None if mark is None else mark.isoformat()

    @activity.defn
    async def record_dead_letter(self, letter: DeadLetter) -> None:
        async with self._db.transaction(self._scope()) as conn:
            await state.dead_letter(conn, UUID(letter.run_id), self._source.source,
                                    letter.entity, letter.raw_ref, letter.error)


def activities(acts: ConnectorActivities) -> list[Callable[..., Any]]:
    return [acts.source_name, acts.list_scopes, acts.read_state, acts.fetch_page,
            acts.process, acts.revoke_scope, acts.list_raw, acts.record_run,
            acts.record_dead_letter]


# --- workflows -----------------------------------------------------------------------

_SHORT: Final = timedelta(minutes=1)
_PAGE: Final = timedelta(minutes=10)
_STATUS_RANK: Final = {"succeeded": 0, "revoked": 0, "failed": 1, "dead_lettered": 2}


def _error(exc: ActivityError) -> str:
    """Exception type and first line only: never a payload value."""
    cause = exc.cause or exc
    first = str(cause).splitlines()[0] if str(cause) else ""
    kind = cause.type if isinstance(cause, ApplicationError) and cause.type else (
        type(cause).__name__)
    return f"{kind}: {first}"[:500]


def _revoked(exc: ActivityError) -> bool:
    cause = exc.cause
    return isinstance(cause, ApplicationError) and cause.type == "ScopeRevokedError"


def _later(a: str | None, b: str | None) -> str | None:
    if a is None or b is None:
        return a or b
    return max(a, b, key=datetime.fromisoformat)


async def _act(fn: Any, arg: Any = None, limit: timedelta = _SHORT) -> Any:
    if arg is None:
        return await workflow.execute_activity_method(
            fn, start_to_close_timeout=limit, retry_policy=RETRY)
    return await workflow.execute_activity_method(
        fn, arg, start_to_close_timeout=limit, retry_policy=RETRY)


@workflow.defn
class ConnectorIngest:
    @workflow.run
    async def run(self, req: IngestRequest) -> RunReport:
        acts = ConnectorActivities
        source = await _act(acts.source_name)
        run_id = str(workflow.uuid4())
        started = workflow.now()
        await _act(acts.record_run, RunUpdate(run_id, req.entity, "incremental",
                                              started.isoformat()))
        scopes: list[str] = await _act(acts.list_scopes, req.entity)
        counts = state.RunCounts()
        reports: list[ScopeReport] = []
        page = 0
        for scope in scopes:
            st: ScopeState = await _act(acts.read_state, ScopeRequest(req.entity, scope))
            since = (datetime.fromisoformat(st.watermark) - OVERLAP if st.watermark
                     else datetime.fromisoformat(req.initial_since))
            cursor = st.cursor
            status, pages, high = "succeeded", 0, None
            while True:
                try:
                    fetched: FetchResult = await _act(acts.fetch_page, FetchRequest(
                        run_id, req.entity, scope, since.isoformat(), cursor, page), _PAGE)
                except ActivityError as exc:
                    if _revoked(exc):
                        await _act(acts.revoke_scope, ScopeRequest(req.entity, scope), _PAGE)
                        status = "revoked"
                    else:
                        status = "failed"  # nothing reached bronze; the next run starts here
                    break
                page += 1
                try:
                    done: PageOutcome = await _act(acts.process, ProcessRequest(
                        req.entity, fetched.raw_ref, run_id, started.isoformat(), True,
                        final=fetched.next_cursor is None, scope=scope, run_high_water=high,
                    ), _PAGE)
                except ActivityError as exc:
                    await _act(acts.record_dead_letter, DeadLetter(
                        run_id, req.entity, fetched.raw_ref, _error(exc)))
                    status = "dead_lettered"
                    break
                pages += 1
                counts.add(done.counts)
                high = _later(high, done.high_water)
                if fetched.next_cursor is None:
                    break
                cursor = fetched.next_cursor
            st = await _act(acts.read_state, ScopeRequest(req.entity, scope))
            reports.append(ScopeReport(scope, status, pages, st.watermark))
        overall = max((r.status for r in reports), key=_STATUS_RANK.__getitem__,
                      default="succeeded")
        overall = "succeeded" if overall == "revoked" else overall
        await _act(acts.record_run, RunUpdate(run_id, req.entity, "incremental",
                                              workflow.now().isoformat(), overall, counts,
                                              scopes))
        return RunReport(run_id, source, req.entity, "incremental", overall, counts, reports)


@workflow.defn
class ConnectorReplay:
    @workflow.run
    async def run(self, req: ReplayRequest) -> RunReport:
        acts = ConnectorActivities
        source = await _act(acts.source_name)
        run_id = str(workflow.uuid4())
        started = workflow.now()
        await _act(acts.record_run, RunUpdate(run_id, req.entity, "replay", started.isoformat()))
        refs: list[str] = await _act(acts.list_raw, req, _PAGE)
        counts = state.RunCounts()
        status = "succeeded"
        for ref in refs:
            try:
                done: PageOutcome = await _act(acts.process, ProcessRequest(
                    req.entity, ref, run_id, started.isoformat(), False), _PAGE)
            except ActivityError as exc:
                await _act(acts.record_dead_letter, DeadLetter(run_id, req.entity, ref,
                                                               _error(exc)))
                status = "dead_lettered"
                break
            counts.add(done.counts)
        await _act(acts.record_run, RunUpdate(run_id, req.entity, "replay",
                                              workflow.now().isoformat(), status, counts))
        return RunReport(run_id, source, req.entity, "replay", status, counts)


WORKFLOWS: Final = [ConnectorIngest, ConnectorReplay]


def task_queue(source: str) -> str:
    return f"connectors-{source}"
