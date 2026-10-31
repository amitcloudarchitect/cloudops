"use client";

// One customer's cost for one invoice month (step 16): charges by class, the
// reconciliation check per account, allocation by application and cost centre
// (unallocated is its own row, never folded away), and recent anomalies.

import Link from "next/link";

import type { AccountCheck, AllocationLine, AnomalyView, ChargeLine, CustomerFinOps } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { PeriodControls, usePeriodChoice } from "@/components/PeriodControls";
import { Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { chargeClassLabel, checkReason, periodLabel, sortKey, verificationReason } from "@/lib/commercial";
import { date, money, percent } from "@/lib/format";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

function load(tenantId: string, period: string | undefined, currency: string | undefined) {
  return (token: string) => apiGet(token, "/v1/finops/customers/{tenant_id}", { tenant_id: tenantId }, { period, currency });
}

function Allocation({ lines, dimension, label, role }: { lines: AllocationLine[]; dimension: AllocationLine["dimension"]; label: string; role: string }) {
  const rows = lines.filter((l) => l.dimension === dimension);
  const columns: Column<AllocationLine>[] = [
    {
      key: "value",
      header: label,
      value: (l) => l.value ?? "Unallocated",
      render: (l) => (l.value === null ? <strong className="font-semibold">Unallocated</strong> : l.value),
      sortable: true,
    },
    { key: "currency", header: "Currency", value: (l) => l.currency, sortable: true },
    {
      key: "amount",
      header: "Amortized cost",
      value: (l) => sortKey(l.amortized.value),
      render: (l) => money(l.amortized.value, l.currency),
      sortable: true,
      className: "tabular-nums",
    },
  ];
  return (
    <section aria-labelledby={`alloc-${dimension}`} className="space-y-2">
      <h3 id={`alloc-${dimension}`} className="font-semibold text-ink">
        By {label.toLowerCase()}
      </h3>
      {rows[0] ? <Provenance metric={rows[0].amortized} /> : null}
      <DataTable
        id={`finops-allocation-${dimension}`}
        caption={`Allocation by ${label.toLowerCase()}`}
        rows={rows}
        rowKey={(l) => `${l.currency}:${l.value ?? ""}`}
        columns={columns}
        role={role}
        empty={{ title: "No cost to allocate", next: "Nothing was billed to this customer this month." }}
      />
    </section>
  );
}

export function FinOpsCustomer({ tenantId }: { tenantId: string }) {
  const me = useMe();
  const choice = usePeriodChoice();
  const loaded = useApi(load(tenantId, choice.period, choice.currency), `finops:${tenantId}:${choice.query}`);
  const back = (
    <Link href={`/finops${choice.query ? `?${choice.query}` : ""}`} className="text-sm link font-medium">
      All customers
    </Link>
  );
  if (loaded.status === "loading") return <>{back}<Loading what="the customer’s cost" /></>;
  if (loaded.status === "error") return <>{back}<LoadError error={loaded.error} what="customer" /></>;
  const data: CustomerFinOps = loaded.data;
  const cost = data.cost;
  const reporting = choice.currency ?? null;
  const reason = verificationReason(cost);

  const chargeColumns: Column<ChargeLine>[] = [
    { key: "class", header: "Charge class", value: (l) => chargeClassLabel(l.charge_class), sortable: true },
    {
      key: "margin",
      header: "Feeds margin",
      value: (l) => (l.feeds_margin ? "Yes" : "No"),
      sortable: true,
    },
    {
      key: "amortized",
      header: "Amortized",
      value: (l) => sortKey(l.amortized.value),
      render: (l) => money(l.amortized.value, l.currency),
      sortable: true,
      className: "tabular-nums",
    },
    {
      key: "unblended",
      header: "Unblended",
      value: (l) => sortKey(l.unblended.value),
      render: (l) => money(l.unblended.value, l.currency),
      sortable: true,
      className: "tabular-nums",
    },
    ...(reporting
      ? [
          {
            key: "converted",
            header: `In ${reporting}`,
            value: (l: ChargeLine) => sortKey(l.converted?.value),
            render: (l: ChargeLine) =>
              l.converted ? money(l.converted.value, reporting) : <span className="text-muted">No {reporting} rate for every day</span>,
            sortable: true,
            className: "tabular-nums",
          } satisfies Column<ChargeLine>,
        ]
      : []),
  ];

  const checkColumns: Column<AccountCheck>[] = [
    {
      key: "account",
      header: "Account",
      value: (c) => c.account_name,
      render: (c) => (c.shared_with_platform ? `${c.account_name} (shared with the platform)` : c.account_name),
      sortable: true,
    },
    { key: "kind", header: "Checked against", value: (c) => (c.kind === "invoice" ? "Invoice" : "Provider total"), sortable: true },
    {
      key: "status",
      header: "Status",
      value: (c) => c.status,
      render: (c) => (
        <span className="flex flex-col items-start gap-1">
          <HealthBadge band={c.status === "verified" ? "healthy" : "at-risk"} label={c.status === "verified" ? "Verified" : "Unverified"} />
          <span className="text-xs text-muted">{checkReason(c)}</span>
        </span>
      ),
      sortable: true,
    },
    {
      key: "ledger",
      header: "Our ledger",
      value: (c) => sortKey(c.ledger.value),
      render: (c) => money(c.ledger.value, c.ledger.unit),
      sortable: true,
      className: "tabular-nums",
    },
    {
      key: "reference",
      header: "Provider’s figure",
      value: (c) => sortKey(c.reference?.value),
      render: (c) => (c.reference ? money(c.reference.value, c.reference.unit) : <span className="text-muted">None received</span>),
      sortable: true,
      className: "tabular-nums",
    },
    {
      key: "drift",
      header: "Drift",
      value: (c) => sortKey(c.drift?.value),
      render: (c) => (c.drift ? percent(c.drift.value, 2) : <span className="text-muted">Not comparable</span>),
      sortable: true,
      className: "tabular-nums",
    },
    { key: "on", header: "Checked on", value: (c) => c.checked_on, render: (c) => date(c.checked_on), sortable: true },
  ];

  const anomalyColumns: Column<AnomalyView>[] = [
    { key: "on", header: "Observed", value: (a) => a.observed_on, render: (a) => date(a.observed_on), sortable: true },
    { key: "account", header: "Account", value: (a) => a.account_name, sortable: true },
    { key: "window", header: "Window", value: (a) => (a.granularity === "day" ? "Day" : "Month"), sortable: true },
    {
      key: "direction",
      header: "Change",
      value: (a) => a.direction,
      render: (a) => <HealthBadge band="watch" label={a.direction === "spike" ? "Spike" : "Drop"} />,
      sortable: true,
    },
    {
      key: "observed",
      header: "Observed cost",
      value: (a) => sortKey(a.observed.value),
      render: (a) => money(a.observed.value, a.currency),
      sortable: true,
      className: "tabular-nums",
    },
    {
      key: "baseline",
      header: "Expected (baseline)",
      value: (a) => sortKey(a.baseline.value),
      render: (a) => (
        <span className="tabular-nums">
          {money(a.baseline.value, a.currency)} <span className="text-xs text-muted">modelled</span>
        </span>
      ),
      sortable: true,
    },
    {
      key: "ratio",
      header: "Against baseline",
      value: (a) => sortKey(a.ratio?.value),
      render: (a) => (a.ratio ? percent(a.ratio.value) : <span className="text-muted">No baseline</span>),
      sortable: true,
      className: "tabular-nums",
    },
  ];

  return (
    <>
      {back}
      <PageHeader title={cost.tenant.name} subtitle={`Cloud cost for ${periodLabel(cost.period)}.`} />
      <div className="mb-4">
        <PeriodControls
          choice={choice}
          shownPeriod={cost.period}
          currencyHelp="Amounts stay in the currency they were billed in. A reporting currency adds each at the daily average rate in force on its usage date."
        />
      </div>

      <div
        className={`mb-6 rounded-card border border-l-4 border-line bg-surface p-4 shadow-card ${cost.verification === "verified" ? "border-l-band-healthy" : "border-l-band-at-risk border-dashed"}`}
        role="status"
      >
        <p className="flex flex-wrap items-center gap-2 font-semibold text-ink">
          <HealthBadge band={cost.verification === "verified" ? "healthy" : "at-risk"} label={cost.verification === "verified" ? "Verified" : "Unverified"} />
          {cost.verification === "verified"
            ? "Every account matches the provider’s own total."
            : "These figures are not verified against the provider’s total."}
        </p>
        {reason ? <p className="mt-1 text-sm text-muted">Why: {reason}.</p> : null}
      </div>

      <div className="space-y-8">
        <section aria-labelledby="charges-heading" className="space-y-2">
          <h2 id="charges-heading" className="section-title">
            Charges by class
          </h2>
          <p className="text-sm text-muted">Only consumption feeds margin; licences, marketplace, support, reservations, tax and credits do not.</p>
          {cost.charges[0] ? <Provenance metric={cost.charges[0].amortized} /> : null}
          <DataTable
            id="finops-charges"
            caption="Charges by class"
            rows={cost.charges}
            rowKey={(l) => `${l.charge_class}:${l.currency}`}
            columns={chargeColumns}
            role={me.role}
            empty={{ title: "No charges this month", next: "Choose another month, or check the billing connectors’ health." }}
          />
        </section>

        <section aria-labelledby="checks-heading" className="space-y-2">
          <h2 id="checks-heading" className="section-title">
            Reconciliation per account
          </h2>
          <p className="text-sm text-muted">
            Our ledger against the invoice once there is one, otherwise the provider’s latest total. Drift above 0.5% leaves the figure unverified.
          </p>
          {cost.checks[0] ? <Provenance metric={cost.checks[0].ledger} /> : null}
          {cost.checks.some((c) => c.shared_with_platform) ? (
            <p className="text-sm text-ink" role="note">
              An account marked “shared with the platform” is also Getronics’ own subscription. Its cost appears under this customer and under the internal tenant, and each is reconciled on its own figures: never add the two together (ADR 0027).
            </p>
          ) : null}
          <DataTable
            id="finops-checks"
            caption="Reconciliation checks"
            rows={cost.checks}
            rowKey={(c) => c.account_id}
            columns={checkColumns}
            role={me.role}
            initial={{ sort: { key: "status", dir: "asc" } }}
            empty={{ title: "No checks for this month", next: "Reconciliation runs daily once the provider publishes a total. Until then the cost is unverified." }}
          />
        </section>

        <section aria-labelledby="allocation-heading" className="space-y-4">
          <h2 id="allocation-heading" className="section-title">
            Allocation
          </h2>
          <p className="text-sm text-muted">From each resource’s tags. Cost with no tag, or no matching resource, is unallocated and shown as its own row.</p>
          <div className="grid gap-6 xl:grid-cols-2">
            <Allocation lines={data.allocation} dimension="application" label="Application" role={me.role} />
            <Allocation lines={data.allocation} dimension="cost_centre" label="Cost centre" role={me.role} />
          </div>
        </section>

        <section aria-labelledby="anomalies-heading" className="space-y-2">
          <h2 id="anomalies-heading" className="section-title">
            Anomalies, last 30 days
          </h2>
          {data.anomalies[0] ? <Provenance metric={data.anomalies[0].observed} /> : null}
          <DataTable
            id="finops-anomalies"
            caption="Cost anomalies"
            rows={data.anomalies}
            rowKey={(a) => `${a.account_id}:${a.granularity}:${a.observed_on}`}
            columns={anomalyColumns}
            role={me.role}
            empty={{ title: "No anomalies", next: "Daily and monthly cost stayed within the expected range for every account." }}
          />
        </section>
      </div>
    </>
  );
}
