"use client";

// FinOps (step 16; HLD M10, ADR 0019): each customer's cloud cost for one invoice
// month, split into consumption (the only class that feeds margin) and other spend,
// with the reconciliation verdict beside it. An unverified figure is marked as such,
// with the reason, so it never reads as a normal verified number. Unallocated spend
// and recent anomalies follow. The verdicts are the API's; this screen names them.

import Link from "next/link";

import type { ChargeLine, CustomerCost, FinOpsOverview as Overview, Unallocated } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { PeriodControls, usePeriodChoice } from "@/components/PeriodControls";
import { Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { periodLabel, sortKey, sumDecimal, verificationReason } from "@/lib/commercial";
import { count, money, percent } from "@/lib/format";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

import { FxCorrections } from "./FxCorrections";

/** Totals per transaction currency, and in the reporting currency when every line converts. */
export interface Spend {
  byCurrency: { currency: string; amount: string }[];
  converted: string | null;
  lines: number;
}

export function spend(lines: readonly ChargeLine[], reporting: string | null): Spend {
  const currencies = [...new Set(lines.map((l) => l.currency))].sort();
  const byCurrency = currencies.map((currency) => ({
    currency,
    amount: sumDecimal(lines.filter((l) => l.currency === currency).map((l) => l.amortized.value)),
  }));
  const converted =
    reporting && lines.length > 0 && lines.every((l) => l.converted !== null)
      ? sumDecimal(lines.map((l) => l.converted?.value ?? "0"))
      : null;
  return { byCurrency, converted, lines: lines.length };
}

export function SpendCell({ value, reporting, unverified }: { value: Spend; reporting: string | null; unverified: boolean }) {
  if (value.lines === 0) return <span className="text-muted">No charges</span>;
  return (
    <span className={`tabular-nums ${unverified ? "italic" : ""}`}>
      {value.byCurrency.map((s) => (
        <span key={s.currency} className="block">
          {money(s.amount, s.currency)}
        </span>
      ))}
      {reporting ? (
        value.converted !== null ? (
          <span className="block text-xs text-muted">≈ {money(value.converted, reporting)}</span>
        ) : (
          <span className="block text-xs text-muted">No {reporting} rate for every day</span>
        )
      ) : null}
      {unverified ? <span className="mt-0.5 inline-block rounded-control border border-dashed border-band-at-risk px-1 text-xs font-semibold not-italic text-ink">Unverified</span> : null}
    </span>
  );
}

export function VerificationBadge({ cost }: { cost: CustomerCost }) {
  const reason = verificationReason(cost);
  return (
    <span className="flex flex-col items-start gap-1">
      <HealthBadge band={cost.verification === "verified" ? "healthy" : "at-risk"} label={cost.verification === "verified" ? "Verified" : "Unverified"} />
      {reason ? <span className="text-xs text-muted">{reason}</span> : null}
    </span>
  );
}

export function unallocatedText(lines: readonly Unallocated[], dimension: Unallocated["dimension"]): string {
  const mine = lines.filter((u) => u.dimension === dimension);
  if (mine.length === 0) return "No cost";
  return mine.map((u) => (u.share ? `${percent(u.share.value)}${mine.length > 1 ? ` of ${u.currency}` : ""}` : `No ${u.currency} cost`)).join("; ");
}

function load(period: string | undefined, currency: string | undefined) {
  return (token: string) => apiGet(token, "/v1/finops/overview", {}, { period, currency });
}

export function FinOps() {
  const me = useMe();
  const choice = usePeriodChoice();
  const loaded = useApi(load(choice.period, choice.currency), `finops:${choice.query}`);
  const header = (
    <PageHeader
      title="FinOps"
      subtitle="Cloud cost per customer for one invoice month: consumption (what feeds margin) against other spend, whether it reconciles with the provider’s total, and what is not allocated."
    />
  );
  if (loaded.status === "loading") return <>{header}<Loading what="cost" /></>;
  if (loaded.status === "error") return <>{header}<LoadError error={loaded.error} what="cost" /></>;
  const data: Overview = loaded.data;
  const reporting = data.reporting_currency;
  const unverified = data.customers.filter((c) => c.verification === "unverified").length;
  const link = (id: string) => `/finops/${id}${choice.query ? `?${choice.query}` : ""}`;
  const first = data.customers.flatMap((c) => c.charges)[0];

  const columns: Column<CustomerCost>[] = [
    {
      key: "customer",
      header: "Customer",
      value: (c) => c.tenant.name,
      render: (c) => (
        <Link href={link(c.tenant.id)} className="font-medium text-accent underline">
          {c.tenant.name}
        </Link>
      ),
      sortable: true,
    },
    {
      key: "verification",
      header: "Reconciliation",
      value: (c) => (c.verification === "verified" ? "Verified" : `Unverified: ${verificationReason(c) ?? ""}`),
      render: (c) => <VerificationBadge cost={c} />,
      sortable: true,
    },
    {
      key: "consumption",
      header: "Consumption",
      value: (c) => {
        const s = spend(c.charges.filter((l) => l.feeds_margin), reporting);
        return reporting ? sortKey(s.converted) : sortKey(s.byCurrency[0]?.amount);
      },
      render: (c) => <SpendCell value={spend(c.charges.filter((l) => l.feeds_margin), reporting)} reporting={reporting} unverified={c.verification === "unverified"} />,
      sortable: true,
    },
    {
      key: "other",
      header: "Other spend",
      value: (c) => {
        const s = spend(c.charges.filter((l) => !l.feeds_margin), reporting);
        return reporting ? sortKey(s.converted) : sortKey(s.byCurrency[0]?.amount);
      },
      render: (c) => <SpendCell value={spend(c.charges.filter((l) => !l.feeds_margin), reporting)} reporting={reporting} unverified={c.verification === "unverified"} />,
      sortable: true,
    },
    {
      key: "application",
      header: "Unallocated, application",
      value: (c) => unallocatedText(c.unallocated, "application"),
      render: (c) => <span className="tabular-nums">{unallocatedText(c.unallocated, "application")}</span>,
      sortable: true,
    },
    {
      key: "cost_centre",
      header: "Unallocated, cost centre",
      value: (c) => unallocatedText(c.unallocated, "cost_centre"),
      render: (c) => <span className="tabular-nums">{unallocatedText(c.unallocated, "cost_centre")}</span>,
      sortable: true,
    },
    {
      key: "anomalies",
      header: "Anomalies, 30 days",
      value: (c) => c.anomalies.value,
      render: (c) => (c.anomalies.value > 0 ? <HealthBadge band="watch" label={`${count(c.anomalies.value)} anomal${c.anomalies.value === 1 ? "y" : "ies"}`} /> : <span>None</span>),
      sortable: true,
    },
  ];

  return (
    <>
      {header}
      <div className="mb-4">
        <PeriodControls
          choice={choice}
          shownPeriod={data.period}
          currencyHelp="Amounts stay in the currency they were billed in. A reporting currency adds each at the daily average rate in force on its usage date."
        />
      </div>
      <section aria-labelledby="finops-customers" className="space-y-2">
        <h2 id="finops-customers" className="text-lg font-semibold text-ink">
          {periodLabel(data.period)}
          {unverified > 0 ? <span className="ml-2 text-sm font-normal text-muted">{count(unverified)} unverified</span> : null}
        </h2>
        <p className="text-sm text-muted">
          Amortized cost. Only consumption feeds margin. A figure marked Unverified does not match the provider’s own total (or could not be checked) and
          should not be quoted as final.
        </p>
        {first ? <Provenance metric={first.amortized} /> : null}
        <DataTable
          id="finops-customers"
          caption="Cost by customer"
          rows={data.customers}
          rowKey={(c) => c.tenant.id}
          columns={columns}
          facets={[{ key: "verification", label: "Reconciliation", value: (c) => (c.verification === "verified" ? "Verified" : "Unverified") }]}
          role={me.role}
          initial={{ sort: { key: "verification", dir: "asc" } }}
          empty={{ title: "No cost for this month", next: "Choose another month, or check the billing connectors’ health." }}
        />
      </section>
      {me.kind === "staff" ? (
        <div className="mt-8">
          <FxCorrections manage={can(me, "fx.manage")} />
        </div>
      ) : null}
    </>
  );
}
