"""The FinOps worker's jobs (step 16; HLD M10-M12): reconciliation, anomalies and
margin, over the database through ``packages/tenancy``.

Each job computes with ``cloudops_commercial`` (pure) and writes derived figures.
They run on the ingest login under the ``finops`` role class with the principal
``job:finops``, so the commercial policy admits them; the application only reads
what they write. The tenant list comes from ``cloudops.finops_tenants()``, the
only way a machine reads it.

**Reconcile** (daily). For the current and previous invoice periods, each
account's line items (unblended, as billed) are compared with the provider's
latest month-to-date total, summing only the days that total covers, and with
the sum of every invoice document for the period issued by then (invoices,
separate marketplace or support invoices, credit memos). Above 0.5% the check is
unverified and alerts once per account, period, kind and day. An account with
cost and no reference is unverified (``no_reference``) and does not page.

**Anomalies** (daily). Day over day for the last ``RECENT_DAYS`` days and month
over month through the latest one, on amortized consumption cost per account and
currency, against the tenant's thresholds or the platform default, the absolute
floor converted into the account's currency at the rate in force. A day that no
longer crosses (billing restated) is removed. Spikes alert. Only consumption
counts (user decision, 2026-11-01): licence, support, marketplace, savings plan,
tax and credit are booked once a month, on the 1st, so counting them would raise
a day-over-day spike every month and project a month's fixed charges from one day.

**Margin** (daily, closed at month end). One transaction per customer: one
customer's failure stops only theirs, and the run then fails so it retries. The
current month is provisional, month to date. From ``CLOSE_DAY`` the previous
month is closed. Closed months are reprocessed for ``RESTATE_MONTHS`` months,
because billing restates after the fact and Finance may correct a rate: if any
figure moves, a new version is written that names the one it restates. Nothing
closed is overwritten. A provisional month has no band and no gap to target.
Each figure records whether the cost under it reconciled (``cost_verified``):
every account with consumption in the month has a latest check that is
verified, the invoice once there is one, else the provider total.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_commercial import finops, margin
from cloudops_commercial.ratecard import RateCard, rate_card_on
from cloudops_observability import record_reconciliation
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

log = logging.getLogger("cloudops.finops")

PRINCIPAL: Final = "job:finops"
RECENT_DAYS: Final = 3  # day-over-day is recomputed this far back: cost lands late
CLOSE_DAY: Final = 5  # the previous month closes from this day (invoices are issued)
RESTATE_MONTHS: Final = 3  # closed months reprocessed for restatement
SIX: Final = Decimal("0.000001")


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def month_end(d: date) -> date:
    return add_months(d, 1) - timedelta(days=1)


def period_label(d: date) -> str:
    return d.strftime("%Y-%m")


def _rows(result: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in result]


async def tenants(db: Database) -> frozenset[UUID]:
    async with db.transaction(TenantScope((), Role.INGEST, PRINCIPAL)) as conn:
        ids = await conn.scalar(text("SELECT cloudops.finops_tenants()"))
    return frozenset(UUID(str(t)) for t in ids or ())


def _scope(ids: Iterable[UUID]) -> TenantScope:
    return TenantScope(ids, Role.FINOPS, PRINCIPAL)


def _alert(alert: str, **fields: Any) -> None:
    """A P2 alert (LLD, Observability: reconciliation drift pages). Structured, for
    the log alert rule; identifiers only, never a customer name."""
    log.warning("finops.alert %s", alert, extra={"alert": alert, **{
        k: str(v) for k, v in fields.items()}})


# --- FX: the rate in force, or None ---------------------------------------------------------

# Same selection as cloudops.fx_rate_on (0001) but NULL instead of an exception,
# so one missing rate leaves one figure not captured rather than failing the run.
_RATE_ON: Final = text(
    "SELECT f.rate FROM cloudops.fx_rate f WHERE f.from_currency = :f AND f.to_currency = :t"
    " AND f.rate_type = 'average' AND f.effective_date <= :d"
    " ORDER BY f.effective_date DESC LIMIT 1"
)
_MONTH_RATES: Final = text(
    "SELECT f.effective_date, f.rate FROM cloudops.fx_rate f"
    " WHERE f.from_currency = :f AND f.to_currency = :t AND f.rate_type = 'average'"
    "   AND f.effective_date BETWEEN :a AND :b ORDER BY f.effective_date"
)


async def rate_on(conn: ScopedConnection, frm: str, to: str, day: date) -> Decimal | None:
    if frm == to:
        return Decimal(1)
    value = await conn.scalar(_RATE_ON, {"f": frm, "t": to, "d": day})
    return None if value is None else Decimal(value)


async def month_rate(conn: ScopedConnection, frm: str, to: str, start: date,
                     through: date) -> margin.MonthRate | None:
    """The month's average rate: the mean of its daily average rates up to ``through``."""
    if frm == to:
        return margin.MonthRate(Decimal(1), start, through, 1)
    rows = _rows(await conn.execute(_MONTH_RATES, {"f": frm, "t": to, "a": start,
                                                   "b": through}))
    return margin.month_rate((r[0], Decimal(r[1])) for r in rows)


