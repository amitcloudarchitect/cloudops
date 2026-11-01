"use client";

// Revenue & margin (ADR 0038): the monthly report workbook's sheet, per customer
// over a range of months, against another range if asked. Consumption, manage
// revenue, cost and margin are monthly averages over the range; professional
// services and the ACVs are the SOW's contract values, and an ACV the SOW does not
// state is derived and marked Modelled. Each figure is in the contract currency,
// or in the reporting currency once one is chosen, converted by the API at the
// rate in force on its transaction date. A figure that cannot be computed says
// why, never zero. Rounding happens here, once. The CSV comes from the API, with
// the totals.

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useId, useState } from "react";

import type { MetricDecimal, RevenueFigures, RevenueOverview, RevenueRow, RevenueTotal } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { ModelledMark } from "@/components/Provenance";
import { apiDownload, apiGet } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { useAuth } from "@/lib/auth";
import { currencyParam, periodLabel, periodParam, REPORTING_CURRENCIES, sortKey } from "@/lib/commercial";
import { customerQuery, useCustomer } from "@/lib/customer";
import { money, percent } from "@/lib/format";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

import { points } from "./Margin";

type Figure = Exclude<keyof RevenueFigures, "currency">;

/** The workbook's columns, in its order. */
const FIGURES: { key: Figure; header: string; ratio?: boolean }[] = [
  { key: "cloud_consumption", header: "Avg monthly cloud consumption" },
  { key: "manage_revenue", header: "Monthly manage revenue" },
  { key: "manage_cost", header: "Manage cost (actual)" },
  { key: "gross_margin_pct", header: "Gross margin on manage revenue", ratio: true },
  { key: "professional_services", header: "Professional services (one-time)" },
  { key: "hosting_acv", header: "Hosting ACV" },
  { key: "manage_acv", header: "Manage ACV" },
];

const GAP: Record<string, string> = {
  no_sow: "no SOW for the range",
  no_margin: "margin not computed for the range",
  partial_range: "some months have no computed margin",
  no_cost_data: "no consumption in the range",
  no_fx_rate: "no rate in force to convert it",
  mixed_currency: "billed in several currencies and no contract currency",
  csp_not_recorded: "the SOW does not say whether Getronics resells the cloud",
  not_csp: "not a CSP customer",
  no_uplift: "no consumption uplift on the SOW",
};

interface Choice {
  from?: string;
  to?: string;
  compareFrom?: string;
  compareTo?: string;
  currency?: string;
}

const PARAMS: Record<keyof Choice, string> = {
  from: "from",
  to: "to",
  compareFrom: "compare_from",
  compareTo: "compare_to",
  currency: "currency",
};

function useChoice(): Choice & { set: (next: Choice) => void } {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const choice: Choice = {
    from: periodParam(params.get("from")),
    to: periodParam(params.get("to")),
    compareFrom: periodParam(params.get("compare_from")),
    compareTo: periodParam(params.get("compare_to")),
    currency: currencyParam(params.get("currency")),
  };
  function set(next: Choice) {
    const merged = { ...choice, ...next };
    const out = new URLSearchParams();
    for (const [k, name] of Object.entries(PARAMS) as [keyof Choice, string][]) {
      const v = merged[k];
      if (v) out.set(name, v);
    }
    const qs = out.toString();
    router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
  }
  return { ...choice, set };
}

function query(c: Choice, tenantId: string | null): Record<string, string | undefined> {
  return { from: c.from, to: c.to, compare_from: c.compareFrom, compare_to: c.compareTo, currency: c.currency, ...customerQuery(tenantId) };
}

