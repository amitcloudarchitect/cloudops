"use client";

// Platform observability (migration 0032, ``platform.observe``): how the platform
// itself is used and behaving. Usage by role, AI gateway calls, tokens and spend by
// agent and customer, triage throughput, connector health per source and the
// platform's own cost, each from its own endpoint so one slow or refused section
// never blanks the others. Every figure carries its source and as-of time, and a
// modelled one says so. Connector detail stays on the Connectors page; the
// platform's metrics, traces and logs are in Managed Grafana and Application
// Insights, linked from here once stack 03-observability is applied.

import Link from "next/link";
import { useState, type ReactNode } from "react";

import type {
  AiAgent,
  AiCustomer,
  AiDay,
  AiFigures,
  PlatformCost,
  SourceHealth,
  TriageCustomer,
  TriageDay,
  UsageDay,
  UsageWeek,
} from "@cloudops/contracts";

import { BarTable } from "@/components/BarTable";
import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { Provenance, type AnyMetric } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { config } from "@/lib/config";
import { customerQuery, useCustomer } from "@/lib/customer";
import { count, date, dateTime, money, percent } from "@/lib/format";
import { band, THRESHOLDS } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi, type Loaded } from "@/lib/useApi";

const WINDOWS = [7, 30, 90] as const;
type Days = (typeof WINDOWS)[number];

const AGENT_LABEL: Record<AiAgent["agent"], string> = {
  classifier: "Classifier",
  enricher: "Enricher",
  drafter: "Drafter (reports and controls)",
  assistant: "AMARA assistant",
  other: "Other (embeddings and indexing)",
};

function n(metric: AnyMetric): number {
  return Number(metric.value);
}

function size(value: string | number | null | undefined): number {
  const x = Number(value ?? 0);
  return Number.isFinite(x) && x > 0 ? x : 0;
}

function spendText(f: AiFigures): string {
  if (!f.spend) return "Not shown";
  if (!f.spend.length) return "—";
  return f.spend.map((s) => money(String(s.spend.value), s.spend.unit, 4)).join(", ");
}

function Section({ id, title, intro, children }: { id: string; title: string; intro: ReactNode; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="mb-10 space-y-4">
      <div>
        <h2 id={id} className="section-title">
          {title}
        </h2>
        <p className="mt-1 max-w-3xl text-sm text-muted">{intro}</p>
      </div>
      {children}
    </section>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="card p-4">
      <h3 className="mb-2 font-sans text-sm font-semibold text-muted">{title}</h3>
      {children}
    </div>
  );
}

function Source({ metric, note }: { metric: AnyMetric | null | undefined; note?: string }) {
  return (
    <div className="mt-2 space-y-1 border-t border-line-subtle pt-2 text-xs text-muted">
      {metric ? <Provenance metric={metric} /> : null}
      {note ? <p>{note}</p> : null}
    </div>
  );
}

/** Day-by-day figures, folded away: the cards and charts above carry the window. */
function Daily({ children }: { children: ReactNode }) {
  return (
    <details className="card p-4">
      <summary className="cursor-pointer text-sm font-semibold text-ink">Daily figures</summary>
      <div className="mt-3">{children}</div>
    </details>
  );
}

function Loads<T>({ loaded, what, children }: { loaded: Loaded<T>; what: string; children: (data: T) => ReactNode }) {
  if (loaded.status === "loading") return <Loading what={what} />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what={what} />;
  return <>{children(loaded.data)}</>;
}

// --- monitoring links ----------------------------------------------------------------

function MonitoringLinks() {
  const links = config().observability ?? { grafanaUrl: "", appInsightsUrl: "" };
  const items = [
    { label: "Managed Grafana", detail: "Platform metrics and the six dashboards", url: links.grafanaUrl },
    { label: "Application Insights", detail: "Traces, logs and failures", url: links.appInsightsUrl },
  ];
  return (
    <ul aria-label="Platform monitoring" className="mb-8 grid gap-3 sm:grid-cols-2">
      {items.map((item) => (
        <li key={item.label} className="card flex items-center justify-between gap-3 p-4">
          <div>
            <p className="text-sm font-semibold text-ink">{item.label}</p>
            <p className="text-xs text-muted">{item.detail}</p>
          </div>
          {item.url ? (
            <a href={item.url} target="_blank" rel="noopener noreferrer" className="link text-sm font-semibold whitespace-nowrap">
              Open {item.label}
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          ) : (
            <HealthBadge band="unknown" label="Not yet applied" />
          )}
        </li>
      ))}
    </ul>
  );
}