# --- reconciliation ---------------------------------------------------------------------------

# The provider total is a running month-to-date figure: its latest reading wins.
# Invoices are documents: an invoice, a later credit memo, a separate marketplace
# or support invoice all belong to the period, so every one issued by ``on`` is
# summed. Documents in different currencies are not summed: the reference is
# then NULL and the check is unverified (currency_mismatch).
_REFERENCES: Final = text(
    "SELECT * FROM ("
    "  SELECT DISTINCT ON (b.account_id, b.invoice_period)"
    "    b.tenant_id, b.account_id, b.invoice_period, b.kind, b.as_of, b.amount, b.currency"
    "  FROM cloudops.billing_total b"
    "  WHERE b.kind = 'provider_total' AND b.invoice_period = ANY(CAST(:periods AS text[]))"
    "    AND b.as_of <= :on"
    "  ORDER BY b.account_id, b.invoice_period, b.as_of DESC) latest"
    " UNION ALL"
    " SELECT b.tenant_id, b.account_id, b.invoice_period, b.kind, max(b.as_of),"
    "   CASE WHEN count(DISTINCT b.currency) = 1 THEN sum(b.amount) END,"
    "   CASE WHEN count(DISTINCT b.currency) = 1 THEN min(b.currency) END"
    " FROM cloudops.billing_total b"
    " WHERE b.kind = 'invoice' AND b.invoice_period = ANY(CAST(:periods AS text[]))"
    "   AND b.as_of <= :on"
    " GROUP BY b.tenant_id, b.account_id, b.invoice_period, b.kind"
)
_LEDGER: Final = text(
    "SELECT c.tenant_id, c.account_id, c.invoice_period,"
    "   array_agg(DISTINCT c.currency ORDER BY c.currency), coalesce(sum(c.unblended_cost), 0)"
    " FROM cloudops.cost_fact c"
    " WHERE c.invoice_period = ANY(CAST(:periods AS text[])) AND c.usage_date <= :through"
    "   AND (CAST(:account AS uuid) IS NULL OR c.account_id = CAST(:account AS uuid))"
    " GROUP BY c.tenant_id, c.account_id, c.invoice_period"
)
_RECORD_CHECK: Final = text(
    "INSERT INTO cloudops.cost_reconciliation (tenant_id, account_id, invoice_period, kind,"
    " checked_on, reference_as_of, ledger_amount, ledger_currency, reference_amount,"
    " reference_currency, drift, status, reason, alerted_at)"
    " VALUES (:tenant_id, :account_id, :period, :kind, :on, :as_of, :ledger, :ledger_ccy,"
    "  :reference, :reference_ccy, :drift, :status, :reason, :alerted_at)"
    " ON CONFLICT (account_id, invoice_period, kind, checked_on) DO UPDATE SET"
    "  reference_as_of = EXCLUDED.reference_as_of, ledger_amount = EXCLUDED.ledger_amount,"
    "  ledger_currency = EXCLUDED.ledger_currency,"
    "  reference_amount = EXCLUDED.reference_amount,"
    "  reference_currency = EXCLUDED.reference_currency, drift = EXCLUDED.drift,"
    "  status = EXCLUDED.status, reason = EXCLUDED.reason,"
    "  alerted_at = coalesce(cloudops.cost_reconciliation.alerted_at, EXCLUDED.alerted_at)"
    " RETURNING (xmax = 0) AS inserted, alerted_at"
)


@dataclass
class ReconcileReport:
    checked: int = 0
    verified: int = 0
    unverified: int = 0
    alerted: int = 0


