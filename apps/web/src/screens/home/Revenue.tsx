"use client";

// Revenue & margin (ADR 0038). Staff: hosting cost by cloud; with commercial.read,
// the hosting price, managed services revenue, cost and margin, and project prices
// once a source holds them. Totals only in a reporting currency, each line
// converted at the rate on its own date. A customer sees the price of its own
// consumption (ADR 0021), closed months only, and nothing of Getronics' cost.

import type { RevenueSection as Revenue } from "@cloudops/contracts";

import { CompareTable, metricText, type CompareRow } from "@/components/CompareTable";
import { MetricCard } from "@/components/MetricCard";
import { Provenance } from "@/components/Provenance";
import { Gap } from "@/components/Widget";
import { apiGet } from "@/lib/api";
import { REPORTING_CURRENCIES } from "@/lib/commercial";
import { month } from "@/lib/format";
import { rangeLabel } from "@/lib/range";
import { useApi } from "@/lib/useApi";

import { Both, HomeSection, Part, queryKey, RangeControls, useRangeChoice, useRangeQuery } from "./frame";

const PATH = "/v1/home/sections/revenue";
const PROVIDER: Record<string, string> = { azure: "Azure", aws: "AWS" };
const NO_SOURCE = "No data source yet";

function CurrencyPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
      Reporting currency
      <select className="field" value={value} onChange={(e) => onChange(e.target.value)}>
        {REPORTING_CURRENCIES.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
        <option value="">None (as billed)</option>
      </select>
    </label>
  );
}

function totalsRows(r: Revenue, o: Revenue | null): CompareRow[] {
  const t = r.totals;
  const u = o?.totals ?? null;
  const noRate = "No rate for a line";
  const rows: CompareRow[] = [{ key: "hosting_cost", label: "Hosting cost (Azure and AWS consumption)", a: t?.hosting_cost, b: u?.hosting_cost, missing: noRate }];
  if (!r.commercial) return rows;
  return [
    ...rows,
    { key: "hosting_price", label: "Hosting revenue (consumption price)", a: t?.hosting_price, b: u?.hosting_price, missing: "Not priced: no uplift or no rate" },
    { key: "managed_revenue", label: "Managed services revenue", a: t?.managed_revenue, b: u?.managed_revenue, missing: noRate },
    { key: "managed_cost", label: "Managed services cost", a: t?.managed_cost, b: u?.managed_cost, missing: noRate },
    { key: "managed_margin", label: "Managed services margin", a: t?.managed_margin, b: u?.managed_margin, missing: noRate },
    { key: "projects_new", label: "New project price (closed project tasks)", a: null, b: null, missing: NO_SOURCE },
    { key: "projects_proposed", label: "Proposed initiatives price (open project tasks)", a: null, b: null, missing: NO_SOURCE },
    { key: "revenue", label: "Revenue: hosting and managed services", a: t?.revenue, b: u?.revenue, missing: "Not complete", total: true },
    { key: "margin", label: "Margin", a: t?.margin, b: u?.margin, missing: "Not complete", total: true },
    { key: "margin_share", label: "Margin share of revenue", a: t?.margin_share, b: u?.margin_share, missing: "Not complete" },
  ];
}

function StaffRevenue({ r, other, headings }: { r: Revenue; other: Revenue | null; headings: { a: string; b?: string } }) {
  const t = r.totals;
  return (
    <div className="flex flex-col gap-5">
      {t ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard label="Hosting cost" metric={t.hosting_cost} display={metricText} href="/finops" unavailable="A line has no rate on its date." />
          {r.commercial ? (
            <>
              <MetricCard label="Managed services revenue" metric={t.managed_revenue} display={metricText} href="/margin" unavailable="A month has no rate on its date." />
              <MetricCard label="Managed services margin" metric={t.managed_margin} display={metricText} href="/margin" unavailable="A month has no rate on its date." />
              <MetricCard label="Revenue" metric={t.revenue} display={metricText} href="/margin" unavailable="Not complete: a hosting line has no price or rate." />
            </>
          ) : null}
        </div>
      ) : (
        <p className="text-sm text-muted">Choose a reporting currency to total across currencies. Each line below stays in the currency it was billed in.</p>
      )}
      {t ? (
        <Part title="By component">
          <CompareTable caption="Revenue and margin by component, in the reporting currency" {...headings} rows={totalsRows(r, other)} provenance={t.hosting_cost ?? t.managed_revenue} />
        </Part>
      ) : null}
      {!r.commercial ? <Gap text="Managed services, margin and project prices are commercial: not available to your role." /> : null}
      <div className="grid gap-5 xl:grid-cols-2">
        <Part title="Hosting by cloud, as billed">
          {r.hosting && r.hosting.length > 0 ? (
            <CompareTable
              caption="Hosting cost and price by cloud and billing currency"
              component="Cloud and currency"
              a="Cost"
              b={r.commercial ? "Price" : undefined}
              change={false}
              rows={r.hosting.map((h) => ({ key: `${h.provider}-${h.currency}`, label: `${PROVIDER[h.provider] ?? h.provider} · ${h.currency}`, a: h.cost, b: h.price, missing: "Not priced" }))}
              provenance={r.hosting[0]?.cost}
            />
          ) : (
            <Gap text="No consumption billed in this range." />
          )}
        </Part>
        {r.managed ? (
          <Part title="Managed services, as contracted">
            {r.managed.length > 0 ? (
              <CompareTable
                caption="Managed services revenue and margin by contract currency"
                component="Contract currency"
                a="Revenue"
                b="Margin"
                change={false}
                rows={r.managed.map((m) => ({ key: m.currency, label: `${m.currency} · ${metricText(m.months)} customer-months${Number(m.provisional.value) > 0 ? `, ${metricText(m.provisional)} provisional` : ""}`, a: m.revenue, b: m.margin }))}
                provenance={r.managed[0]?.revenue}
              />
            ) : (
              <Gap text="No margin computed for a month in this range." />
            )}
          </Part>
        ) : null}
      </div>
      {r.projects && !r.projects.available ? (
        <Gap text="Project prices: no data source yet. New project price (closed ServiceNow project tasks) and proposed initiatives (open ones) appear here once project task prices are ingested." />
      ) : null}
    </div>
  );
}