// --- usage ---------------------------------------------------------------------------

function Usage({ days, role }: { days: Days; role: string }) {
  const { tenantId } = useCustomer();
  const loaded = useApi(
    (t) => apiGet(t, "/v1/observability/usage", {}, { days: String(days), ...customerQuery(tenantId) }),
    `obs.usage.${days}.${tenantId ?? "all"}`,
  );
  const dayColumns: Column<UsageDay>[] = [
    { key: "day", header: "Day (UTC)", value: (d) => d.day, render: (d) => date(d.day) },
    { key: "users", header: "Active users", value: (d) => n(d.active_users), className: "tabular-nums" },
    { key: "sessions", header: "Sessions (modelled)", value: (d) => n(d.sessions), className: "tabular-nums" },
    { key: "requests", header: "API requests", value: (d) => n(d.requests), className: "tabular-nums" },
    {
      key: "roles",
      header: "By role",
      value: (d) => d.by_role.map((r) => `${r.role} ${r.users.value}`).join(", "),
      sortable: false,
    },
  ];
  const weekColumns: Column<UsageWeek>[] = [
    { key: "week", header: "Week from (Monday)", value: (w) => w.week_start, render: (w) => `${date(w.week_start)}${w.partial ? " (part)" : ""}` },
    { key: "users", header: "Active users", value: (w) => n(w.active_users), className: "tabular-nums" },
    {
      key: "roles",
      header: "By role",
      value: (w) => w.by_role.map((r) => `${r.role} ${r.users.value}`).join(", "),
      sortable: false,
    },
  ];
  return (
    <Section
      id="obs-usage"
      title="Usage"
      intro="Who used the platform, by role, from the action log. The log records API requests, not screens: the most used endpoints stand in for the most used screens. A session is a console page load, so sessions are modelled."
    >
      <Loads loaded={loaded} what="usage">
        {(u) => (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <MetricCard label={`Active users, last ${days} days`} metric={u.active_users} display={(m) => count(n(m))} />
              <MetricCard label="Sessions (console page loads)" metric={u.sessions} display={(m) => count(n(m))} />
              <MetricCard label="API requests" metric={u.requests} display={(m) => count(n(m))} />
              <MetricCard label="Requests refused by the permission matrix" metric={u.blocked} display={(m) => count(n(m))} />
            </div>
            <div className="grid gap-3 lg:grid-cols-2">
              <Panel title="Active users by role">
                <BarTable
                  caption={`Active users by role, last ${days} days`}
                  labelHeader="Role"
                  valueHeader="Users"
                  rows={u.by_role.map((r) => ({ key: r.role, label: r.role, size: n(r.users), text: count(n(r.users)) }))}
                />
                <Source metric={u.active_users} note={`${count(n(u.staff_users))} Getronics staff, ${count(n(u.customer_users))} customer users.`} />
              </Panel>
              <Panel title="Most used API endpoints">
                <BarTable
                  caption={`Most used API endpoints, last ${days} days`}
                  labelHeader="Endpoint"
                  valueHeader="Requests"
                  extra={["Users"]}
                  rows={u.top_endpoints.map((e) => ({
                    key: e.action,
                    label: <code className="text-xs">{e.action}</code>,
                    size: n(e.requests),
                    text: count(n(e.requests)),
                    cells: [count(n(e.users))],
                  }))}
                />
                <Source metric={u.top_endpoints[0]?.requests} />
              </Panel>
            </div>
            <DataTable
              id="observability-usage-weekly"
              caption="Active users by week"
              rows={[...u.weekly].reverse()}
              rowKey={(w) => w.week_start}
              columns={weekColumns}
              role={role}
              empty={{ title: "No activity in this window", next: "Choose a longer window." }}
            />
            <Daily>
              <DataTable
                id="observability-usage-daily"
                caption="Active users by day"
                rows={[...u.daily].reverse()}
                rowKey={(d) => d.day}
                columns={dayColumns}
                role={role}
                empty={{ title: "No activity in this window", next: "Choose a longer window." }}
              />
            </Daily>
          </>
        )}
      </Loads>
    </Section>
  );
}

// --- AI ------------------------------------------------------------------------------