async def reconcile(db: Database, on: date) -> ReconcileReport:
    """Every account with cost or a reference in the current or previous period."""
    periods = [period_label(add_months(month_start(on), -1)), period_label(on)]
    report = ReconcileReport()
    ids = await tenants(db)
    async with db.transaction(_scope(ids)) as conn:
        refs = _rows(await conn.execute(_REFERENCES, {"periods": periods, "on": on}))
        totals = {(r[1], r[2]): r for r in _rows(await conn.execute(
            _LEDGER, {"periods": periods, "through": on, "account": None}))}
        checks: list[dict[str, Any]] = []
        provider_seen: set[tuple[Any, Any]] = set()
        for tenant, account, period, kind, as_of, amount, ccy in refs:
            through = month_end(date.fromisoformat(f"{period}-01")) if kind == "invoice" \
                else as_of
            ledger = _rows(await conn.execute(_LEDGER, {
                "periods": [period], "through": through, "account": account}))
            currencies, total = (ledger[0][3], Decimal(ledger[0][4])) if ledger else ([], 0)
            checks.append(_check(tenant, account, period, kind, on, as_of, Decimal(total),
                                 list(currencies), None if amount is None else Decimal(amount),
                                 ccy, mixed_reference=amount is None))
            if kind == "provider_total":
                provider_seen.add((account, period))
        for (account, period), (tenant, _a, _p, currencies, total) in sorted(
                totals.items(), key=lambda i: (str(i[0][0]), i[0][1])):
            if (account, period) not in provider_seen:
                checks.append(_check(tenant, account, period, "provider_total", on, None,
                                     Decimal(total), list(currencies), None, None))
        for c in checks:
            row = tuple((await conn.execute(_RECORD_CHECK, c)).one())
            record_reconciliation(kind=c["kind"], status=c["status"], reason=c["reason"])
            report.checked += 1
            if c["status"] == "verified":
                report.verified += 1
            else:
                report.unverified += 1
            if c["alerted_at"] is not None and row[1] == c["alerted_at"]:
                report.alerted += 1
                _alert("reconciliation_drift", account=c["account_id"], period=c["period"],
                       kind=c["kind"], drift=c["drift"])
    return report


def _check(tenant: UUID, account: UUID, period: str, kind: str, on: date, as_of: date | None,
           ledger: Decimal, currencies: Sequence[str], reference: Decimal | None,
           reference_ccy: str | None, *, mixed_reference: bool = False) -> dict[str, Any]:
    ledger_ccy = currencies[0] if len(currencies) == 1 else None
    status: str
    reason: str | None
    drift: Decimal | None
    if mixed_reference:
        status, reason, drift = "unverified", "currency_mismatch", None
    elif reference is None or reference_ccy is None:
        status, reason, drift = "unverified", "no_reference", None
    elif ledger_ccy is None and currencies:
        status, reason, drift = "unverified", "mixed_currency", None
    else:
        r = finops.reconcile(ledger, ledger_ccy or reference_ccy, reference, reference_ccy)
        status, reason, drift = r.status, r.reason, r.drift
    # Pages on drift only: a figure that disagrees with the provider. A missing
    # reference is shown as unverified without waking anyone.
    alert = reason in ("drift", "currency_mismatch", "mixed_currency")
    return {
        "tenant_id": tenant, "account_id": account, "period": period, "kind": kind, "on": on,
        "as_of": as_of, "ledger": ledger, "ledger_ccy": ledger_ccy, "reference": reference,
        "reference_ccy": reference_ccy, "drift": drift, "status": status, "reason": reason,
        "alerted_at": datetime.now(UTC) if alert else None,
    }


# --- anomalies ---------------------------------------------------------------------------------

