"use client";

import type { CustomerOverview, CustomerPrice } from "@cloudops/contracts";

import { EmptyState } from "@/components/EmptyState";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { count, money, month } from "@/lib/format";
import { band, THRESHOLDS, type Band } from "@/lib/health";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const ORDER: Record<Band, number> = { critical: 0, "at-risk": 1, watch: 2, healthy: 3, unknown: 4 };

function worst(c: CustomerOverview): Band {
  const bands: Band[] = [
    band(c.sla_breached_open?.value, THRESHOLDS.slaBreachedOpen),
    band(c.critical_vulnerabilities?.value, THRESHOLDS.criticalVulnerabilities),
    band(c.open_incidents?.value, THRESHOLDS.openIncidents),
  ].filter((b) => b !== "unknown");
  return bands.sort((a, b) => ORDER[a] - ORDER[b])[0] ?? "unknown";
}

function workLink(tenant: string, facets: Record<string, string>): string {
  return `/work?${new URLSearchParams({ customer: tenant, ...facets }).toString()}`;
}

// A customer sees its consumption at the price Getronics charges, never
// Getronics' cost, and only for a closed month: nothing finer, so no single
// line's price shows (ADR 0021). Not captured is said, never shown as zero.
function PriceCards({ prices }: { prices: CustomerPrice[] }) {
  if (prices.length === 0) {
    return <MetricCard label="Cloud consumption, last month" metric={null} unavailable="No consumption last month" />;
  }
  return (
    <>
      {prices.map((p) => (
        <MetricCard
          key={`${p.period}-${p.currency}`}
          label={`Cloud consumption, ${month(p.period)} (${p.currency})`}
          metric={p.price}
          display={(x) => money(String(x.value), x.unit)}
          unavailable={p.not_captured ? "Not yet priced: your Getronics service manager can confirm the rate" : undefined}
        />
      ))}
    </>
  );
}

export function CustomerOverviewView() {
  const me = useMe();
  useAssistantContext(
    me.tenants.length > 1 ? { screen: "customers" } : { screen: "customer", tenantId: me.tenants[0]?.id ?? null },
  );
  const loaded = useApi((token) => apiGet(token, "/v1/customers"), "customers");
  if (loaded.status === "loading") return <Loading what="your customers" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="customers" />;
  const customers = [...loaded.data].sort((a, b) => ORDER[worst(a)] - ORDER[worst(b)] || a.tenant.name.localeCompare(b.tenant.name));
  const tickets = can(me, "tickets.read");
  const many = me.tenants.length > 1;

  return (
    <>
      <PageHeader
        title={many ? "Customers" : (customers[0]?.tenant.name ?? "Customer")}
        subtitle={many ? "Most at risk first. Each figure links to the work behind it." : "Your services at a glance."}
      />
      {customers.length === 0 ? (
        <EmptyState title="No customers assigned" next="A platform administrator assigns customers to your account." />
      ) : (
        <ul className="space-y-6">
          {customers.map((c) => (
            <li key={c.tenant.id} aria-labelledby={`customer-${c.tenant.id}`}>
              {many ? (
                <h2 id={`customer-${c.tenant.id}`} className="mb-2 section-title">
                  {c.tenant.name}
                </h2>
              ) : (
                <h2 id={`customer-${c.tenant.id}`} className="sr-only">
                  {c.tenant.name}
                </h2>
              )}
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                <MetricCard label="Resources" metric={c.resources} display={(m) => count(Number(m.value))} href={c.resources ? "/resources" : undefined} />
                <MetricCard
                  label="Open incidents"
                  metric={c.open_incidents}
                  display={(m) => count(Number(m.value))}
                  band={c.open_incidents ? band(c.open_incidents.value, THRESHOLDS.openIncidents) : undefined}
                  href={tickets ? workLink(c.tenant.name, { kind: "Incident" }) : undefined}
                />
                <MetricCard
                  label="SLA breached, open"
                  metric={c.sla_breached_open}
                  display={(m) => count(Number(m.value))}
                  band={c.sla_breached_open ? band(c.sla_breached_open.value, THRESHOLDS.slaBreachedOpen) : undefined}
                  href={tickets ? workLink(c.tenant.name, { sla: "Breached" }) : undefined}
                />
                <MetricCard
                  label="Critical vulnerabilities"
                  metric={c.critical_vulnerabilities}
                  display={(m) => count(Number(m.value))}
                  band={c.critical_vulnerabilities ? band(c.critical_vulnerabilities.value, THRESHOLDS.criticalVulnerabilities) : undefined}
                />
                {c.price_last_month !== null ? (
                  <PriceCards prices={c.price_last_month} />
                ) : c.cost_30d === null ? (
                  <MetricCard label="Cloud cost, 30 days" metric={null} />
                ) : c.cost_30d.length === 0 ? (
                  <MetricCard label="Cloud cost, 30 days" metric={null} unavailable="No cost in the last 30 days" />
                ) : (
                  c.cost_30d.map((m) => (
                    <MetricCard key={m.unit} label={`Cloud cost, 30 days (${m.unit})`} metric={m} display={(x) => money(String(x.value), x.unit)} />
                  ))
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