function Ai({ days, role }: { days: Days; role: string }) {
  const { tenantId } = useCustomer();
  const loaded = useApi(
    (t) => apiGet(t, "/v1/observability/ai", {}, { days: String(days), ...customerQuery(tenantId) }),
    `obs.ai.${days}.${tenantId ?? "all"}`,
  );
  const customerColumns: Column<AiCustomer>[] = [
    { key: "tenant", header: "Customer", value: (c) => c.tenant.name },
    { key: "calls", header: `Calls (${days} days)`, value: (c) => n(c.calls), className: "tabular-nums" },
    { key: "tokens", header: `Tokens (${days} days)`, value: (c) => n(c.tokens), render: (c) => count(n(c.tokens)), className: "tabular-nums" },
    {
      key: "quota",
      header: "Month of quota",
      value: (c) => Number(c.quota_used.value),
      render: (c) => (
        <HealthBadge
          band={band(Number(c.quota_used.value) * 100, THRESHOLDS.aiQuotaUsedPercent)}
          label={`${percent(String(c.quota_used.value))} of ${count(n(c.quota))}`}
        />
      ),
    },
    {
      key: "assistant",
      header: "AMARA sub-budget",
      value: (c) => Number(c.assistant_budget_used.value),
      render: (c) => (
        <HealthBadge
          band={band(Number(c.assistant_budget_used.value) * 100, THRESHOLDS.aiQuotaUsedPercent)}
          label={`${percent(String(c.assistant_budget_used.value))} of ${count(n(c.assistant_budget))}`}
        />
      ),
    },
    { key: "refused", header: "Refused", value: (c) => n(c.refused), className: "tabular-nums" },
    { key: "spend", header: "Spend", value: (c) => spendText(c), className: "tabular-nums" },
  ];
  const dayColumns: Column<AiDay>[] = [
    { key: "day", header: "Day (UTC)", value: (d) => d.day, render: (d) => date(d.day) },
    { key: "calls", header: "Calls", value: (d) => n(d.calls), className: "tabular-nums" },
    { key: "tokens", header: "Tokens", value: (d) => n(d.tokens), render: (d) => count(n(d.tokens)), className: "tabular-nums" },
    { key: "refused", header: "Refused", value: (d) => n(d.refused), className: "tabular-nums" },
    { key: "failed", header: "Failed", value: (d) => n(d.failed), className: "tabular-nums" },
    { key: "spend", header: "Spend", value: (d) => spendText(d), className: "tabular-nums" },
  ];
  return (
    <Section
      id="obs-ai"
      title="AI gateway"
      intro="Every model call the gateway made, from its own call records: calls, tokens and spend by agent and customer. Each customer's month is set against the tenant quota and the assistant's sub-budget (40% of the quota)."
    >
      <Loads loaded={loaded} what="AI gateway usage">
        {(a) => (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <MetricCard label={`Calls, last ${days} days`} metric={a.totals.calls} display={(m) => count(n(m))} />
              <MetricCard label="Tokens" metric={a.totals.tokens} display={(m) => count(n(m))} />
              <MetricCard label="Refused by a budget or the breaker" metric={a.totals.refused} display={(m) => count(n(m))} />
              <MetricCard
                label="Spend"
                metric={a.totals.spend?.[0]?.spend}
                display={() => spendText(a.totals)}
                unavailable={a.spend_visible ? "No spend in this window" : "Spend needs cost.read: not available to your role"}
              />
            </div>
            <Panel title="Tokens by agent">
              <BarTable
                caption={`Tokens by agent, last ${days} days`}
                labelHeader="Agent"
                valueHeader="Tokens"
                extra={["Calls", "Spend"]}
                rows={a.by_agent.map((g) => ({
                  key: g.agent,
                  label: AGENT_LABEL[g.agent],
                  size: n(g.tokens),
                  text: count(n(g.tokens)),
                  cells: [count(n(g.calls)), spendText(g)],
                }))}
              />
              <Source
                metric={a.totals.tokens}
                note={`The gateway warns from ${percent(String(a.alert_share.value), 0)} of a budget and refuses at 100%. Spend is Getronics' cost, in each call's currency, never summed across currencies.`}
              />
            </Panel>
            <DataTable
              id="observability-ai-customers"
              caption={`AI usage by customer (month ${a.month})`}
              rows={a.by_customer}
              rowKey={(c) => c.tenant.id}
              columns={customerColumns}
              role={role}
              empty={{ title: "No customers in your scope", next: "Customers appear here once onboarded." }}
            />
            <Daily>
              <DataTable
                id="observability-ai-daily"
                caption="AI gateway calls by day"
                rows={[...a.daily].reverse()}
                rowKey={(d) => d.day}
                columns={dayColumns}
                role={role}
                empty={{ title: "No calls in this window", next: "Choose a longer window." }}
              />
            </Daily>
          </>
        )}
      </Loads>
    </Section>
  );
}