_DAILY: Final = text(
    "SELECT c.tenant_id, c.account_id, c.currency, c.usage_date, sum(c.amortized_cost)"
    " FROM cloudops.cost_fact c WHERE c.usage_date BETWEEN :a AND :b"
    "   AND c.charge_class = 'consumption'"
    " GROUP BY c.tenant_id, c.account_id, c.currency, c.usage_date"
)
_THRESHOLDS: Final = text(
    "SELECT d.granularity, t.tenant_id, coalesce(t.min_ratio, d.min_ratio),"
    "   coalesce(t.min_amount, d.min_amount), coalesce(t.currency, d.currency)"
    " FROM staging.anomaly_threshold_default d"
    " LEFT JOIN cloudops.anomaly_threshold t ON t.granularity = d.granularity"
)
_DEFAULTS: Final = text(
    "SELECT granularity, min_ratio, min_amount, currency FROM staging.anomaly_threshold_default"
)
_UPSERT_ANOMALY: Final = text(
    "INSERT INTO cloudops.cost_anomaly (tenant_id, account_id, granularity, observed_on,"
    " observed, baseline, change, ratio, currency, direction, threshold_ratio,"
    " threshold_amount, alerted_at)"
    " VALUES (:tenant_id, :account_id, :granularity, :observed_on, :observed, :baseline,"
    "  :change, :ratio, :currency, :direction, :threshold_ratio, :threshold_amount,"
    "  :alerted_at)"
    " ON CONFLICT (account_id, granularity, observed_on, currency) DO UPDATE SET"
    "  observed = EXCLUDED.observed, baseline = EXCLUDED.baseline, change = EXCLUDED.change,"
    "  ratio = EXCLUDED.ratio, direction = EXCLUDED.direction,"
    "  threshold_ratio = EXCLUDED.threshold_ratio,"
    "  threshold_amount = EXCLUDED.threshold_amount, detected_at = now(),"
    "  alerted_at = coalesce(cloudops.cost_anomaly.alerted_at, EXCLUDED.alerted_at)"
    " RETURNING (xmax = 0)"
)
_CLEAR_ANOMALY: Final = text(
    "DELETE FROM cloudops.cost_anomaly WHERE account_id = :account_id"
    " AND granularity = :granularity AND observed_on = :observed_on AND currency = :currency"
)


@dataclass
class AnomalyReport:
    days: int = 0
    found: int = 0
    alerted: int = 0


async def anomalies(db: Database, on: date) -> AnomalyReport:
    """Day over day for the ``RECENT_DAYS`` days before ``on``, and month over
    month through the last of them."""
    report = AnomalyReport()
    last = on - timedelta(days=1)
    first_recent = on - timedelta(days=RECENT_DAYS)
    start = min(first_recent - timedelta(days=finops.TRAILING_DAYS),
                add_months(month_start(last), -1))
    ids = await tenants(db)
    async with db.transaction(_scope(ids)) as conn:
        series: dict[tuple[UUID, UUID, str], dict[date, Decimal]] = defaultdict(dict)
        for tenant, account, ccy, day, amount in _rows(await conn.execute(
                _DAILY, {"a": start, "b": last})):
            series[(tenant, account, ccy)][day] = Decimal(amount)
        defaults = {r[0]: (Decimal(r[1]), Decimal(r[2]), str(r[3]))
                    for r in _rows(await conn.execute(_DEFAULTS))}
        overrides = {(r[1], r[0]): (Decimal(r[2]), Decimal(r[3]), str(r[4]))
                     for r in _rows(await conn.execute(_THRESHOLDS)) if r[1] is not None}

        async def threshold(tenant: UUID, granularity: str, ccy: str,
                            day: date) -> finops.Threshold | None:
            ratio, amount, t_ccy = overrides.get((tenant, granularity),
                                                 defaults[granularity])
            fx = await rate_on(conn, t_ccy, ccy, day)
            if fx is None:
                log.warning("finops.anomaly no rate %s->%s on %s", t_ccy, ccy, day)
                return None
            return finops.Threshold(ratio, amount * fx)

        for (tenant, account, ccy), daily in sorted(series.items(),
                                                     key=lambda i: tuple(map(str, i[0]))):
            found: list[finops.Anomaly] = []
            checked: list[tuple[str, date]] = []
            day = first_recent
            while day <= last:
                t = await threshold(tenant, "day", ccy, day)
                if t is not None:
                    checked.append(("day", day))
                    report.days += 1
                    a = finops.day_over_day(daily, day, t)
                    if a is not None:
                        found.append(a)
                day += timedelta(days=1)
            t = await threshold(tenant, "month", ccy, last)
            if t is not None:
                this = month_start(last)
                prior = add_months(this, -1)
                mtd = sum((v for d, v in daily.items() if d >= this), Decimal(0))
                prior_days = [d for d in daily if prior <= d < this]
                last_month = sum((daily[d] for d in prior_days), Decimal(0)) \
                    if prior_days else None
                checked.append(("month", last))
                a = finops.month_over_month(mtd, last, last.day, month_end(last).day,
                                            last_month, t)
                if a is not None:
                    found.append(a)
            hits = {(a.window, a.observed_on) for a in found}
            for granularity, observed_on in checked:
                if (granularity, observed_on) not in hits:
                    await conn.execute(_CLEAR_ANOMALY, {
                        "account_id": account, "granularity": granularity,
                        "observed_on": observed_on, "currency": ccy})
            for a in found:
                t = await threshold(tenant, a.window, ccy, a.observed_on)
                assert t is not None  # noqa: S101 - found only where a threshold was
                spike = a.direction == "spike"
                inserted = (await conn.execute(_UPSERT_ANOMALY, {
                    "tenant_id": tenant, "account_id": account, "granularity": a.window,
                    "observed_on": a.observed_on, "observed": a.observed,
                    "baseline": a.baseline, "change": a.change, "ratio": a.ratio,
                    "currency": ccy, "direction": a.direction,
                    "threshold_ratio": t.min_ratio, "threshold_amount": t.min_amount,
                    "alerted_at": datetime.now(UTC) if spike else None})).scalar()
                report.found += 1
                if spike and inserted:
                    report.alerted += 1
                    _alert("cost_anomaly", account=account, window=a.window,
                           observed_on=a.observed_on, ratio=a.ratio)
    return report


