"use client";

// Revenue & margin (ADR 0038). Staff revenue, cost and margin are the revenue
// table's, and project prices the project task ingest's: until each is served, the
// section names every component it will show and says it has no data source yet,
// never a figure of its own. A customer sees the price of its own consumption (ADR
// 0021), closed months only, converted at each day's rate, and nothing of Getronics'
// cost.

import { useState } from "react";

import type { RevenueSection as Revenue } from "@cloudops/contracts";

import { metricText } from "@/components/CompareTable";
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

// What the staff view will show once its sources are served, and where each comes from.
const COMPONENTS: readonly { label: string; source: string }[] = [
  { label: "Hosting cost (Azure and AWS consumption)", source: "revenue table" },
  { label: "Managed services revenue", source: "revenue table" },
  { label: "Managed services cost", source: "revenue table" },
  { label: "Margin", source: "revenue table" },
  { label: "New project price (closed project tasks)", source: "ServiceNow project tasks" },
  { label: "Proposed initiatives price (open project tasks)", source: "ServiceNow project tasks" },
];

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

function StaffRevenue({ r }: { r: Revenue }) {
  const pending = (s: Revenue["revenue"]) => s !== null && !s.available;
  return (
    <div className="flex flex-col gap-3">
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">Revenue and margin by component, and the source each waits for</caption>
        <thead>
          <tr className="text-left text-xs text-muted">
            <th scope="col" className="pb-1 pr-2 font-semibold">
              Component
            </th>
            <th scope="col" className="pb-1 pr-2 font-semibold">
              Source
            </th>
            <th scope="col" className="pb-1 text-right font-semibold">
              Figure
            </th>
          </tr>
        </thead>
        <tbody>
          {COMPONENTS.map((c) => {
            const waiting = c.source === "revenue table" ? pending(r.revenue) : pending(r.projects);
            return (
              <tr key={c.label} className="border-t border-line-subtle">
                <th scope="row" className="py-1.5 pr-2 text-left font-normal text-ink">
                  {c.label}
                </th>
                <td className="py-1.5 pr-2 text-ink">{c.source}</td>
                <td className="py-1.5 text-right text-muted">{waiting ? "No data source yet" : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <Gap text="No data source yet. Revenue, cost and margin come from the revenue table, and project prices from the ServiceNow project task ingest; this section shows them, by range and compared, once each is served." />
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

export function RevenueSection({ customer }: { customer: boolean }) {
  const [choice, setChoice] = useRangeChoice(customer ? "last_month" : "mtd");
  const [currency, setCurrency] = useState("EUR");
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
        {customer ? <CurrencyPicker value={currency} onChange={setCurrency} /> : null}
      </RangeControls>
      <Both a={a} b={b} what="revenue and margin">
        {(r, other) => {
          const headings = other && choice.b ? { a: rangeLabel(choice.a), b: rangeLabel(choice.b) } : { a: rangeLabel(choice.a) };
          return r.view === "customer" ? <CustomerRevenue r={r} other={other} label={headings.a} otherLabel={headings.b} /> : <StaffRevenue r={r} />;
        }}
      </Both>
    </HomeSection>
  );
}
