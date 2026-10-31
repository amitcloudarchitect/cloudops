"""The pack's two commercial sections, from the commercial engine (step 16).

- **Financial dashboard**: the month's margin snapshot as the commercial engine
  computed it (``cloudops.margin_snapshot``, latest version): revenue, cost and
  margin on the band basis, margin percentage, health band against the SOW
  target, FX impact, and the effort variance by workgroup. Nothing is
  recomputed here, so the pack says what the Margin screen says.
- **Revenue mix**: the SOW in force for the month, split into its hosting,
  manage and professional services values (``cloudops.sow``).

Both carry margin, cost base or SOW revenue, which a customer never sees (HLD
principle 6). The API stores them apart from the pack, in
``report_pack_commercial`` under the ``commercial_visibility`` policy, and a
reader without a commercial role gets the pack without them. Both tables they
read sit under that policy too, so a scope without it finds no rows: the
sections then say "not captured", never zero.

Amounts are in the contract currency the snapshot and SOW record, unrounded;
a snapshot in another currency than the pack's is a gap, not a conversion.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from cloudops_domain.reporting.sections import (
    HUNDRED,
    SECTION_TITLES,
    Figure,
    Section,
    Table,
    month_bounds,
    ref,
)
from cloudops_tenancy import ScopedConnection

SOURCE: Final = "commercial_engine"
NOT_CAPTURED: Final = {
    "no_sow": "no SOW in force for the month",
    "no_people_cost": "the rate card has no people cost for the delivery location",
    "no_fx_rate": "no average FX rate from the rate card currency for the month",
    "no_cost_data": "no billing data for the month",
}

_SNAPSHOT: Final = text(
    "SELECT id, version, status, through, not_captured, contract_currency, revenue,"
    " modelled_cost, actual_cost, modelled_margin, actual_margin, margin_at_prior_rates,"
    " margin_pct, band, band_basis, target_margin, gap_to_target, effort_hours,"
    " effort_items_without_hours, effort_complete"
    " FROM cloudops.margin_snapshot WHERE tenant_id = :t AND period = :p"
    " ORDER BY version DESC LIMIT 1"
)
_VARIANCE: Final = text(
    "SELECT workgroup, activity, hours, share, excess_cost, currency"
    " FROM cloudops.margin_variance WHERE tenant_id = :t AND snapshot_id = :s"
    " ORDER BY excess_cost DESC, hours DESC, workgroup, activity"
)
# The SOW in force for the month, as the pack's contract currency is chosen.
_SOW: Final = text(
    "SELECT id, currency, monthly_manage_revenue, hosting_acv, manage_acv,"
    " professional_services FROM cloudops.sow WHERE tenant_id = :t AND effective_from < :e"
    " AND (effective_to IS NULL OR effective_to >= :s) ORDER BY effective_from DESC LIMIT 1"
)


def _gap(key: str, why: str) -> Section:
    return Section(key, SECTION_TITLES[key], False, why)


def _pct(fraction: Any) -> str:
    return str(Decimal(fraction) * HUNDRED)


class CommercialSections:
    """``FinancialSections`` from the commercial engine's stored figures."""

    async def financial_dashboard(self, conn: ScopedConnection, tenant_id: UUID, period: date,
                                  contract_currency: str) -> Section:
        key = "financial_dashboard"
        row = (await conn.execute(_SNAPSHOT, {"t": tenant_id, "p": period})).one_or_none()
        if row is None:
            return _gap(key, "the commercial engine has not computed margin for the month")
        (sid, version, status, through, not_captured, ccy, revenue, _m_cost, a_cost, m_margin,
         a_margin, at_prior, pct, band, basis, target, gap, hours, without_hours,
         complete) = tuple(row)
        if not_captured is not None:
            return _gap(key, NOT_CAPTURED.get(str(not_captured), str(not_captured)))
        if str(ccy).strip() != contract_currency:
            return _gap(key, f"margin is recorded in {str(ccy).strip()}, the pack in"
                             f" {contract_currency}")
        q = ref("financial_dashboard", period=period, snapshot=sid)
        modelled = basis == "modelled"
        cost = _m_cost if modelled else a_cost
        margin = m_margin if modelled else a_margin
        basis_note = "modelled: observed effort is incomplete" if modelled else None
        state = f"{status}, version {version}, figures through {through:%Y-%m-%d}"

        def money(k: str, label: str, value: Any, note: str | None = None) -> Figure:
            return Figure(k, label, str(Decimal(value)), "money", contract_currency, SOURCE, q,
                          note)

        figures = [
            money("revenue", "Revenue", revenue, state),
            money("cost", "Cost", cost, basis_note),
            money("margin", "Margin", margin, basis_note),
        ]
        if pct is not None:
            figures.append(Figure("margin_pct", "Margin", _pct(pct), "percent", "percent", SOURCE,
                                  q, f"health band: {band}" if band else None))
        if target is not None:
            figures.append(Figure("target_margin", "Target margin (SOW)", _pct(target), "percent",
                                  "percent", "sow_register", q))
        if gap is not None:
            figures.append(Figure("gap_to_target", "Gap to target (points)", _pct(gap), "percent",
                                  "percent", SOURCE, q))
        if margin is not None and at_prior is not None:
            figures.append(money("fx_impact", "FX impact on margin", Decimal(margin)
                                 - Decimal(at_prior), "against last month's average rate"))
        figures.append(Figure(
            "effort_hours", "Observed effort", str(Decimal(hours)), "hours", "hours",
            "servicenow", q, None if complete else
            f"partial: {without_hours} work items without hours"))
        variance = [tuple(r) for r in await conn.execute(_VARIANCE, {"t": tenant_id, "s": sid})]
        tables: tuple[Table, ...] = ()
        if variance:
            tables = (Table(
                "effort_variance", "Effort by workgroup",
                ("Workgroup", "Activity", "Hours", "Share", f"Excess cost ({contract_currency})"),
                ("text", "text", "hours", "percent", "money"),
                tuple((str(w), str(a), str(Decimal(h)), _pct(s), str(Decimal(c)))
                      for w, a, h, s, c, cur in variance if str(cur).strip() == contract_currency),
            ),)
        return Section(key, SECTION_TITLES[key], True, None, tuple(figures), tables)

    async def revenue_mix(self, conn: ScopedConnection, tenant_id: UUID, period: date,
                          contract_currency: str) -> Section:
        key = "revenue_mix"
        start, end = month_bounds(period)
        row = (await conn.execute(_SOW, {"t": tenant_id, "s": start.date(), "e": end.date()})
               ).one_or_none()
        if row is None:
            return _gap(key, NOT_CAPTURED["no_sow"])
        sid, ccy, monthly, hosting, manage, services = tuple(row)
        if str(ccy).strip() != contract_currency:
            return _gap(key, f"the SOW is in {str(ccy).strip()}, the pack in {contract_currency}")
        q = ref("revenue_mix", period=period, sow=sid)
        parts = (("Hosting (annual contract value)", hosting),
                 ("Manage (annual contract value)", manage),
                 ("Professional services (SOW value)", services))
        recorded = [Decimal(v) for _, v in parts if v is not None]
        whole = sum(recorded, Decimal(0))
        rows = tuple(
            (label, "" if v is None else str(Decimal(v)),
             "" if v is None or not whole else str(Decimal(v) * HUNDRED / whole))
            for label, v in parts)
        missing = [label for label, v in parts if v is None]
        figures = (Figure("monthly_manage_revenue", "Monthly manage revenue",
                          str(Decimal(monthly)), "money", contract_currency, "sow_register", q,
                          "as the SOW in force states it"),)
        note = Table("revenue_mix", "Contract value by component",
                     ("Component", f"Value ({contract_currency})", "Share"),
                     ("text", "money", "percent"), rows)
        tables: tuple[Table, ...] = (note,)
        if missing:
            tables += (Table("not_recorded", "Not recorded on the SOW", ("Component",),
                             ("text",), tuple((m,) for m in missing)),)
        return Section(key, SECTION_TITLES[key], True, None, figures, tables)
