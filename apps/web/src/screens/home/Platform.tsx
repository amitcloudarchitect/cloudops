"use client";

// Platform (ADR 0038, staff only): connector health now, customers onboarded by the
// end of the range split by cloud, and each customer's size band: the higher of its
// spend band and its resource band, on thresholds set in Settings.

import Link from "next/link";

import type { CustomerSize, PlatformSection as Platform } from "@cloudops/contracts";

import { CompareTable, metricText, type CompareRow } from "@/components/CompareTable";
import { MetricCard } from "@/components/MetricCard";
import { Provenance } from "@/components/Provenance";
import { Body, Gap, Stat, useShared } from "@/components/Widget";
import { apiGet } from "@/lib/api";
import { rangeLabel } from "@/lib/range";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

import { Both, HomeSection, Part, queryKey, RangeControls, useRangeChoice, useRangeQuery } from "./frame";

const PATH = "/v1/home/sections/platform";
export const SIZE_LABEL: Record<string, string> = { large: "Large", medium: "Medium", small: "Small" };
const PROVIDER: Record<string, string> = { azure: "Azure", aws: "AWS" };
const SPEND_REASON: Record<string, string> = {
  not_permitted: "Not available to your role",
  no_cost: "No cost that month",
  no_fx_rate: "No rate to convert",
};

function SizeBadge({ band }: { band: string }) {
  // Size is not health: a neutral outlined label, named in text.
  return <span className="inline-block rounded-full border border-line px-2 py-0.5 text-xs font-semibold text-ink">{SIZE_LABEL[band] ?? band}</span>;
}

function Connectors() {
  const loaded = useShared("connectors.health", (t) => apiGet(t, "/v1/connectors/health"));
  return (
    <Body loaded={loaded} what="connector health">
      {(h) => (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Outage" value={metricText(h.outages)} />
            <Stat label="Late" value={metricText(h.late)} />
            <Stat label="Needs attention" value={metricText(h.attention)} />
            <Stat label="Never run" value={metricText(h.never_run)} />
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <Provenance metric={h.outages} />
            <Link href="/connectors" className="link text-xs font-semibold">
              Every connector
            </Link>
          </div>
        </>
      )}
    </Body>
  );
}

