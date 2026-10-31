"use client";

// One customer-month's margin (step 16): the figures behind the band, the variance
// by workgroup and activity type (which exceed the priced slab, and what that
// costs), and every version of the figure, since a restatement is a new version.

import Link from "next/link";

import type { MarginDetail, VarianceView, VersionView } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { PeriodControls, usePeriodChoice } from "@/components/PeriodControls";
import { Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { periodLabel, sortKey } from "@/lib/commercial";
import { count, dateTime, decimal, money, percent } from "@/lib/format";
import { marginBand } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

import { awaitingClose, BandBadge, notCaptured, points, StatusMarker, Trend } from "./Margin";

function load(tenantId: string, period: string | undefined, currency: string | undefined) {
  return (token: string) => apiGet(token, "/v1/commercial/margin/{tenant_id}", { tenant_id: tenantId }, { period, currency });
}

export function MarginCustomer({ tenantId }: { tenantId: string }) {
  const me = useMe();
  const choice = usePeriodChoice();
  const loaded = useApi(load(tenantId, choice.period, choice.currency), `margin:${tenantId}:${choice.query}`);
  const back = (
    <Link href={`/margin${choice.query ? `?${choice.query}` : ""}`} className="text-sm font-medium text-accent underline">
      All customers
    </Link>
  );
  if (loaded.status === "loading") return <>{back}<Loading what="the customer’s margin" /></>;
  if (loaded.status === "error") return <>{back}<LoadError error={loaded.error} what="customer" /></>;
  const data: MarginDetail = loaded.data;
  const m = data.margin;
  const ccy = m.contract_currency;
  const moneyIn = (unit: string) => (x: { value: string | number }) => money(String(x.value), unit);
  const ratio = (x: { value: string | number }) => percent(String(x.value));

  const varianceColumns: Column<VarianceView>[] = [
    { key: "workgroup", header: "Workgroup", value: (v) => v.workgroup, sortable: true },
    { key: "activity", header: "Activity type", value: (v) => v.activity, sortable: true },
    {
      key: "hours",
      header: "Hours",
      value: (v) => sortKey(v.hours.value),
      render: (v) => (
        <span className="tabular-nums">
          {decimal(v.hours.value, 1)}
          {v.items_without_hours.value > 0 ? (
            <span className="block text-xs text-muted">{count(v.items_without_hours.value)} items without hours</span>
          ) : null}
        </span>
      ),
      sortable: true,
    },
    { key: "items", header: "Work items", value: (v) => v.items.value, render: (v) => count(v.items.value), sortable: true, className: "tabular-nums" },
    { key: "share", header: "Share of hours", value: (v) => sortKey(v.share.value), render: (v) => percent(v.share.value), sortable: true, className: "tabular-nums" },
    {
      key: "excess",
      header: "Beyond the priced slab",
      value: (v) => sortKey(v.excess_hours.value),
      render: (v) =>
        Number(v.excess_hours.value) > 0 ? (
          <span className="flex flex-col items-start gap-1 tabular-nums">
            <HealthBadge band="at-risk" label="Exceeds slab" />
            <span>
              {decimal(v.excess_hours.value, 1)} h, {money(v.excess_cost.value, v.excess_cost.unit)}
            </span>
            <span className="text-xs text-muted">modelled</span>
          </span>
        ) : (
          <span className="text-muted">Within slab</span>
        ),
      sortable: true,
    },
  ];

  const versionColumns: Column<VersionView>[] = [
    { key: "version", header: "Version", value: (v) => v.version.value, render: (v) => `Version ${count(v.version.value)}`, sortable: true },
    { key: "status", header: "Status", value: (v) => v.status, render: (v) => (v.status === "provisional" ? "Provisional" : "Closed"), sortable: true },
    { key: "computed", header: "Computed", value: (v) => v.computed_at, render: (v) => dateTime(v.computed_at), sortable: true },
    {
      key: "restates",
      header: "Restates",
      value: (v) => v.restates_version?.value ?? null,
      render: (v) => (v.restates_version ? `Version ${count(v.restates_version.value)}` : <span className="text-muted">Original</span>),
      sortable: true,
    },
    {
      key: "pct",
      header: "Margin %",
      value: (v) => sortKey(v.margin_pct?.value),
      render: (v) => (v.margin_pct ? percent(v.margin_pct.value) : <span className="text-muted">Not captured</span>),
      sortable: true,
      className: "tabular-nums",
    },
  ];

  return (
    <>
      {back}
      <PageHeader title={m.tenant.name} subtitle={`Margin for ${periodLabel(m.period)}${ccy ? `, in the contract currency (${ccy})` : ""}.`}>
        {m.not_captured ? null : <StatusMarker m={m} />}
      </PageHeader>
      <div className="mb-4">
        <PeriodControls
          choice={choice}
          shownPeriod={m.period}
          currencyHelp="Margin is computed in the contract’s currency. A reporting currency converts it at the month’s average rate, with the FX impact as its own line."
        />
      </div>

      {m.not_captured ? (
        <EmptyState
          title={notCaptured(m) ?? "Not captured"}
          next={
            m.not_captured === "no_sow"
              ? "There is no SOW for this customer, so there is no revenue to set cost against. Margin is captured once the SOW is registered."
              : "Margin is captured once the missing input is in place; it is never shown as zero."
          }
        />
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <BandBadge m={m} />
            {m.exceeds_slab ? <HealthBadge band="at-risk" label="Effort exceeds the priced slab" /> : null}
            {m.unpriced_cis && m.unpriced_cis.value > 0 ? (
              <HealthBadge band="watch" label={`${count(m.unpriced_cis.value)} critical CIs with no catalogue price`} />
            ) : null}
            {m.rate_card_version ? <span className="text-xs text-muted">Rate card {m.rate_card_version}</span> : null}
          </div>
          <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <MetricCard label="Margin %" metric={m.margin_pct} display={ratio} band={m.margin_pct ? marginBand(m.band) : undefined} unavailable="Not captured" />
            <MetricCard
              label="Gap to target"
              metric={m.gap_to_target}
              display={(x) => points(String(x.value))}
              unavailable={awaitingClose(m) ? "At month close" : "No target in the SOW"}
              trend={m.target_margin ? { direction: "flat", text: `target ${percent(m.target_margin.value)}`, better: true } : undefined}
            />
            {ccy ? (
              <>
                <MetricCard label="Revenue" metric={m.revenue} display={moneyIn(ccy)} unavailable="Not captured" />
                <MetricCard label="Actual cost" metric={m.actual_cost} display={moneyIn(ccy)} unavailable="Not available" />
                <MetricCard label="Modelled cost" metric={m.modelled_cost} display={moneyIn(ccy)} unavailable="Not available" />
                <MetricCard label="Actual margin" metric={m.actual_margin} display={moneyIn(ccy)} unavailable="Not available" />
                <MetricCard label="Modelled margin" metric={m.modelled_margin} display={moneyIn(ccy)} unavailable="Not available" />
              </>
            ) : null}
            {m.effort ? (
              <MetricCard
                label="Effort"
                metric={m.effort.hours}
                display={(x) => `${decimal(String(x.value), 1)} h`}
                trend={{
                  direction: "flat",
                  text: m.effort.complete
                    ? "every work item has hours"
                    : `${count(m.effort.items_without_hours.value)} of ${count(m.effort.items.value)} work items without hours`,
                  better: m.effort.complete,
                }}
              />
            ) : null}
            {choice.currency ? (
              m.reported ? (
                <>
                  <MetricCard label={`Margin in ${m.reported.currency}`} metric={m.reported.margin} display={moneyIn(m.reported.currency)} />
                  <MetricCard label={`FX impact (${m.reported.currency})`} metric={m.reported.fx_impact} display={moneyIn(m.reported.currency)} />
                </>
              ) : (
                <MetricCard label={`Margin in ${choice.currency}`} metric={null} unavailable={`No ${choice.currency} rate for the month`} />
              )
            ) : null}
          </div>
          <section aria-labelledby="trend-heading" className="mb-6 space-y-2">
            <h2 id="trend-heading" className="text-lg font-semibold text-ink">
              Last six months
            </h2>
            <Trend points={m.trend} />
          </section>
        </>
      )}

      <div className="space-y-8">
        <section aria-labelledby="variance-heading" className="space-y-2">
          <h2 id="variance-heading" className="text-lg font-semibold text-ink">
            Variance by workgroup and activity type
          </h2>
          <p className="text-sm text-muted">
            Where the month’s hours went. Hours beyond what the slab priced are shared out by each line’s share of hours; their cost is modelled.
          </p>
          {data.variance[0] ? <Provenance metric={data.variance[0].hours} /> : null}
          <DataTable
            id="margin-variance"
            caption="Variance by workgroup and activity type"
            rows={data.variance}
            rowKey={(v) => `${v.workgroup}:${v.activity}`}
            columns={varianceColumns}
            facets={[{ key: "workgroup", label: "Workgroup", value: (v) => v.workgroup }]}
            role={me.role}
            initial={{ sort: { key: "excess", dir: "desc" } }}
            empty={{ title: "No effort recorded", next: "Variance appears once the month has work items with hours." }}
          />
        </section>

        <section aria-labelledby="versions-heading" className="space-y-2">
          <h2 id="versions-heading" className="text-lg font-semibold text-ink">
            Version history
          </h2>
          <p className="text-sm text-muted">A restatement (for example after an FX correction) is a new version; earlier versions are kept as they were.</p>
          {data.versions[0] ? <Provenance metric={data.versions[0].version} /> : null}
          <DataTable
            id="margin-versions"
            caption="Margin versions"
            rows={data.versions}
            rowKey={(v) => String(v.version.value)}
            columns={versionColumns}
            role={me.role}
            initial={{ sort: { key: "version", dir: "desc" } }}
            empty={{ title: "No versions", next: "Margin for this month has not been computed." }}
          />
        </section>
      </div>
    </>
  );
}