/** "YYYY-MM" moved by `n` months. */
function shift(period: string, n: number): string {
  const [y = 0, m = 1] = period.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + n, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** Months from `first` to `last`, both included. */
function span(first: string, last: string): number {
  const [a = 0, b = 1] = first.split("-").map(Number);
  const [c = 0, d = 1] = last.split("-").map(Number);
  return (c - a) * 12 + d - b + 1;
}

function shown(m: MetricDecimal, ratio: boolean): string {
  return ratio ? percent(m.value) : money(m.value, m.unit);
}

function change(m: MetricDecimal, ratio: boolean): string {
  if (ratio) return points(m.value);
  const text = money(m.value, m.unit);
  const body = text.slice(m.unit.length + 1);
  return body.startsWith("-") ? `−${m.unit} ${body.slice(1)}` : `+${m.unit} ${body}`;
}

function side(block: RevenueRow["current"] | null | undefined, reporting: boolean): RevenueFigures | null {
  if (!block) return null;
  return reporting ? block.reported ?? null : block.contract;
}

function gapText(row: RevenueRow, key: Figure, reporting: boolean): string {
  const gaps = row.current.gaps;
  const found =
    gaps.find((g) => g.figure === key) ?? (reporting ? gaps.find((g) => g.figure === `reported.${key}`) : undefined) ?? gaps.find((g) => g.figure === "row");
  return `Not captured — ${found ? GAP[found.reason] ?? found.reason : "no figure"}`;
}

function Value({ metric, ratio }: { metric: MetricDecimal; ratio: boolean }) {
  return (
    <span className="flex flex-col items-start gap-0.5">
      <span>{shown(metric, ratio)}</span>
      {metric.is_modelled ? (
        <span className="text-xs">
          <ModelledMark />
        </span>
      ) : null}
    </span>
  );
}

function Cell({ row, f, reporting }: { row: RevenueRow; f: (typeof FIGURES)[number]; reporting: boolean }) {
  const ratio = f.ratio ?? false;
  const now = side(row.current, reporting)?.[f.key];
  const before = side(row.compare, reporting)?.[f.key];
  const delta = row.change ? (reporting ? row.change.reported : row.change.contract)?.[f.key] : null;
  return (
    <span className="tabular-nums">
      {now ? <Value metric={now} ratio={ratio} /> : <span className="text-sm text-muted">{gapText(row, f.key, reporting)}</span>}
      {row.compare ? (
        <span className="block text-xs text-muted">
          was {before ? shown(before, ratio) : "not captured"}
          {delta ? ` (${change(delta, ratio)})` : ""}
        </span>
      ) : null}
    </span>
  );
}

function sortValue(row: RevenueRow, key: Figure, reporting: boolean): number | null {
  return sortKey(side(row.current, reporting)?.[key]?.value);
}

function cspText(csp: boolean | null): string {
  return csp === null ? "Not recorded" : csp ? "Yes" : "No";
}

function TotalsTable({ caption, totals, compare, changes }: { caption: string; totals: RevenueTotal[]; compare: RevenueTotal[]; changes: RevenueFigures[] }) {
  if (totals.length === 0) return <p className="text-sm text-muted">No totals: no customer has figures in the range.</p>;
  return (
    <div className="card overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-line">
            <th scope="col" className="px-3 py-2 font-semibold">
              Total
            </th>
            {FIGURES.map((f) => (
              <th key={f.key} scope="col" className="px-3 py-2 font-semibold">
                {f.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {totals.map((t) => {
            const was = compare.find((c) => c.currency === t.currency);
            const delta = changes.find((c) => c.currency === t.currency);
            return (
              <tr key={t.currency} className="border-b border-line-subtle align-top">
                <th scope="row" className="px-3 py-2 font-semibold">
                  {t.currency}
                  <span className="block text-xs font-normal text-muted">{t.customers.value} customers</span>
                </th>
                {FIGURES.map((f) => {
                  const m = t.figures[f.key];
                  const left = t.missing.find((x) => x.figure === f.key);
                  const before = was?.figures[f.key];
                  const d = delta?.[f.key];
                  return (
                    <td key={f.key} className="px-3 py-2 tabular-nums">
                      {m ? <Value metric={m} ratio={f.ratio ?? false} /> : <span className="text-muted">No figure</span>}
                      {left ? <span className="block text-xs text-muted">{left.customers.value} not included</span> : null}
                      {was ? (
                        <span className="block text-xs text-muted">
                          was {before ? shown(before, f.ratio ?? false) : "no figure"}
                          {d ? ` (${change(d, f.ratio ?? false)})` : ""}
                        </span>
                      ) : null}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function rangeLabel(r: { first: string; last: string }): string {
  return r.first === r.last ? periodLabel(r.first) : `${periodLabel(r.first)} to ${periodLabel(r.last)}`;
}

function Controls({ choice, data }: { choice: ReturnType<typeof useChoice>; data: RevenueOverview | null }) {
  const help = useId();
  const from = choice.from ?? data?.range.first ?? "";
  const to = choice.to ?? data?.range.last ?? from;
  const comparing = !!(choice.compareFrom && choice.compareTo);
  const month = (label: string, value: string, onChange: (v: string) => void) => (
    <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
      {label}
      <input type="month" className="field" value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
  return (
    <div className="flex flex-wrap items-end gap-3">
      {month("From", from, (v) => v && choice.set({ from: v, to: to < v ? v : to }))}
      {month("To", to, (v) => v && choice.set({ to: v, from: from > v ? v : from }))}
      <label className="flex items-center gap-2 self-center text-sm">
        <input
          type="checkbox"
          checked={comparing}
          onChange={(e) =>
            choice.set(
              // By default, the range of the same length just before this one.
              e.target.checked && from
                ? { compareFrom: shift(from, -span(from, to)), compareTo: shift(from, -1) }
                : { compareFrom: undefined, compareTo: undefined },
            )
          }
        />
        Compare with another range
      </label>
      {comparing ? (
        <>
          {month("Compare from", choice.compareFrom ?? "", (v) => v && choice.set({ compareFrom: v }))}
          {month("Compare to", choice.compareTo ?? "", (v) => v && choice.set({ compareTo: v }))}
        </>
      ) : null}
      <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
        Reporting currency
        <select className="field" aria-describedby={help} value={choice.currency ?? ""} onChange={(e) => choice.set({ currency: e.target.value || undefined })}>
          <option value="">None (contract currency)</option>
          {REPORTING_CURRENCIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>
      <p id={help} className="max-w-sm text-xs text-muted">
        Figures are computed in each contract’s currency. A reporting currency converts each at the rate in force on its date: consumption on its usage day,
        revenue and margin at their month’s average, SOW values when the SOW took effect.
      </p>
    </div>
  );
}

export function Revenue() {
  useAssistantContext({ screen: "margin" });
  const me = useMe();
  const { token } = useAuth();
  const choice = useChoice();
  const { tenantId } = useCustomer();
  const [exportError, setExportError] = useState<string | null>(null);
  const q = query(choice, tenantId);
  const loaded = useApi((t) => apiGet(t, "/v1/commercial/revenue", {}, q), `revenue:${JSON.stringify(q)}`);
  const header = (
    <PageHeader
      title="Revenue & margin"
      subtitle="Each customer’s cloud consumption, manage revenue, cost and margin over a range of months, with professional services and contract values."
    />
  );
  if (loaded.status === "loading")
    return (
      <>
        {header}
        <Loading what="revenue and margin" />
      </>
    );
  if (loaded.status === "error")
    return (
      <>
        {header}
        <LoadError error={loaded.error} what="revenue and margin" />
      </>
    );
  const data = loaded.data;
  const reporting = data.reporting_currency !== null;
  const order = new Map([...data.rows].sort((a, b) => a.tenant.name.localeCompare(b.tenant.name)).map((r, i) => [r.tenant.id, i + 1]));

  async function exportCsv() {
    setExportError(null);
    try {
      const blob = await apiDownload(await token(), "/v1/commercial/revenue.csv", {}, { from: data.range.first, to: data.range.last, currency: choice.currency, ...customerQuery(tenantId) });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `revenue-margin-${data.range.first}-${data.range.last}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch {
      setExportError("The CSV could not be downloaded. Try again.");
    }
  }

  const columns: Column<RevenueRow>[] = [
    { key: "sl", header: "Sl No", value: (r) => order.get(r.tenant.id) ?? null, sortable: true },
    {
      key: "customer",
      header: "Customer name",
      value: (r) => r.tenant.name,
      render: (r) => (
        <Link href={`/margin/${r.tenant.id}`} className="link font-medium">
          {r.tenant.name}
        </Link>
      ),
      sortable: true,
    },
    { key: "csp", header: "CSP", value: (r) => cspText(r.csp), sortable: true },
    ...FIGURES.map(
      (f): Column<RevenueRow> => ({
        key: f.key,
        header: f.header,
        value: (r) => sortValue(r, f.key, reporting),
        render: (r) => <Cell row={r} f={f} reporting={reporting} />,
        sortable: true,
      }),
    ),
  ];
  const totals = reporting ? (data.totals.reported ? [data.totals.reported] : []) : data.totals.contract;
  const compareTotals = data.compare_totals ? (reporting ? (data.compare_totals.reported ? [data.compare_totals.reported] : []) : data.compare_totals.contract) : [];
  const totalChanges = data.totals_change ? (reporting ? (data.totals_change.reported ? [data.totals_change.reported] : []) : data.totals_change.contract) : [];

  return (
    <>
      {header}
      <div className="mb-4">
        <Controls choice={choice} data={data} />
      </div>
      <section aria-labelledby="revenue-customers" className="space-y-2">
        <h2 id="revenue-customers" className="section-title">
          {rangeLabel(data.range)}
          {data.compare_range ? <span className="ml-2 text-sm font-normal text-muted">against {rangeLabel(data.compare_range)}</span> : null}
        </h2>
        <p className="text-sm text-muted">
          Consumption, revenue, cost and margin are monthly averages over the range; cost falls back to modelled where effort was incomplete. Hosting and
          manage ACV come from the SOW where it states them; otherwise manage ACV is 12 × the SOW’s monthly manage revenue and hosting ACV, for CSP customers
          only, is 12 × average consumption × (1 + the SOW’s uplift). Both are then marked Modelled. Professional services is the SOW’s figure until
          ServiceNow project tasks are ingested.
        </p>
        {exportError ? (
          <p role="alert" className="text-sm text-band-critical">
            {exportError}
          </p>
        ) : null}
        <DataTable
          id="revenue-customers"
          caption="Revenue and margin by customer"
          rows={data.rows}
          rowKey={(r) => r.tenant.id}
          columns={columns}
          facets={[{ key: "csp", label: "CSP", value: (r) => cspText(r.csp) }]}
          role={me.role}
          initial={{ sort: { key: "sl", dir: "asc" } }}
          empty={{ title: "No customers in your scope", next: "A platform administrator assigns customers to your account." }}
          onExport={() => void exportCsv()}
        />
      </section>
      <section aria-labelledby="revenue-totals" className="mt-6 space-y-2">
        <h2 id="revenue-totals" className="section-title">
          Totals{reporting ? ` in ${data.reporting_currency ?? ""}` : " by contract currency"}
        </h2>
        <TotalsTable caption="Revenue and margin totals" totals={totals} compare={compareTotals} changes={totalChanges} />
      </section>
    </>
  );
}