function SizeTable({ p }: { p: Platform }) {
  return (
    <table className="w-full border-collapse text-sm">
      <caption className="sr-only">Each customer&rsquo;s size band, with its spend and resources</caption>
      <thead>
        <tr className="text-left text-xs text-muted">
          <th scope="col" className="pb-1 pr-2 font-semibold">Customer</th>
          <th scope="col" className="pb-1 pr-2 font-semibold">Clouds</th>
          <th scope="col" className="pb-1 pl-1 text-right font-semibold">Spend, {p.spend_month}</th>
          <th scope="col" className="pb-1 pl-1 text-right font-semibold">Resources</th>
          <th scope="col" className="pb-1 pl-2 text-right font-semibold">Band</th>
        </tr>
      </thead>
      <tbody>
        {p.sizes.map((s: CustomerSize) => (
          <tr key={s.tenant.id} className="border-t border-line-subtle align-top">
            <th scope="row" className="py-1.5 pr-2 text-left font-normal text-ink">{s.tenant.name}</th>
            <td className="py-1.5 pr-2 text-ink">{s.providers.length ? s.providers.map((x) => PROVIDER[x] ?? x).join(", ") : "None yet"}</td>
            <td className="py-1.5 pl-1 text-right tabular-nums text-ink">
              {s.spend ? metricText(s.spend) : <span className="text-muted">{SPEND_REASON[s.spend_reason ?? ""] ?? "—"}</span>}
              {s.spend_band ? <span className="block text-xs text-muted">{SIZE_LABEL[s.spend_band]} by spend</span> : null}
            </td>
            <td className="py-1.5 pl-1 text-right tabular-nums text-ink">
              {metricText(s.resources)}
              <span className="block text-xs text-muted">{SIZE_LABEL[s.resource_band]} by resources</span>
            </td>
            <td className="py-1.5 pl-2 text-right">
              <SizeBadge band={s.band} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function PlatformSection() {
  const me = useMe();
  const [choice, setChoice] = useRangeChoice();
  const qa = useRangeQuery(choice.a);
  const qb = useRangeQuery(choice.b);
  const a = useApi((t) => (qa ? apiGet(t, PATH, {}, qa) : Promise.reject(new Error("invalid range"))), queryKey(PATH, qa));
  const b = useApi((t) => (qb ? apiGet(t, PATH, {}, qb) : Promise.resolve(null)), queryKey(PATH, qb));
  return (
    <HomeSection id="platform" title="Platform" subtitle="Connector health, customers onboarded by cloud, and customer size bands." href="/settings/size-bands" hrefLabel="Size band thresholds">
      {can(me, "connectors.read") ? (
        <div className="mb-5">
          <Part title="Connector health, now">
            <Connectors />
          </Part>
        </div>
      ) : null}
      <RangeControls section="Platform" choice={choice} onChange={setChoice} />
      <Both a={a} b={b} what="the platform">
        {(p, other) => {
          const headings = other && choice.b ? { a: rangeLabel(choice.a), b: rangeLabel(choice.b) } : { a: rangeLabel(choice.a) };
          const provider = (x: Platform | null, name: string) => x?.by_provider.find((v) => v.provider === name)?.customers;
          const bandOf = (x: Platform | null, name: string) => x?.bands.find((v) => v.band === name)?.customers;
          const rows: CompareRow[] = [
            { key: "customers", label: "Customers onboarded", a: p.customers, b: other?.customers, total: true },
            { key: "azure", label: "On Azure", a: provider(p, "azure"), b: provider(other, "azure") },
            { key: "aws", label: "On AWS", a: provider(p, "aws"), b: provider(other, "aws") },
            { key: "both", label: "On both", a: p.both_clouds, b: other?.both_clouds },
            { key: "none", label: "No cloud account yet", a: p.no_cloud, b: other?.no_cloud },
            ...["large", "medium", "small"].map((x) => ({ key: x, label: `${SIZE_LABEL[x]} customers`, a: bandOf(p, x), b: bandOf(other, x) })),
          ];
          const policy = p.policy;
          return (
            <div className="flex flex-col gap-5">
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <MetricCard label="Customers onboarded" metric={p.customers} display={metricText} href="/mapping" />
                <MetricCard label="On Azure" metric={provider(p, "azure")} display={metricText} />
                <MetricCard label="On AWS" metric={provider(p, "aws")} display={metricText} />
                <MetricCard label="Large customers" metric={bandOf(p, "large")} display={metricText} />
              </div>
              <Part title="Customers by cloud and size">
                <CompareTable caption="Customers onboarded by the end of each range, by cloud and size band" component="Customers" {...headings} rows={rows} provenance={p.customers} />
              </Part>
              <Part title={`Size bands, as at ${rangeLabel(choice.a)}`}>
                <p className="mb-2 text-sm text-muted">
                  The higher of two bands. Spend in {policy.currency} a month: Large above {metricText(policy.spend_large_above)}, Medium from{" "}
                  {metricText(policy.spend_medium_from)}. Resources: Large above {metricText(policy.resources_large_above)}, Medium from{" "}
                  {metricText(policy.resources_medium_from)}. Spend is the last whole month the range reaches ({p.spend_month}), converted at each day&rsquo;s rate. Thresholds
                  version {metricText(policy.version)}{" "}
                  <Link href="/settings/size-bands" className="link">
                    {can(me, "sizebands.manage") ? "change them in Settings" : "see their history"}
                  </Link>
                  .
                </p>
                {p.sizes.length > 0 ? <SizeTable p={p} /> : <Gap text="No customer onboarded by the end of the range." />}
              </Part>
            </div>
          );
        }}
      </Both>
    </HomeSection>
  );
}
