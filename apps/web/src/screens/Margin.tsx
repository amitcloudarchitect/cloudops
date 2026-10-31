"use client";

// Margin (step 16; HLD M11-M12, ADR 0019): per customer, the month's margin in
// the contract currency, actual against modelled, banded by the API on the
// console's one scale, with the gap to the SOW's target. A customer whose margin
// cannot be computed is "Not captured" with the reason, never a zero or a dash.
// Provisional (month to date) and closed figures are marked, and so is a restated
// one. Given a reporting currency, the converted margin and its FX impact follow
// as their own lines.

import Link from "next/link";

import type { MarginView, TrendPoint } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { PeriodControls, usePeriodChoice } from "@/components/PeriodControls";
import { Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { notCapturedReason, periodLabel, sortKey, statusLabel } from "@/lib/commercial";
import { count, date, decimal, money, percent } from "@/lib/format";
import { BAND_LABEL, marginBand, type Band } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const BAND_ORDER: Record<Band, number> = { critical: 0, "at-risk": 1, watch: 2, healthy: 3, unknown: 4 };
const BAND_DOT: Record<Band, string> = {
  critical: "bg-band-critical",
  "at-risk": "bg-band-at-risk",
  watch: "bg-band-watch",
  healthy: "bg-band-healthy",
  unknown: "bg-band-unknown",
};

export function notCaptured(m: MarginView): string | null {
  return m.not_captured ? `Not captured — ${notCapturedReason(m.not_captured)}` : null;
}

function NotCaptured({ m }: { m: MarginView }) {
  return <span className="text-sm text-muted">{notCaptured(m)}</span>;
}

/** Points of margin, signed: "+2.5 pts", "−4.0 pts". */
export function points(ratio: string): string {
  const pct = percent(ratio);
  const text = `${pct.endsWith("%") ? pct.slice(0, -1) : pct} pts`;
  return text.startsWith("-") ? `−${text.slice(1)}` : `+${text}`;
}

export function BandBadge({ m }: { m: MarginView }) {
  if (m.not_captured) return <HealthBadge band="unknown" label={notCaptured(m) ?? "Not captured"} />;
  const band = marginBand(m.band);
  return (
    <span className="flex flex-col items-start gap-1">
      <HealthBadge band={band} label={BAND_LABEL[band]} />
      {m.band_basis === "modelled" ? <span className="text-xs text-muted">On modelled cost: effort incomplete</span> : null}
    </span>
  );
}

export function StatusMarker({ m }: { m: MarginView }) {
  if (m.status === null) return <NotCaptured m={m} />;
  return (
    <span className="flex flex-col items-start gap-1 text-sm">
      <span
        className={`rounded-control border px-1.5 text-xs font-semibold ${m.status === "provisional" ? "border-dashed border-modelled bg-modelled-bg text-modelled" : "border-line text-ink"}`}
      >
        {statusLabel(m)}
      </span>
      {m.status === "provisional" && m.through ? <span className="text-xs text-muted">to {date(m.through)}</span> : null}
      {m.version ? <span className="text-xs text-muted">version {count(m.version.value)}</span> : null}
    </span>
  );
}

function trendPoint(p: TrendPoint): string {
  const month = periodLabel(p.period).split(" ")[0]?.slice(0, 3) ?? p.period;
  const value = p.margin_pct ? percent(p.margin_pct.value) : "not captured";
  return `${month} ${value}${p.status === "provisional" ? " (provisional)" : ""}`;
}

export function Trend({ points: trend }: { points: TrendPoint[] }) {
  if (trend.length === 0) return <span className="text-muted">No history</span>;
  return (
    <ol className="flex flex-wrap gap-x-2 gap-y-0.5 text-xs tabular-nums" aria-label="Margin, last six months">
      {trend.map((p) => (
        <li key={p.period} className="flex items-center gap-1">
          <span aria-hidden="true" className={`inline-block size-2 rounded-full ${BAND_DOT[marginBand(p.band)]}`} />
          {trendPoint(p)}
        </li>
      ))}
    </ol>
  );
}

function load(period: string | undefined, currency: string | undefined) {
  return (token: string) => apiGet(token, "/v1/commercial/margin", {}, { period, currency });
}

export function Margin() {
  const me = useMe();
  const choice = usePeriodChoice();
  const loaded = useApi(load(choice.period, choice.currency), `margin:${choice.query}`);
  const header = (
    <PageHeader
      title="Margin"
      subtitle="Each customer’s margin for one month in its contract currency: actual against modelled, the band, and the gap to the SOW’s target."
    />
  );
  if (loaded.status === "loading") return <>{header}<Loading what="margin" /></>;
  if (loaded.status === "error") return <>{header}<LoadError error={loaded.error} what="margin" /></>;
  const data = loaded.data;
  const reporting = data.reporting_currency;
  const link = (id: string) => `/margin/${id}${choice.query ? `?${choice.query}` : `?period=${data.period}`}`;
  const notCapturedCount = data.customers.filter((m) => m.not_captured).length;
  const first = data.customers.find((m) => m.margin_pct)?.margin_pct;

  const columns: Column<MarginView>[] = [
    {
      key: "customer",
      header: "Customer",
      value: (m) => m.tenant.name,
      render: (m) => (
        <Link href={link(m.tenant.id)} className="font-medium text-accent underline">
          {m.tenant.name}
        </Link>
      ),
      sortable: true,
    },
    {
      key: "band",
      header: "Band",
      value: (m) => BAND_ORDER[m.not_captured ? "unknown" : marginBand(m.band)],
      render: (m) => <BandBadge m={m} />,
      sortable: true,
    },
    {
      key: "pct",
      header: "Margin %",
      value: (m) => sortKey(m.margin_pct?.value),
      render: (m) =>
        m.not_captured || !m.margin_pct ? (
          <NotCaptured m={m} />
        ) : (
          <span className="tabular-nums">
            {percent(m.margin_pct.value)}
            {m.band_basis === "modelled" ? <span className="block text-xs text-muted">modelled</span> : null}
          </span>
        ),
      sortable: true,
    },
    {
      key: "gap",
      header: "Gap to target",
      value: (m) => sortKey(m.gap_to_target?.value),
      render: (m) =>
        m.not_captured ? (
          <NotCaptured m={m} />
        ) : m.gap_to_target && m.target_margin ? (
          <span className="tabular-nums">
            {points(m.gap_to_target.value)}
            <span className="block text-xs text-muted">target {percent(m.target_margin.value)}</span>
          </span>
        ) : (
          <span className="text-sm text-muted">No target in the SOW</span>
        ),
      sortable: true,
    },
    {
      key: "margin",
      header: "Actual / modelled margin",
      value: (m) => sortKey(m.actual_margin?.value ?? m.modelled_margin?.value),
      render: (m) =>
        m.not_captured || !m.contract_currency ? (
          <NotCaptured m={m} />
        ) : (
          <span className="tabular-nums">
            <span className="block">{m.actual_margin ? money(m.actual_margin.value, m.contract_currency) : "Actual not available"}</span>
            <span className="block text-xs text-muted">
              modelled {m.modelled_margin ? money(m.modelled_margin.value, m.contract_currency) : "not available"}
            </span>
          </span>
        ),
      sortable: true,
    },
    ...(reporting
      ? [
          {
            key: "reported",
            header: `Margin in ${reporting}`,
            value: (m: MarginView) => sortKey(m.reported?.margin.value),
            render: (m: MarginView) =>
              m.not_captured ? (
                <NotCaptured m={m} />
              ) : m.reported ? (
                <span className="tabular-nums">
                  <span className="block">{money(m.reported.margin.value, m.reported.currency)}</span>
                  <span className="block text-xs text-muted">FX impact {money(m.reported.fx_impact.value, m.reported.currency)}</span>
                </span>
              ) : (
                <span className="text-sm text-muted">No {reporting} rate for the month</span>
              ),
            sortable: true,
          } satisfies Column<MarginView>,
        ]
      : []),
    {
      key: "effort",
      header: "Effort",
      value: (m) => (m.effort ? sortKey(m.effort.hours.value) : null),
      render: (m) =>
        m.effort ? (
          <span className="tabular-nums">
            <span className="block">{decimal(m.effort.hours.value, 1)} h</span>
            <span className="block text-xs text-muted">
              {m.effort.complete
                ? "complete"
                : `${count(m.effort.items_without_hours.value)} of ${count(m.effort.items.value)} items without hours`}
            </span>
          </span>
        ) : (
          <NotCaptured m={m} />
        ),
      sortable: true,
    },
    {
      key: "status",
      header: "Status",
      value: (m) => (m.status ? statusLabel(m) : "Not captured"),
      render: (m) => <StatusMarker m={m} />,
      sortable: true,
    },
    {
      key: "trend",
      header: "Trend",
      value: (m) => m.trend.map(trendPoint).join(", "),
      render: (m) => <Trend points={m.trend} />,
      sortable: false,
    },
  ];

  return (
    <>
      {header}
      <div className="mb-4">
        <PeriodControls
          choice={choice}
          shownPeriod={data.period}
          currencyHelp="Margin is computed in each contract’s currency. A reporting currency converts it at the month’s average rate, with the FX impact as its own line."
        />
      </div>
      <section aria-labelledby="margin-customers" className="space-y-2">
        <h2 id="margin-customers" className="text-lg font-semibold text-ink">
          {periodLabel(data.period)}
          {notCapturedCount > 0 ? <span className="ml-2 text-sm font-normal text-muted">{count(notCapturedCount)} not captured</span> : null}
        </h2>
        <p className="text-sm text-muted">
          Banded on actual cost; on modelled cost while effort is incomplete (work items without hours). Provisional figures are month to date and change
          until the month closes; a restated figure is a new version, never an overwrite.
        </p>
        {first ? <Provenance metric={first} /> : null}
        <DataTable
          id="margin-customers"
          caption="Margin by customer"
          rows={data.customers}
          rowKey={(m) => m.tenant.id}
          columns={columns}
          facets={[
            { key: "band", label: "Band", value: (m) => (m.not_captured ? "Not captured" : BAND_LABEL[marginBand(m.band)]) },
            { key: "status", label: "Status", value: (m) => (m.status ? statusLabel(m) : "Not captured") },
          ]}
          role={me.role}
          initial={{ sort: { key: "band", dir: "asc" } }}
          empty={{ title: "No customers in your scope", next: "A platform administrator assigns customers to your account." }}
        />
      </section>
    </>
  );
}
