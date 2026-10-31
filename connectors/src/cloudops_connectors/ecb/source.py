"""ECB reference rates through the connector contract (``core/ingest.py``).

One scope, one page a run: the history file.

fetch     GET the history file; the body is the page (stored in bronze first)
inspect   how many days it holds (a file that does not parse is left for
          ``process`` to fail, so it dead-letters); there is never a next page
process   parse in Decimal, derive every directed rate of the currency set
          (``mapping.rates``) for days from ``since`` minus ``LOOKBACK``, and
          store them in one transaction with the watermark (the newest ECB
          day, midnight UTC)

Storing a rate, against what ``cloudops.fx_rate`` already holds for its
(from, to, type, day):

- nothing: inserted, ``source`` ``ecb``;
- the same value: unchanged;
- a different value from ``ecb``: the ECB revised it. Updated, and an explicit
  ``cloudops.fx_restatement`` entry records the previous and new rate (HLD,
  Multi-currency: a correction is never a silent overwrite);
- a different value from another feed (the synthetic seed locally): replaced
  by the ECB's, with the same explicit entry;
- anything from ``finance``: never touched. A finance correction wins; the
  rate is counted as skipped.

The update re-checks the row's source under its row lock, so a finance
correction committed while the page ran is not overwritten either; an insert
that loses a race to one does nothing.

``fx_rate`` and ``fx_restatement`` are reference data with no tenant, so the
transaction is scoped to no tenant at all. Replay re-runs the same page with
``advance=False``: rates are compared again (a no-op when nothing changed), the
watermark stays where it is.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from sqlalchemy import text

from cloudops_connectors.core import state
from cloudops_connectors.core.ingest import (
    Fetched,
    PageContext,
    PageInfo,
    PageOutcome,
    Source,
    principal,
)
from cloudops_connectors.core.raw import RawStore
from cloudops_connectors.ecb import mapping
from cloudops_connectors.ecb.client import EcbClient
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

# Days before ``since`` still compared each run, so a revision the ECB makes to
# a recent rate is picked up. The first run (no watermark) stores everything.
LOOKBACK: Final = timedelta(days=31)
FINANCE: Final = "finance"
REVISED: Final = "ECB revised the published rate"
REPLACED: Final = "ECB rate replaced a rate from another source"

_EXISTING: Final = text(
    "SELECT from_currency, to_currency, rate_type, effective_date, rate, source"
    " FROM cloudops.fx_rate WHERE effective_date BETWEEN :lo AND :hi"
    " AND from_currency = ANY(CAST(:ccys AS text[]))"
    " AND to_currency = ANY(CAST(:ccys AS text[]))"
)
_INSERT: Final = text(
    "INSERT INTO cloudops.fx_rate (from_currency, to_currency, rate, rate_type,"
    " effective_date, source)"
    " VALUES (:from_currency, :to_currency, :rate, :rate_type, :effective_date, :source)"
    " ON CONFLICT (from_currency, to_currency, rate_type, effective_date) DO NOTHING"
)
_REVISE: Final = text(
    "UPDATE cloudops.fx_rate f SET rate = :rate, source = :source"
    " FROM (SELECT id, rate, source FROM cloudops.fx_rate"
    "       WHERE from_currency = :from_currency AND to_currency = :to_currency"
    "         AND rate_type = :rate_type AND effective_date = :effective_date"
    "       FOR UPDATE) o"
    " WHERE f.id = o.id AND o.source <> :finance AND o.rate <> :rate"
    " RETURNING o.rate, o.source"
)
_RESTATE: Final = text(
    "INSERT INTO cloudops.fx_restatement (from_currency, to_currency, rate_type,"
    " effective_date, previous_rate, new_rate, source, reason, entered_by)"
    " VALUES (:from_currency, :to_currency, :rate_type, :effective_date, :previous,"
    "         :rate, :source, :reason, :entered_by)"
)


@dataclass
class FxOutcome:
    inserted: int = 0
    revised: int = 0  # updated, each with its fx_restatement entry
    unchanged: int = 0
    skipped: int = 0  # a finance correction holds the rate


def _params(r: mapping.Rate) -> dict[str, object]:
    return {"from_currency": r.from_currency, "to_currency": r.to_currency,
            "rate_type": r.rate_type, "effective_date": r.effective_date, "rate": r.rate,
            "source": mapping.SOURCE}


async def store(conn: ScopedConnection, rates: Iterable[mapping.Rate]) -> FxOutcome:
    """Insert, revise or skip each rate (see the module docstring)."""
    wanted = list(rates)
    out = FxOutcome()
    if not wanted:
        return out
    ccys = sorted({r.from_currency for r in wanted} | {r.to_currency for r in wanted})
    held: dict[tuple[str, str, str, date], tuple[object, str]] = {}
    for row in await conn.execute(_EXISTING, {
            "lo": min(r.effective_date for r in wanted),
            "hi": max(r.effective_date for r in wanted), "ccys": ccys}):
        f, t, kind, day, rate, source = tuple(row)
        held[(str(f), str(t), str(kind), day)] = (rate, str(source))
    inserts: list[dict[str, object]] = []
    for r in wanted:
        existing = held.get((r.from_currency, r.to_currency, r.rate_type, r.effective_date))
        if existing is None:
            inserts.append(_params(r))
            continue
        rate, source = existing
        if source == FINANCE:
            out.skipped += 1
        elif rate == r.rate:
            out.unchanged += 1
        else:
            done = (await conn.execute(_REVISE, {**_params(r), "finance": FINANCE})).one_or_none()
            if done is None:  # a finance correction landed meanwhile, or nothing changed
                out.skipped += 1
                continue
            previous, was = tuple(done)
            await conn.execute(_RESTATE, {
                **_params(r), "previous": previous,
                "reason": REVISED if was == mapping.SOURCE else REPLACED,
                "entered_by": principal(mapping.SOURCE)})
            out.revised += 1
    if inserts:
        result = await conn.execute(_INSERT, inserts)
        added = result.rowcount if isinstance(result.rowcount, int) else len(inserts)
        out.inserted += max(added, 0)
        out.skipped += len(inserts) - max(added, 0)
    return out


class EcbSource(Source):
    source = mapping.SOURCE
    entities = mapping.ENTITIES

    def __init__(self, client: EcbClient,
                 currencies: Iterable[str] = mapping.DEFAULT_CURRENCIES) -> None:
        self._client = client
        self.currencies = mapping.currency_set(currencies)
        self.instance = f"{client.base_url}{client.path}"
        self.last: FxOutcome | None = None  # the last page's outcome, for tests and logs

    @staticmethod
    def _check(entity: str) -> None:
        if entity not in mapping.ENTITIES:
            raise ValueError(f"unknown entity {entity!r}")

    async def fetch(
        self, entity: str, scope: str, since: datetime, cursor: str | None
    ) -> Fetched:
        self._check(entity)
        return Fetched(await self._client.history(), ".xml")

    async def inspect(
        self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes, cursor: str | None
    ) -> PageInfo:
        self._check(entity)
        try:
            days = len(mapping.history(body).days)
        except ValueError:
            days = 0  # ``process`` fails it: dead-lettered with its bronze reference
        return PageInfo(days, None)

    async def process(
        self, db: Database, raw: RawStore, entity: str, ref: str, ctx: PageContext
    ) -> PageOutcome:
        self._check(entity)
        h = mapping.history(await raw.get(ref))
        since = datetime.fromisoformat((await raw.meta(ref))["since"])
        rates = mapping.rates(h, self.currencies, (since - LOOKBACK).date())
        async with db.transaction(TenantScope((), Role.INGEST,
                                              principal(self.source))) as conn:
            outcome = await store(conn, rates)
            if ctx.advance and h.newest is not None:
                await state.advance_watermark(conn, self.source, entity, ctx.scope_key,
                                              datetime.combine(h.newest, time.min, UTC))
        self.last = outcome
        counts = state.RunCounts(pages=1, records=len(rates), rejected=h.rejected,
                                 written=outcome.inserted + outcome.revised)
        newest = h.newest
        return PageOutcome(counts, None if newest is None
                           else datetime.combine(newest, time.min, UTC).isoformat())