# --- margin --------------------------------------------------------------------------------------

_TENANT: Final = text(
    "SELECT delivery_location::text, coverage::text, support::text FROM cloudops.tenant"
    " WHERE id = :t"
)
_SOW: Final = text(
    "SELECT id, currency, monthly_manage_revenue, target_margin, coverage::text,"
    "   support::text FROM cloudops.sow"
    " WHERE tenant_id = :t AND effective_from <= :d"
    "   AND (effective_to IS NULL OR effective_to >= :d)"
    " ORDER BY effective_from DESC LIMIT 1"
)
_INVENTORY: Final = text(
    "SELECT type_canonical, count(*) FROM cloudops.resource"
    " WHERE tenant_id = :t AND is_critical_ci AND first_seen < :end"
    "   AND (deleted_at IS NULL OR deleted_at >= :start)"
    " GROUP BY type_canonical"
)
_CONSUMPTION: Final = text(
    "SELECT c.usage_date, c.currency, sum(c.amortized_cost) FROM cloudops.cost_fact c"
    " WHERE c.tenant_id = :t AND c.charge_class = 'consumption'"
    "   AND c.usage_date BETWEEN :a AND :b"
    " GROUP BY c.usage_date, c.currency"
)
_EFFORT: Final = text(
    "SELECT coalesce(nullif(btrim(w.assignment_workgroup), ''), '(unassigned)'), w.kind,"
    "   coalesce(sum(w.actual_hours), 0), count(*), count(*) FILTER (WHERE w.actual_hours IS NULL)"
    " FROM cloudops.work_item w"
    " WHERE w.tenant_id = :t AND w.closed_at >= :start AND w.closed_at < :end"
    " GROUP BY 1, 2"
)
# Per account with consumption in the invoice period (checks are keyed by it, not
# by usage date), its latest check: the invoice once one exists, else the
# provider total. An account never checked counts against.
_COST_VERIFIED: Final = text(
    "WITH accounts AS ("
    "  SELECT DISTINCT c.account_id FROM cloudops.cost_fact c"
    "  WHERE c.tenant_id = :t AND c.charge_class = 'consumption'"
    "    AND c.invoice_period = :p AND c.usage_date <= :b),"
    " latest AS ("
    "  SELECT DISTINCT ON (r.account_id) r.account_id, r.status"
    "  FROM cloudops.cost_reconciliation r"
    "  WHERE r.tenant_id = :t AND r.invoice_period = :p"
    "  ORDER BY r.account_id, (r.kind = 'invoice') DESC, r.checked_on DESC)"
    " SELECT count(*), count(*) FILTER (WHERE l.status = 'verified')"
    " FROM accounts a LEFT JOIN latest l ON l.account_id = a.account_id"
)
_CURRENT: Final = text(
    "SELECT id, version, status, revenue, modelled_cost, actual_cost, margin_pct,"
    "   not_captured, effort_hours, effort_items_without_hours, consumption,"
    "   fx_rate, prior_fx_rate, margin_at_prior_rates, through, band, cost_verified"
    " FROM cloudops.margin_snapshot WHERE tenant_id = :t AND period = :p"
    " ORDER BY version DESC LIMIT 1"
)
_SNAPSHOT_COLUMNS: Final = (
    "tenant_id", "sow_id", "period", "through", "version", "status", "restates",
    "rate_card_version", "not_captured", "contract_currency", "revenue", "modelled_cost",
    "actual_cost", "tooling_cost", "modelled_margin", "actual_margin", "margin_at_prior_rates",
    "margin_pct", "band_basis", "band", "target_margin", "gap_to_target", "fte",
    "modelled_hours", "effort_hours", "effort_items", "effort_items_without_hours",
    "effort_complete", "unpriced_cis", "exceeds_slab", "consumption", "consumption_currency",
    "fx_rate", "fx_rate_from", "fx_rate_to", "prior_fx_rate", "prior_fx_rate_from",
    "prior_fx_rate_to", "cost_verified",
)
_INSERT_SNAPSHOT: Final = text(
    f"INSERT INTO cloudops.margin_snapshot ({', '.join(_SNAPSHOT_COLUMNS)})"  # noqa: S608
    f" VALUES ({', '.join(':' + c for c in _SNAPSHOT_COLUMNS)}) RETURNING id"
)
_UPDATE_SNAPSHOT: Final = text(
    "UPDATE cloudops.margin_snapshot SET computed_at = now(), "  # noqa: S608
    + ", ".join(f"{c} = :{c}" for c in _SNAPSHOT_COLUMNS
                if c not in ("tenant_id", "period", "version", "restates"))
    + " WHERE id = :id"
)
_CLEAR_VARIANCE: Final = text("DELETE FROM cloudops.margin_variance WHERE snapshot_id = :id")
_INSERT_VARIANCE: Final = text(
    "INSERT INTO cloudops.margin_variance (tenant_id, snapshot_id, workgroup, activity,"
    " hours, items, items_without_hours, share, excess_hours, excess_cost, currency)"
    " VALUES (:tenant_id, :snapshot_id, :workgroup, :activity, :hours, :items,"
    "  :items_without_hours, :share, :excess_hours, :excess_cost, :currency)"
)


