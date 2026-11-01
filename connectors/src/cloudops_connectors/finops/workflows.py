"""The FinOps state machine (CLAUDE.md rule 3: Temporal owns it).

``FinOpsDaily``: reconcile, then anomalies, then margin, for one business day.
The order matters: margin reads the cost reconciliation just wrote about. Each
step is an activity with the connector retry policy; a failed step fails the
run after its retries, and the next day's run recomputes everything it covers.

``FinOpsRecompute``: every waiting "recompute margin now" request (migration 0032,
ADR 0034), each customer's daily margin work as of today. A schedule runs it every
minute; the API only records the request.

``FinOpsReprocess``: month-end reprocessing of one closed month (billing restated
or a rate corrected): the invoice reconciliation of that month, then margin.

Workflow code is deterministic: dates arrive as ISO strings in the request.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Final

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from cloudops_connectors.core.ingest import RETRY
    from cloudops_connectors.finops import jobs
    from cloudops_tenancy import Database

TASK_QUEUE: Final = "finops"
LIMIT: Final = timedelta(minutes=30)


@dataclass(frozen=True)
class DayRequest:
    # ISO date: the run covers the days before it. Empty: the day the workflow
    # starts (workflow time), so a schedule's stored request never goes stale.
    on: str = ""

    def resolved(self) -> DayRequest:
        return self if self.on else DayRequest(workflow.now().date().isoformat())


@dataclass(frozen=True)
class PeriodRequest:
    period: str  # ISO date in the month to reprocess


class FinOpsActivities:
    def __init__(self, db: Database) -> None:
        self._db = db

    @activity.defn(name="finops_reconcile")
    async def reconcile(self, req: DayRequest) -> dict[str, Any]:
        return asdict(await jobs.reconcile(self._db, date.fromisoformat(req.on)))

    @activity.defn(name="finops_anomalies")
    async def anomalies(self, req: DayRequest) -> dict[str, Any]:
        return asdict(await jobs.anomalies(self._db, date.fromisoformat(req.on)))

    @activity.defn(name="finops_margins")
    async def margins(self, req: DayRequest) -> dict[str, Any]:
        return asdict(await jobs.margins(self._db, date.fromisoformat(req.on)))

    @activity.defn(name="finops_recompute")
    async def recompute(self, req: DayRequest) -> dict[str, Any]:
        return asdict(await jobs.recompute_pending(self._db, date.fromisoformat(req.on)))

    @activity.defn(name="finops_reprocess")
    async def reprocess(self, req: PeriodRequest) -> dict[str, Any]:
        return asdict(await jobs.reprocess(self._db, date.fromisoformat(req.period)))

    @activity.defn(name="finops_reconcile_close")
    async def reconcile_close(self, req: PeriodRequest) -> dict[str, Any]:
        """The invoice check of a closed month, run as of its following month."""
        start = jobs.month_start(date.fromisoformat(req.period))
        return asdict(await jobs.reconcile(self._db, jobs.add_months(start, 1)
                                           + timedelta(days=jobs.CLOSE_DAY)))


def activities(acts: FinOpsActivities) -> list[Any]:
    return [acts.reconcile, acts.anomalies, acts.margins, acts.reprocess, acts.reconcile_close,
            acts.recompute]


@workflow.defn
class FinOpsDaily:
    @workflow.run
    async def run(self, req: DayRequest) -> dict[str, Any]:
        req = req.resolved()
        out: dict[str, Any] = {"on": req.on}
        for name, fn in (("reconcile", FinOpsActivities.reconcile),
                         ("anomalies", FinOpsActivities.anomalies),
                         ("margins", FinOpsActivities.margins)):
            out[name] = await workflow.execute_activity_method(
                fn, req, start_to_close_timeout=LIMIT, retry_policy=RETRY)
        return out


@workflow.defn
class FinOpsReprocess:
    @workflow.run
    async def run(self, req: PeriodRequest) -> dict[str, Any]:
        out: dict[str, Any] = {"period": req.period}
        out["reconcile"] = await workflow.execute_activity_method(
            FinOpsActivities.reconcile_close, req, start_to_close_timeout=LIMIT,
            retry_policy=RETRY)
        out["margins"] = await workflow.execute_activity_method(
            FinOpsActivities.reprocess, req, start_to_close_timeout=LIMIT, retry_policy=RETRY)
        return out


@workflow.defn
class FinOpsRecompute:
    @workflow.run
    async def run(self, req: DayRequest) -> dict[str, Any]:
        req = req.resolved()
        result: dict[str, Any] = await workflow.execute_activity_method(
            FinOpsActivities.recompute, req, start_to_close_timeout=LIMIT, retry_policy=RETRY)
        return result


WORKFLOWS: Final = [FinOpsDaily, FinOpsReprocess, FinOpsRecompute]
