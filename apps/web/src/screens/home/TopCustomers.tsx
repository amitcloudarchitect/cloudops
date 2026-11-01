"use client";

// Top 10 customers (ADR 0038, staff only), by a modelled health score, least
// healthy first: what needs attention. Ranking by revenue reads the revenue table
// and joins once it is served; until then a commercial role is told so. Choosing a
// customer narrows every section to it, through the top bar's selector.

import type { HealthPart, TopCustomer, TopCustomers as Top } from "@cloudops/contracts";

import { changeText, metricText } from "@/components/CompareTable";
import { HealthBadge } from "@/components/HealthBadge";
import { ModelledMark, Provenance } from "@/components/Provenance";
import { Gap } from "@/components/Widget";
import { apiGet } from "@/lib/api";
import { useCustomer } from "@/lib/customer";
import { rangeLabel } from "@/lib/range";
import { useApi } from "@/lib/useApi";

import { Both, HomeSection, queryKey, RangeControls, useRangeChoice, useRangeQuery } from "./frame";

const PATH = "/v1/home/sections/top-customers";
const PART_LABEL: Record<HealthPart["part"], string> = {
  p1_p2: "Open P1 and P2 incidents",
  availability: "Critical resources unavailable",
  vulnerabilities: "Open severity 4 and 5 vulnerabilities",
  security_events: "Open high security events",
};

function Parts({ c }: { c: TopCustomer }) {
  const shown = c.parts.filter((p) => p.penalty && Number(p.penalty.value) > 0);
  if (shown.length === 0) return <span className="text-xs text-muted">No penalty</span>;
  return (
    <ul className="text-xs text-muted">
      {shown.map((p) => (
        <li key={p.part}>
          {PART_LABEL[p.part]}: {p.observed ? metricText(p.observed) : "—"}, −{p.penalty ? metricText(p.penalty) : "0"}
        </li>
      ))}
    </ul>
  );
}

export function TopCustomersSection() {
  const { setTenantId, tenantId } = useCustomer();
  const [choice, setChoice] = useRangeChoice();
  const qa = useRangeQuery(choice.a);
  const qb = useRangeQuery(choice.b);
  const a = useApi((t) => (qa ? apiGet(t, PATH, {}, qa) : Promise.reject(new Error("invalid range"))), queryKey(PATH, qa));
  const b = useApi((t) => (qb ? apiGet(t, PATH, {}, qb) : Promise.resolve(null)), queryKey(PATH, qb));
  return (
    <HomeSection
      id="top"
      title="Top 10 customers"
      subtitle="By a modelled health score, least healthy first. Choose a customer to narrow every section to it."
      href="/customers"
    >
      <RangeControls section="Top 10 customers" choice={choice} onChange={setChoice} />
      {tenantId ? <p className="mb-3 text-sm text-muted">Narrowed to one customer in the top bar: choose All customers there to rank them all.</p> : null}
      <Both a={a} b={b} what="the top customers">
        {(top: Top, other: Top | null) => {
          const otherBy = new Map((other?.customers ?? []).map((c) => [c.tenant.id, c]));
          if (top.customers.length === 0) return <Gap text="No customer in scope." />;
          const otherLabel = other && choice.b ? rangeLabel(choice.b) : null;
          return (
            <>
              <table className="w-full border-collapse text-sm" data-testid="top-customers">
                <caption className="sr-only">
                  The ten least healthy customers first, {rangeLabel(choice.a)}
                </caption>
                <thead>
                  <tr className="text-left text-xs text-muted">
                    <th scope="col" className="pb-1 pr-2 font-semibold">#</th>
                    <th scope="col" className="pb-1 pr-2 font-semibold">Customer</th>
                    <th scope="col" className="pb-1 pr-2 font-semibold">Health</th>
                    {otherLabel ? <th scope="col" className="pb-1 pl-2 text-right font-semibold">{otherLabel}</th> : null}
                  </tr>
                </thead>
                <tbody>
                  {top.customers.map((c) => {
                    const prior = otherBy.get(c.tenant.id);
                    const priorText = prior ? `Health ${metricText(prior.health)} (${changeText(c.health, prior.health) ?? "—"})` : "Not in that range's top 10";
                    return (
                      <tr key={c.tenant.id} className="border-t border-line-subtle align-top">
                        <td className="py-1.5 pr-2 tabular-nums text-muted">{metricText(c.rank)}</td>
                        <th scope="row" className="py-1.5 pr-2 text-left font-normal">
                          <button type="button" className="link text-left font-semibold" onClick={() => setTenantId(c.tenant.id)}>
                            {c.tenant.name}
                            <span className="sr-only">: show this customer on every section</span>
                          </button>
                        </th>
                        <td className="py-1.5 pr-2">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-semibold tabular-nums text-ink">{metricText(c.health)}</span>
                            <HealthBadge band={c.health_band} />
                          </div>
                          <Parts c={c} />
                        </td>
                        {otherLabel ? <td className="py-1.5 pl-2 text-right text-xs text-muted">{priorText}</td> : null}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div className="mt-3 flex flex-col gap-1 border-t border-line-subtle pt-2 text-xs text-muted">
                {top.customers[0] ? <Provenance metric={top.customers[0].health} /> : null}
                <p className="flex flex-wrap items-center gap-2">
                  <ModelledMark />
                  Health is modelled: 100, less 20 per open P1 and 10 per open P2 incident (at most 40), up to 20 for the share of critical resources unavailable in inventory,
                  2 per open severity 4 or 5 vulnerability (at most 20) and 5 per open high security event (at most 20), as at the end of the range. Healthy from 80, watch from
                  60, at risk from 40.
                </p>
                <p>Ranked {metricText(top.ranked)} customers.</p>
                {top.revenue && !top.revenue.available ? (
                  <p data-testid="top-revenue-pending">
                    Ranking by revenue (hosting, managed services and projects): no data source yet. It joins once the revenue table is served.
                  </p>
                ) : null}
              </div>
            </>
          );
        }}
      </Both>
    </HomeSection>
  );
}