// --- triage --------------------------------------------------------------------------

function Triage({ days, role }: { days: Days; role: string }) {
  const { tenantId } = useCustomer();
  const loaded = useApi(
    (t) => apiGet(t, "/v1/observability/triage", {}, { days: String(days), ...customerQuery(tenantId) }),
    `obs.triage.${days}.${tenantId ?? "all"}`,
  );
  const rate = (r: TriageCustomer | TriageDay) => (r.override_rate ? percent(String(r.override_rate.value)) : "—");
  const columns = <R extends TriageCustomer | TriageDay>(first: Column<R>): Column<R>[] => [
    first,
    { key: "triaged", header: "Triaged", value: (r) => n(r.triaged), className: "tabular-nums" },
    { key: "ready", header: "Ready for planning", value: (r) => n(r.ready_for_planning), className: "tabular-nums" },
    { key: "human", header: "Sent to a person", value: (r) => n(r.human), className: "tabular-nums" },
    { key: "accepted", header: "Reviews accepted", value: (r) => n(r.accepted), className: "tabular-nums" },
    { key: "overridden", header: "Reviews overridden", value: (r) => n(r.overridden), className: "tabular-nums" },
    { key: "rate", header: "Override rate", value: (r) => (r.override_rate ? Number(r.override_rate.value) : null), render: rate, className: "tabular-nums" },
  ];
  return (
    <Section
      id="obs-triage"
      title="Triage throughput"
      intro="Tickets the triage pipeline classified, where it sent them, and what reviewers made of it. An override is a person correcting the agents."
    >
      <Loads loaded={loaded} what="triage throughput">
        {(t) => (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <MetricCard label={`Triaged, last ${days} days`} metric={t.totals.triaged} display={(m) => count(n(m))} />
              <MetricCard label="Ready for planning" metric={t.totals.ready_for_planning} display={(m) => count(n(m))} />
              <MetricCard label="Sent to a person" metric={t.totals.human} display={(m) => count(n(m))} href="/work" />
              <MetricCard
                label="Reviews overridden"
                metric={t.totals.override_rate}
                display={(m) => `${percent(String(m.value))} (${count(n(t.totals.overridden))} of ${count(n(t.totals.overridden) + n(t.totals.accepted))})`}
                unavailable="No reviews in this window"
              />
            </div>
            <DataTable
              id="observability-triage-customers"
              caption="Triage by customer"
              rows={t.by_customer}
              rowKey={(c) => c.tenant.id}
              columns={columns<TriageCustomer>({ key: "tenant", header: "Customer", value: (c) => c.tenant.name })}
              role={role}
              empty={{ title: "No customers in your scope", next: "Customers appear here once onboarded." }}
            />
            <Daily>
              <DataTable
                id="observability-triage-daily"
                caption="Triage by day"
                rows={[...t.daily].reverse()}
                rowKey={(d) => d.day}
                columns={columns<TriageDay>({ key: "day", header: "Day (UTC)", value: (d) => d.day, render: (d) => date(d.day) })}
                role={role}
                empty={{ title: "No triage in this window", next: "Choose a longer window." }}
              />
            </Daily>
          </>
        )}
      </Loads>
    </Section>
  );
}

// --- connectors ----------------------------------------------------------------------

function Connectors() {
  const loaded = useApi((t) => apiGet(t, "/v1/observability/connectors"), "obs.connectors");
  return (
    <Section
      id="obs-connectors"
      title="Connectors"
      intro={
        <>
          Health per source, from the platform's run log: the worst of its entities, for every customer whichever is selected. The{" "}
          <Link href="/connectors" className="link">
            Connectors page
          </Link>{" "}
          has each entity, its reasons and schema drift.
        </>
      }
    >
      <Loads loaded={loaded} what="connector health">
        {(c) =>
          !c.visible ? (
            <p className="card p-4 text-sm text-muted">Connector health needs connectors.read, which your role does not hold.</p>
          ) : (
            <Panel title="Connector health by source">
              <ConnectorTable sources={c.sources} />
              <Source metric={c.sources[0]?.runs_24h} note={`As of ${dateTime(c.as_of)}. Run figures cover the last 24 hours.`} />
            </Panel>
          )
        }
      </Loads>
    </Section>
  );
}