@dataclass
class MarginReport:
    computed: int = 0
    closed: int = 0
    restated: int = 0
    not_captured: int = 0
    failed: int = 0


class MarginRunError(RuntimeError):
    """Some customers' margin failed; the others were written. Raised after the
    run so the activity retries (recomputing is idempotent) and the failure shows."""


async def _per_tenant(db: Database, report: MarginReport,
                      work: Callable[[ScopedConnection, UUID], Awaitable[None]]) -> MarginReport:
    """One transaction per customer: one customer's failure stops only theirs."""
    for tenant in sorted(await tenants(db), key=str):
        try:
            async with db.transaction(_scope([tenant])) as conn:
                await work(conn, tenant)
        except Exception:
            report.failed += 1
            log.exception("finops.margin failed tenant=%s", tenant)
            _alert("margin_failed", tenant=tenant)
    if report.failed:
        raise MarginRunError(f"margin failed for {report.failed} customer(s)")
    return report


async def cost_verified(conn: ScopedConnection, tenant: UUID, period: date,
                        through: date) -> bool | None:
    """Did the month's cost reconcile? None when there is no cost to verify."""
    accounts, verified = _rows(await conn.execute(_COST_VERIFIED, {
        "t": tenant, "b": through, "p": period_label(period)}))[0]
    return None if not accounts else accounts == verified