function CustomerRevenue({ r, other, label, otherLabel }: { r: Revenue; other: Revenue | null; label: string; otherLabel?: string }) {
  const blocks = [{ heading: label, body: r }, ...(other && otherLabel ? [{ heading: otherLabel, body: other }] : [])];
  return (
    <div className="grid gap-5 xl:grid-cols-2">
      {blocks.map(({ heading, body }) => (
        <Part key={heading} title={`Cloud consumption price · ${heading}`}>
          {body.customer_price && body.customer_price.length > 0 ? (
            <table className="w-full border-collapse text-sm">
              <caption className="sr-only">The price of your cloud consumption by month, cloud and currency</caption>
              <thead>
                <tr className="text-left text-xs text-muted">
                  <th scope="col" className="pb-1 font-semibold">Month</th>
                  <th scope="col" className="pb-1 font-semibold">Cloud</th>
                  <th scope="col" className="pb-1 text-right font-semibold">Price</th>
                  <th scope="col" className="pb-1 text-right font-semibold">Converted</th>
                </tr>
              </thead>
              <tbody>
                {body.customer_price.map((p) => (
                  <tr key={`${p.period}-${p.provider}-${p.currency}`} className="border-t border-line-subtle">
                    <th scope="row" className="py-1.5 pr-2 text-left font-normal text-ink">{month(p.period)}</th>
                    <td className="py-1.5 text-ink">{PROVIDER[p.provider] ?? p.provider}</td>
                    <td className="py-1.5 text-right tabular-nums text-ink">{p.price ? metricText(p.price) : <span className="text-muted">Not priced yet</span>}</td>
                    <td className="py-1.5 text-right tabular-nums text-ink">{p.converted ? metricText(p.converted) : <span className="text-muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Gap text="No closed month in this range has consumption. The current month shows once it closes." />
          )}
          {body.customer_price?.[0]?.price ? (
            <div className="mt-2">
              <Provenance metric={body.customer_price[0].price} />
            </div>
          ) : null}
        </Part>
      ))}
    </div>
  );
}

export function RevenueSection({ customer, currency, onCurrency }: { customer: boolean; currency: string; onCurrency: (c: string) => void }) {
  const [choice, setChoice] = useRangeChoice(customer ? "last_month" : "mtd");
  const extra = { currency: currency || undefined };
  const qa = useRangeQuery(choice.a, extra);
  const qb = useRangeQuery(choice.b, extra);
  const a = useApi((t) => (qa ? apiGet(t, PATH, {}, qa) : Promise.reject(new Error("invalid range"))), queryKey(PATH, qa));
  const b = useApi((t) => (qb ? apiGet(t, PATH, {}, qb) : Promise.resolve(null)), queryKey(PATH, qb));
  return (
    <HomeSection
      id="revenue"
      title={customer ? "Your cloud consumption" : "Revenue & margin"}
      subtitle={
        customer
          ? "The price of your cloud consumption by closed month. The current month shows once it closes."
          : "Hosting cost and revenue, managed services revenue, cost and margin, and project prices."
      }
      href={customer ? "/customers" : "/margin"}
    >
      <RangeControls section="Revenue and margin" choice={choice} onChange={setChoice}>
        <CurrencyPicker value={currency} onChange={onCurrency} />
      </RangeControls>
      <Both a={a} b={b} what="revenue and margin">
        {(r, other) => {
          const headings = other && choice.b ? { a: rangeLabel(choice.a), b: rangeLabel(choice.b) } : { a: rangeLabel(choice.a) };
          return r.view === "customer" ? (
            <CustomerRevenue r={r} other={other} label={headings.a} otherLabel={headings.b} />
          ) : (
            <StaffRevenue r={r} other={other} headings={headings} />
          );
        }}
      </Both>
    </HomeSection>
  );
}