function ConnectorTable({ sources }: { sources: SourceHealth[] }) {
  if (!sources.length) return <p className="text-sm text-muted">No connector is scheduled yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">Connector health by source</caption>
        <thead>
          <tr className="text-xs text-muted">
            {["Source", "Health", "Last success", "Failed runs (24 h)", "Quarantine", "Open drift"].map((h) => (
              <th key={h} scope="col" className="pb-1 pr-3 font-semibold">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sources.map((s) => (
            <tr key={s.source_system} className="border-t border-line-subtle">
              <th scope="row" className="py-1.5 pr-3 font-normal text-ink">
                {s.source_system} <span className="text-xs text-muted">({count(n(s.entities))})</span>
              </th>
              <td className="py-1.5 pr-3">
                <HealthBadge band={s.band} />
              </td>
              <td className="py-1.5 pr-3 whitespace-nowrap">{s.last_success_at ? dateTime(s.last_success_at) : "Never"}</td>
              <td className="py-1.5 pr-3 tabular-nums">
                {count(n(s.failed_runs_24h))} of {count(n(s.runs_24h))}
                {s.failed_run_rate_24h ? ` (${percent(String(s.failed_run_rate_24h.value))})` : ""}
              </td>
              <td className="py-1.5 pr-3 tabular-nums">{count(n(s.quarantine_depth))}</td>
              <td className="py-1.5 pr-3 tabular-nums">{count(n(s.drift_open))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- platform cost -------------------------------------------------------------------

function PlatformCostSection() {
  const { tenantId } = useCustomer();
  const loaded = useApi((t) => apiGet(t, "/v1/observability/platform-cost", {}, customerQuery(tenantId)), `obs.platform-cost.${tenantId ?? "all"}`);
  return (
    <Section
      id="obs-cost"
      title="Platform cost"
      intro="What the platform's own resources cost in the subscription that hosts it, from the host's live cost feed. Amounts stay in their billing currency."
    >
      <Loads loaded={loaded} what="platform cost">
        {(p) => <PlatformCostBody cost={p} />}
      </Loads>
    </Section>
  );
}

function PlatformCostBody({ cost }: { cost: PlatformCost }) {
  if (!cost.visible) return <p className="card p-4 text-sm text-muted">Platform cost needs cost.read, which your role does not hold.</p>;
  if (!cost.host) return <p className="card p-4 text-sm text-muted">No tenant in this view hosts the platform, so there is no platform cost to show.</p>;
  if (!cost.currencies.length) {
    return <p className="card p-4 text-sm text-muted">No cost has arrived yet from {cost.host.name}&apos;s feed for the platform&apos;s resources.</p>;
  }
  return (
    <Panel title={`Hosted in ${cost.host.name}`}>
      {cost.currencies.map((c) => (
        <BarTable
          key={c.currency}
          caption={`Platform cost by month, ${c.currency}`}
          hideCaption={cost.currencies.length < 2}
          labelHeader="Month"
          valueHeader={`Amortised (${c.currency})`}
          rows={c.points.map((p) => ({
            key: p.period,
            label: `${p.period}${p.month_to_date ? " (to date)" : ""}`,
            size: size(p.amortized ? String(p.amortized.value) : null),
            text: p.amortized ? money(String(p.amortized.value), c.currency, 2) : "—",
          }))}
        />
      ))}
      <Source
        metric={cost.currencies[0]?.points.findLast((p) => p.amortized)?.amortized}
        note={
          cost.captured
            ? "Lines of the platform's resource groups only (rg-cloudops-*); the rest of the host's subscription is not the platform."
            : "This month's feed has no resource-level lines, so the platform's part is not yet captured."
        }
      />
    </Panel>
  );
}

// --- the page ------------------------------------------------------------------------

export function Observability() {
  const me = useMe();
  const [days, setDays] = useState<Days>(30);
  return (
    <>
      <PageHeader title="Observability" subtitle="How the platform itself is used and behaving: usage, AI, triage, connectors and its own cost.">
        <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
          Window
          <select className="field" value={days} onChange={(e) => setDays(Number(e.target.value) as Days)}>
            {WINDOWS.map((w) => (
              <option key={w} value={w}>
                Last {w} days
              </option>
            ))}
          </select>
        </label>
      </PageHeader>
      <MonitoringLinks />
      <Usage days={days} role={me.role} />
      <Ai days={days} role={me.role} />
      <Triage days={days} role={me.role} />
      <Connectors />
      <PlatformCostSection />
    </>
  );
}