async def _inputs(conn: ScopedConnection, tenant: UUID, period: date, through: date,
                  card: RateCard, provisional: bool = False,
                  ) -> tuple[margin.MarginInputs, UUID | None]:
    end = add_months(period, 1)
    loc, t_cov, t_sup = _rows(await conn.execute(_TENANT, {"t": tenant}))[0]
    sow = _rows(await conn.execute(_SOW, {"t": tenant, "d": through}))
    sow_id, ccy, revenue, target, s_cov, s_sup = (
        sow[0] if sow else (None, None, None, None, None, None))
    priced: dict[str, int] = defaultdict(int)
    unpriced = 0
    for type_canonical, n in _rows(await conn.execute(_INVENTORY, {
            "t": tenant, "start": period, "end": end})):
        row = card.catalog_type_of.get(type_canonical)
        if row is None:
            unpriced += int(n)
        else:
            priced[row] += int(n)
    consumption = Decimal(0)
    missing_rate = False
    lines = _rows(await conn.execute(_CONSUMPTION, {"t": tenant, "a": period, "b": through}))
    for day, c_ccy, amount in lines:
        fx = await rate_on(conn, c_ccy, card.currency, day)
        if fx is None:
            missing_rate = True
            continue
        consumption += Decimal(amount) * fx
    effort = tuple(
        margin.EffortBucket(str(r[0]), str(r[1]), Decimal(r[2]), int(r[3]), int(r[4]))
        for r in _rows(await conn.execute(_EFFORT, {
            "t": tenant, "start": datetime.combine(period, datetime.min.time(), UTC),
            "end": datetime.combine(min(end, through + timedelta(days=1)),
                                    datetime.min.time(), UTC)})))
    contract = ccy or card.currency
    rate = prior = None
    if sow and not missing_rate:
        rate = await month_rate(conn, card.currency, contract, period, through)
        prior_start = add_months(period, -1)
        prior = await month_rate(conn, card.currency, contract, prior_start,
                                 month_end(prior_start))
    return margin.MarginInputs(
        period=period, contract_currency=contract,
        revenue=None if revenue is None else Decimal(revenue),
        target_margin=None if target is None else Decimal(target),
        location=str(loc), coverage=s_cov or t_cov, support=s_sup or t_sup,
        priced_inventory=dict(priced), unpriced_cis=unpriced, consumption_eur=consumption,
        has_cost_data=bool(lines), effort=effort, rate=rate, prior_rate=prior,
        provisional=provisional), sow_id


def _snapshot_row(f: margin.MarginFigures, tenant: UUID, sow_id: UUID | None, through: date,
                  status: str, version: int, restates: UUID | None, card: RateCard,
                  consumption: Decimal, verified: bool | None) -> dict[str, Any]:
    return {
        "tenant_id": tenant, "sow_id": sow_id, "period": f.period, "through": through,
        "version": version, "status": status, "restates": restates,
        "rate_card_version": f.rate_card_version, "not_captured": f.not_captured,
        "contract_currency": f.contract_currency if f.revenue is not None else None,
        "revenue": f.revenue, "modelled_cost": f.modelled_cost, "actual_cost": f.actual_cost,
        "tooling_cost": f.tooling_cost, "modelled_margin": f.modelled_margin,
        "actual_margin": f.actual_margin, "margin_at_prior_rates": f.margin_at_prior_rates,
        "margin_pct": f.margin_pct, "band_basis": f.band_basis, "band": f.band,
        "target_margin": f.target_margin, "gap_to_target": f.gap_to_target, "fte": f.fte,
        "modelled_hours": f.modelled_hours, "effort_hours": f.effort_hours,
        "effort_items": f.effort_items,
        "effort_items_without_hours": f.effort_items_without_hours,
        "effort_complete": f.effort_complete, "unpriced_cis": f.unpriced_cis,
        "exceeds_slab": f.exceeds_slab, "consumption": consumption,
        "consumption_currency": card.currency,
        "fx_rate": None if f.rate is None else f.rate.rate,
        "fx_rate_from": None if f.rate is None else f.rate.first_day,
        "fx_rate_to": None if f.rate is None else f.rate.last_day,
        "prior_fx_rate": None if f.prior_rate is None else f.prior_rate.rate,
        "prior_fx_rate_from": None if f.prior_rate is None else f.prior_rate.first_day,
        "prior_fx_rate_to": None if f.prior_rate is None else f.prior_rate.last_day,
        "cost_verified": verified,
    }


def _q(v: Any, places: str = "0.000001") -> Any:
    return None if v is None else Decimal(v).quantize(Decimal(places))


def _moved(stored: tuple[Any, ...], row: dict[str, Any]) -> bool:
    """Has any figure a reader sees changed, at the precision it is stored in?"""
    (_id, _v, _s, revenue, modelled, actual, pct, not_captured, hours, missing, consumption,
     fx, prior_fx, at_prior, through, band, verified) = stored
    return bool(
        _q(revenue) != _q(row["revenue"]) or _q(modelled) != _q(row["modelled_cost"])
        or _q(actual) != _q(row["actual_cost"])
        or _q(pct, "0.00000001") != _q(row["margin_pct"], "0.00000001")
        or not_captured != row["not_captured"]
        or _q(hours, "0.0001") != _q(row["effort_hours"], "0.0001")
        or missing != row["effort_items_without_hours"]
        or _q(consumption) != _q(row["consumption"])
        or _q(fx, "0.00000001") != _q(row["fx_rate"], "0.00000001")
        or _q(prior_fx, "0.00000001") != _q(row["prior_fx_rate"], "0.00000001")
        or _q(at_prior) != _q(row["margin_at_prior_rates"])
        or through != row["through"]
        or band != row["band"]
        or verified != row["cost_verified"]
    )


