"""Provider totals and invoices -> ``cloudops.billing_total`` (step 16).

The figures reconciliation compares our line items with (migration 0011):
``provider_total``, the provider's own month-to-date total for an account,
read daily; and ``invoice``, the issued invoice at close. Shared by the AWS
(Cost Explorer, Invoicing) and Azure (Cost Management query, Billing invoices)
sources, which parse their responses into ``Total``s; this module stores them.

- The account is the provider's id, resolved by an exact alias of the
  provider (``identity.resolve_ids``). An unknown one goes to the alias
  quarantine with a count; nothing it carries is written.
- Upsert on (account, kind, invoice period, as of): a re-read of the same day,
  or a replay, rewrites the same row. Two figures for one key in a page (a
  credit memo issued beside its invoice) are summed when their currency
  agrees; a key whose figures disagree on currency is rejected, never guessed.
- Amounts are ``Decimal`` from end to end, rounded once to numeric(18,6).
- One transaction, scoped to exactly the tenants the page resolved to, holds
  the rows, the quarantine counts and, on the scope's last page, the
  watermark (the newest ``as_of`` the run stored).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_connectors.core import identity, state
from cloudops_connectors.core.ingest import PageContext, PageOutcome, principal
from cloudops_tenancy import Database, Role, ScopedConnection, TenantScope

PROVIDER_TOTAL: Final = "provider_total"
INVOICE: Final = "invoice"
KINDS: Final = frozenset({PROVIDER_TOTAL, INVOICE})
MONEY: Final = Decimal("0.000001")  # numeric(18,6)

_UPSERT: Final = text(
    "INSERT INTO cloudops.billing_total (tenant_id, account_id, source_system, kind,"
    " invoice_period, as_of, amount, currency, document_ref, raw_extra)"
    " VALUES (:tenant_id, :account_id, :source, :kind, :period, :as_of, :amount, :currency,"
    "         :document_ref, CAST(:raw_extra AS jsonb))"
    " ON CONFLICT (account_id, kind, invoice_period, as_of) DO UPDATE SET"
    "   amount = EXCLUDED.amount, currency = EXCLUDED.currency,"
    "   document_ref = EXCLUDED.document_ref, raw_extra = EXCLUDED.raw_extra,"
    "   source_system = EXCLUDED.source_system, ingested_at = now()"
)


@dataclass(frozen=True)
class Total:
    account: str  # the provider's account id: an alias of the provider
    kind: str
    invoice_period: str  # YYYY-MM
    as_of: date
    amount: Decimal
    currency: str
    document_ref: str | None = None
    raw_extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown billing total kind {self.kind!r}")
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite():
            raise TypeError("a billing amount is a finite Decimal")


def merge(totals: Iterable[Total]) -> tuple[list[Total], int]:
    """One figure per (account, kind, period, as_of); the count of figures
    rejected because their key's figures disagree on currency."""
    keyed: dict[tuple[str, str, str, date], list[Total]] = {}
    for t in totals:
        keyed.setdefault((t.account, t.kind, t.invoice_period, t.as_of), []).append(t)
    out: list[Total] = []
    rejected = 0
    for group in keyed.values():
        if len({t.currency for t in group}) > 1:
            rejected += len(group)
            continue
        if len(group) == 1:
            out.append(group[0])
            continue
        refs = sorted({t.document_ref for t in group if t.document_ref})
        out.append(replace(
            group[0], amount=sum((t.amount for t in group), Decimal(0)),
            document_ref=",".join(refs) or None,
            raw_extra={"parts": [t.raw_extra for t in group]}))
    return out, rejected


def watermark(as_of: date) -> datetime:
    return datetime.combine(as_of, time.min, UTC)


async def accounts(
    conn: ScopedConnection, provider: str, tenant_of: Mapping[str, UUID]
) -> dict[str, UUID]:
    """``cloud_account`` ids for resolved accounts, created on first sight. An
    account held by a different tenant than its alias names is an
    inconsistency for a person: the page fails, nothing is written under either."""
    known = await identity.known_accounts(conn, provider, tenant_of)
    for native, (_, tenant) in known.items():
        if tenant_of[native] != tenant:
            raise ValueError("an account is held by a different tenant than its alias")
    out = {native: account for native, (account, _) in known.items()}
    missing = {a: t for a, t in tenant_of.items() if a not in known}
    if missing:
        out.update(await identity.upsert_accounts(
            conn, provider, missing, [identity.AccountSeen(a, None) for a in missing]))
    return out


async def store(
    db: Database, ref: str, ctx: PageContext, *, source: str, entity: str,
    totals: Iterable[Total], records: int, rejected: int = 0,
    binding: identity.Binding | None = None,
) -> PageOutcome:
    """Write one page's totals (see the module docstring). ``binding``: a
    customer credential's run, whose accounts resolve to its tenant only."""
    merged, conflicting = merge(totals)
    counts = state.RunCounts(pages=1, records=records, rejected=rejected + conflicting)
    who = principal(source)
    async with db.transaction(TenantScope((), Role.INGEST, who)) as conn:
        resolved = await identity.resolve_ids(conn, source, {t.account for t in merged},
                                              binding)
    tenant_of = {a: t for a, t in resolved.items() if t is not None}
    unmatched = Counter(t.account for t in merged if t.account not in tenant_of)
    mine = [t for t in merged if t.account in tenant_of]
    high = max((t.as_of for t in merged), default=None)
    mark = None
    if ctx.advance and ctx.final:
        marks = [m for m in (ctx.run_high_water, None if high is None else watermark(high))
                 if m is not None]
        mark = max(marks) if marks else None
    async with db.transaction(TenantScope(set(tenant_of.values()), Role.INGEST, who)) as conn:
        if mine:
            account_of = await accounts(conn, source, {t.account: tenant_of[t.account]
                                                       for t in mine})
            await conn.execute(_UPSERT, [_row(t, tenant_of[t.account], account_of[t.account],
                                              source) for t in mine])
        await state.quarantine_aliases(conn, source, unmatched, ref)
        if mark is not None:
            await state.advance_watermark(conn, source, entity, ctx.scope_key, mark)
    counts.written = len(mine)
    counts.quarantined = sum(unmatched.values())
    return PageOutcome(counts, None if high is None else watermark(high).isoformat())


def _row(t: Total, tenant: UUID, account: UUID, source: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant, "account_id": account, "source": source, "kind": t.kind,
        "period": t.invoice_period, "as_of": t.as_of,
        "amount": t.amount.quantize(MONEY, rounding=ROUND_HALF_UP), "currency": t.currency,
        "document_ref": t.document_ref,
        "raw_extra": json.dumps(t.raw_extra, sort_keys=True, default=str) if t.raw_extra
        else None,
    }