async def _write_variance(conn: ScopedConnection, snapshot: UUID, tenant: UUID,
                          f: margin.MarginFigures) -> None:
    await conn.execute(_CLEAR_VARIANCE, {"id": snapshot})
    if f.variance:
        await conn.execute(_INSERT_VARIANCE, [{
            "tenant_id": tenant, "snapshot_id": snapshot, "workgroup": v.workgroup,
            "activity": v.activity, "hours": v.hours, "items": v.items,
            "items_without_hours": v.items_without_hours, "share": v.share,
            "excess_hours": v.excess_hours, "excess_cost": v.excess_cost,
            "currency": f.contract_currency} for v in f.variance])


async def compute_period(conn: ScopedConnection, tenant: UUID, period: date, through: date,
                         close: bool, report: MarginReport) -> None:
    """Compute one customer-month and store it by the rules above."""
    card = rate_card_on(period)
    inputs, sow_id = await _inputs(conn, tenant, period, through, card, provisional=not close)
    verified = await cost_verified(conn, tenant, period, through)
    figures = margin.compute(inputs, card)
    report.computed += 1
    if figures.not_captured is not None:
        report.not_captured += 1
    current = _rows(await conn.execute(_CURRENT, {"t": tenant, "p": period}))
    status = "closed" if close else "provisional"
    if not current:
        row = _snapshot_row(figures, tenant, sow_id, through, status, 1, None, card,
                            inputs.consumption_eur, verified)
        snapshot = (await conn.execute(_INSERT_SNAPSHOT, row)).scalar_one()
        await _write_variance(conn, snapshot, tenant, figures)
        report.closed += close
        return
    stored = current[0]
    snapshot_id, version, stored_status = stored[0], int(stored[1]), str(stored[2])
    if stored_status == "provisional":
        row = _snapshot_row(figures, tenant, sow_id, through, status, version, None, card,
                            inputs.consumption_eur, verified)
        await conn.execute(_UPDATE_SNAPSHOT, {**row, "id": snapshot_id})
        await _write_variance(conn, snapshot_id, tenant, figures)
        report.closed += close
        return
    # Closed: never overwritten. A restatement is a new version naming the old.
    row = _snapshot_row(figures, tenant, sow_id, through, "closed", version + 1, snapshot_id,
                        card, inputs.consumption_eur, verified)
    if _moved(stored, row):
        snapshot = (await conn.execute(_INSERT_SNAPSHOT, row)).scalar_one()
        await _write_variance(conn, snapshot, tenant, figures)
        report.restated += 1
        log.info("finops.margin restated period=%s version=%s", period, version + 1)


async def margins(db: Database, on: date) -> MarginReport:
    """The daily margin run for the day before ``on`` (cost lands a day late)."""
    report = MarginReport()
    last = on - timedelta(days=1)
    this = month_start(last)
    previous = add_months(this, -1)
    close_previous = on >= add_months(previous, 1).replace(day=CLOSE_DAY)

    async def work(conn: ScopedConnection, tenant: UUID) -> None:
        await compute_period(conn, tenant, this, last, close=False, report=report)
        await compute_period(conn, tenant, previous, month_end(previous),
                             close=close_previous, report=report)
        for k in range(1, RESTATE_MONTHS + 1):
            older = add_months(previous, -k)
            await compute_period(conn, tenant, older, month_end(older), close=True,
                                 report=report)

    return await _per_tenant(db, report, work)


async def reprocess(db: Database, period: date) -> MarginReport:
    """Month-end reprocessing of one closed month for every customer: billing
    restated, or Finance corrected a rate. Restates only what moved."""
    report = MarginReport()
    start = month_start(period)

    async def work(conn: ScopedConnection, tenant: UUID) -> None:
        await compute_period(conn, tenant, start, month_end(start), close=True, report=report)

    return await _per_tenant(db, report, work)
