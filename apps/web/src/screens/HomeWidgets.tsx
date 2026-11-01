"use client";

// The home dashboard's widgets (ADR 0030). Each reads one endpoint (a home summary,
// or the module's own where it already answers), shows every figure with its
// source and as-of time, links to the screen holding the evidence, and says "not
// available" or "not captured" rather than drawing a zero. Money stays a decimal
// string, in its own currency, rounded once here.

import Link from "next/link";

import type {
  ClosestItem,
  ConnectorHealth,
  CustomerCostHome,
  MarginView,
  MetricDecimal,
  OperationsHome,
  TenantBacklog,
  WorkgroupBacklog,
} from "@cloudops/contracts";

import { BarTable, type Bar } from "@/components/BarTable";
import { HealthBadge } from "@/components/HealthBadge";
import { MetricCard } from "@/components/MetricCard";
import { Body, Frame, Gap, Source, Stat, useShared } from "@/components/Widget";
import { apiGet } from "@/lib/api";
import { notCapturedReason, periodLabel, statusLabel } from "@/lib/commercial";
import { WIDGETS, type WidgetId } from "@/lib/dashboard";
import { count, date, dateTime, decimal, money, month, percent } from "@/lib/format";
import { band, BAND_LABEL, marginBand, priorityBand, THRESHOLDS, type Band } from "@/lib/health";
import { useMe } from "@/lib/session";

const SHORT_MONTH = new Intl.DateTimeFormat("en-GB", { month: "short", year: "numeric", timeZone: "UTC" });
const ORDER: Record<Band, number> = { critical: 0, "at-risk": 1, watch: 2, healthy: 3, unknown: 4 };

function n(metric: { value: number | string }): string {
  return count(Number(metric.value));
}

function hours(value: string): string {
  return `${decimal(value, 1)} h`;
}

/** Drawing only: a bar's length from a decimal string. Text never goes through this. */
function size(value: string | null | undefined): number {
  const x = Number(value ?? 0);
  return Number.isFinite(x) && x > 0 ? x : 0;
}

function useOperations() {
  return useShared("home.operations", (t) => apiGet(t, "/v1/home/operations"));
}

function useCost() {
  return useShared("home.cost", (t) => apiGet(t, "/v1/home/cost"));
}

function frame(id: WidgetId) {
  return { id, title: WIDGETS[id].title, wide: WIDGETS[id].wide ?? false };
}

// --- tickets -------------------------------------------------------------------------

const PRIORITY_LABEL: Record<string, string> = { "1": "P1", "2": "P2", "3": "P3", "4": "P4", "5": "P5", none: "No priority" };

function atRiskNote(ops: OperationsHome) {
  const targets = ops.by_priority
    .filter((p) => p.target_hours)
    .map((p) => `${PRIORITY_LABEL[p.priority] ?? p.priority} ${count(Number(p.target_hours?.value))} h`)
    .join(", ");
  return (
    <>
      <strong className="font-semibold text-modelled">Modelled:</strong> at risk is an open incident that ServiceNow has not flagged, with at least{" "}
      {percent(String(ops.at_risk_fraction.value), 0)} of its priority&rsquo;s resolution target elapsed ({targets}; platform policy, wall-clock time).
    </>
  );
}

export function IncidentsWidget() {
  const loaded = useOperations();
  return (
    <Frame {...frame("incidents")} href="/work?kind=Incident">
      <Body loaded={loaded} what="incidents">
        {(ops) => (
          <>
            <div className="mb-4 grid gap-3 sm:grid-cols-3">
              <MetricCard
                label="Open incidents"
                metric={ops.open_incidents}
                display={n}
                band={band(ops.open_incidents.value, THRESHOLDS.openIncidents)}
                href="/work?kind=Incident"
              />
              <MetricCard
                label="SLA breached, open"
                metric={ops.breached_open}
                display={n}
                band={band(ops.breached_open.value, THRESHOLDS.slaBreachedOpen)}
                href="/work?sla=Breached"
              />
              <MetricCard
                label="SLA at risk"
                metric={ops.at_risk}
                display={n}
                band={band(ops.at_risk.value, THRESHOLDS.slaBreachedOpen)}
                href="/work?kind=Incident"
              />
            </div>
            <BarTable
              caption="Open incidents by priority, with SLA breached and at risk"
              labelHeader="Priority"
              valueHeader="Open"
              extra={["Breached", "At risk"]}
              rows={ops.by_priority.map((p) => ({
                key: p.priority,
                label: (
                  <>
                    {PRIORITY_LABEL[p.priority] ?? p.priority}
                    {p.target_hours ? <span className="ml-1 text-xs text-muted">({count(Number(p.target_hours.value))} h)</span> : null}
                  </>
                ),
                size: Number(p.open.value),
                text: n(p.open),
                band: priorityBand(p.priority),
                title: `${PRIORITY_LABEL[p.priority] ?? p.priority}: ${n(p.open)} open`,
                cells: [
                  n(p.breached),
                  p.at_risk ? (
                    n(p.at_risk)
                  ) : (
                    <span className="text-muted">
                      —<span className="sr-only">no target</span>
                    </span>
                  ),
                ],
              }))}
            />
            <Source metric={ops.open_incidents} note={atRiskNote(ops)} />
          </>
        )}
      </Body>
    </Frame>
  );
}

const RISK: Record<ClosestItem["risk"], { band: Band; label: string }> = {
  breached: { band: "critical", label: "Breached" },
  at_risk: { band: "at-risk", label: "At risk (modelled)" },
  within: { band: "healthy", label: "Within target" },
  no_target: { band: "unknown", label: "No target" },
};

export function ClosestWidget() {
  const loaded = useOperations();
  const me = useMe();
  return (
    <Frame {...frame("closest")} href="/work?kind=Incident">
      <Body loaded={loaded} what="incidents">
        {(ops) =>
          ops.closest.length === 0 ? (
            <Gap text="No open incidents." />
          ) : (
            <>
              <ul className="divide-y divide-line-subtle text-sm">
                {ops.closest.map((i) => (
                  <li key={`${i.tenant.id}-${i.number}`} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <span className="min-w-0">
                      <Link href={`/work/${encodeURIComponent(i.number)}`} className="link font-medium">
                        {i.number}
                      </Link>
                      <span className="ml-2 text-muted">
                        {i.priority ?? "No priority"}
                        {me.tenants.length > 1 ? ` · ${i.tenant.name}` : ""}
                      </span>
                      <span className="block truncate text-xs text-muted">
                        {hours(String(i.elapsed_hours.value))} open{i.target_hours ? ` of a ${count(Number(i.target_hours.value))} h target` : ""}
                      </span>
                    </span>
                    <HealthBadge band={RISK[i.risk].band} label={RISK[i.risk].label} />
                  </li>
                ))}
              </ul>
              <Source metric={ops.closest[0]?.elapsed_hours} note="Breached first, then the largest share of the modelled target used." />
            </>
          )
        }
      </Body>
    </Frame>
  );
}

export function TriageWidget() {
  const loaded = useShared("home.triage", (t) => apiGet(t, "/v1/home/triage"));
  return (
    <Frame {...frame("triage")} href="/work">
      <Body loaded={loaded} what="the triage queue">
        {(q) => (
          <>
            <div className="mb-3 grid grid-cols-3 gap-2">
              <Stat label="Awaiting review" value={n(q.awaiting)} />
              <Stat label="Routed to a person" value={n(q.needs_person)} />
              <Stat label={`Below ${percent(String(q.threshold.value), 0)} confidence`} value={n(q.low_confidence)} />
            </div>
            {q.items.length === 0 ? (
              <Gap text="Nothing awaits review." />
            ) : (
              <BarTable
                caption="Triage awaiting review, least confident first"
                labelHeader="Ticket"
                valueHeader="Confidence"
                extra={["Route"]}
                rows={q.items.map((i) => ({
                  key: `${i.tenant.id}-${i.number}`,
                  label: (
                    <Link href={`/work/${encodeURIComponent(i.number)}`} className="link font-medium">
                      {i.number}
                    </Link>
                  ),
                  size: size(i.confidence ? String(i.confidence.value) : null),
                  text: i.confidence ? percent(String(i.confidence.value), 0) : "None",
                  band: i.below_threshold ? "at-risk" : "healthy",
                  title: `${i.number}: ${i.intent ?? "no intent"}${i.below_threshold ? ", below the threshold" : ""}`,
                  cells: [i.route === "human" ? "Person" : "Planning"],
                }))}
              />
            )}
            <Source metric={q.awaiting} note="The classifier's own confidence; a person reviews before anything is planned." />
          </>
        )}
      </Body>
    </Frame>
  );
}

export function ChangesWidget() {
  const loaded = useOperations();
  return (
    <Frame {...frame("changes")} href="/work?kind=Change+task">
      <Body loaded={loaded} what="changes">
        {(ops) => (
          <>
            <div className="mb-3 grid grid-cols-3 gap-2">
              <Stat label="Opened, 7 days" value={n(ops.changes.opened)} />
              <Stat label="Closed, 7 days" value={n(ops.changes.closed)} />
              <Stat label="Open now" value={n(ops.changes.open_now)} />
            </div>
            {ops.changes.recent.length === 0 ? (
              <Gap text="No changes opened or closed in the last seven days." />
            ) : (
              <ul className="divide-y divide-line-subtle text-sm">
                {ops.changes.recent.map((c) => (
                  <li key={`${c.tenant.id}-${c.number}`} className="py-2">
                    <Link href={`/work/${encodeURIComponent(c.number)}`} className="link font-medium">
                      {c.number}
                    </Link>
                    <span className="ml-2 text-xs text-muted">
                      {c.closed_at ? `closed ${date(c.closed_at)}` : `opened ${date(c.opened_at)}`}
                    </span>
                    <span className="block truncate text-xs text-muted">{c.short_description ?? c.configuration_item ?? ""}</span>
                  </li>
                ))}
              </ul>
            )}
            <Source metric={ops.changes.opened} />
          </>
        )}
      </Body>
    </Frame>
  );
}

export function NoisyWidget() {
  const loaded = useOperations();
  return (
    <Frame {...frame("noisy")} href="/resources">
      <Body loaded={loaded} what="noisy configuration items">
        {(ops) =>
          ops.noisy_cis.length === 0 ? (
            <Gap text="No incidents named a configuration item in the last 30 days." />
          ) : (
            <>
              <BarTable
                caption="Incidents per configuration item, last 30 days"
                labelHeader="Configuration item"
                valueHeader="Incidents"
                rows={ops.noisy_cis.map((c) => ({
                  key: `${c.tenant.id}-${c.configuration_item}`,
                  label: c.resource_id ? (
                    <Link href={`/resources/${c.resource_id}`} className="link">
                      {c.configuration_item}
                    </Link>
                  ) : (
                    c.configuration_item
                  ),
                  size: Number(c.incidents.value),
                  text: n(c.incidents),
                }))}
              />
              <Source metric={ops.noisy_cis[0]?.incidents} note="Incidents opened in the last 30 days." />
            </>
          )
        }
      </Body>
    </Frame>
  );
}

export function SlaTrendWidget() {
  const loaded = useShared("performance", (t) => apiGet(t, "/v1/performance"));
  return (
    <Frame {...frame("sla_trend")} href="/performance">
      <Body loaded={loaded} what="SLA attainment">
        {(p) => (
          <>
            <BarTable
              caption="Incidents resolved within SLA, by month"
              labelHeader="Month"
              valueHeader="Within SLA"
              extra={["Resolved"]}
              rows={[...p.months].reverse().map((m) => ({
                key: m.month,
                label: month(m.month),
                size: size(m.sla_attainment ? String(m.sla_attainment.value) : null),
                text: m.sla_attainment ? `${decimal(String(m.sla_attainment.value), 1)}%` : "—",
                title: m.sla_attainment ? undefined : "No incident resolved that month",
                cells: [n(m.incidents_resolved)],
              }))}
            />
            <Source metric={p.months.at(-1)?.incidents_resolved} note="A month with no incident resolved shows a dash, not 0%." />
          </>
        )}
      </Body>
    </Frame>
  );
}

function backlogRows<T extends TenantBacklog | WorkgroupBacklog>(rows: T[], label: (r: T) => React.ReactNode, key: (r: T) => string): Bar[] {
  return rows.map((r) => ({
    key: key(r),
    label: label(r),
    size: Number(r.open_items.value),
    text: n(r.open_items),
    band: band(r.breached_open.value, THRESHOLDS.slaBreachedOpen),
    cells: [n(r.breached_open), r.mttr_hours ? hours(String(r.mttr_hours.value)) : "—"],
  }));
}

export function BacklogCustomerWidget() {
  const loaded = useOperations();
  return (
    <Frame {...frame("backlog_customer")} href="/customers">
      <Body loaded={loaded} what="the backlog">
        {(ops) => (
          <>
            <BarTable
              caption="Open work items, open SLA breaches and mean time to resolve incidents (30 days), by customer"
              labelHeader="Customer"
              valueHeader="Open"
              extra={["Breached", "MTTR"]}
              rows={backlogRows(
                [...ops.by_customer].sort((a, b) => Number(b.open_items.value) - Number(a.open_items.value)),
                (r) => r.tenant.name,
                (r) => r.tenant.id,
              )}
            />
            <Source metric={ops.by_customer[0]?.open_items} note="MTTR: mean resolve time of incidents closed in the last 30 days; a dash where none closed." />
          </>
        )}
      </Body>
    </Frame>
  );
}

export function BacklogWorkgroupWidget() {
  const loaded = useOperations();
  return (
    <Frame {...frame("backlog_workgroup")} href="/work">
      <Body loaded={loaded} what="the backlog">
        {(ops) =>
          !ops.by_workgroup || ops.by_workgroup.length === 0 ? (
            <Gap text="No open work items." />
          ) : (
            <>
              <BarTable
                caption="Open work items, open SLA breaches and MTTR (30 days), by workgroup"
                labelHeader="Workgroup"
                valueHeader="Open"
                extra={["Breached", "MTTR"]}
                rows={backlogRows(
                  ops.by_workgroup.slice(0, 8),
                  (r) => r.workgroup ?? "Unassigned",
                  (r) => r.workgroup ?? "",
                )}
              />
              <Source metric={ops.by_workgroup[0]?.open_items} />
            </>
          )
        }
      </Body>
    </Frame>
  );
}

export function CsatWidget() {
  return (
    <Frame {...frame("csat")}>
      <Gap text="Not captured: no customer satisfaction survey source is connected." />
    </Frame>
  );
}

// --- platform ------------------------------------------------------------------------

function rate(value: string | undefined): string {
  return value === undefined ? "—" : percent(value, 0);
}

export function ConnectorsWidget() {
  const loaded = useShared("connectors.health", (t) => apiGet(t, "/v1/connectors/health"));
  return (
    <Frame {...frame("connectors")} href="/connectors">
      <Body loaded={loaded} what="connector health">
        {(h) => {
          const worst = [...h.items].sort((a, b) => ORDER[a.band] - ORDER[b.band]).slice(0, 6);
          return (
            <>
              <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat label="Outage" value={n(h.outages)} />
                <Stat label="Late" value={n(h.late)} />
                <Stat label="Needs attention" value={n(h.attention)} />
                <Stat label="Never run" value={n(h.never_run)} />
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[34rem] text-sm">
                  <caption className="sr-only">The six connectors in the worst health</caption>
                  <thead>
                    <tr className="text-left text-xs text-muted">
                      <th scope="col" className="pb-1 font-semibold">Connector</th>
                      <th scope="col" className="pb-1 font-semibold">Health</th>
                      <th scope="col" className="pb-1 font-semibold">Last success</th>
                      <th scope="col" className="pb-1 text-right font-semibold">Failed runs, 24 h</th>
                      <th scope="col" className="pb-1 text-right font-semibold">Quarantine</th>
                      <th scope="col" className="pb-1 text-right font-semibold">Drift</th>
                    </tr>
                  </thead>
                  <tbody>
                    {worst.map((c: ConnectorHealth) => (
                      <tr key={`${c.source_system}-${c.entity}`} className="border-t border-line-subtle">
                        <th scope="row" className="py-1.5 pr-2 text-left font-normal">
                          {c.source_system} · {c.entity}
                        </th>
                        <td className="py-1.5">
                          <HealthBadge band={c.band} />
                        </td>
                        <td className="py-1.5 text-xs">{c.last_success_at ? dateTime(c.last_success_at) : "Never"}</td>
                        <td className="py-1.5 text-right tabular-nums">{rate(c.failed_run_rate_24h ? String(c.failed_run_rate_24h.value) : undefined)}</td>
                        <td className="py-1.5 text-right tabular-nums">
                          {count(Number(c.alias_quarantine_depth.value) + Number(c.resource_quarantine_depth.value))}
                        </td>
                        <td className="py-1.5 text-right tabular-nums">{n(c.drift_open)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Source metric={h.outages} note="Quarantine: names and resources held for a person. Drift: schema changes not yet acknowledged." />
            </>
          );
        }}
      </Body>
    </Frame>
  );
}

export function MappingWidget() {
  const quarantine = useShared("mapping.quarantine", (t) => apiGet(t, "/v1/mapping/quarantine"));
  const proposals = useShared("mapping.proposals", (t) => apiGet(t, "/v1/mapping/proposals"));
  return (
    <Frame {...frame("mapping")} href="/mapping">
      <Body loaded={quarantine} what="the mapping queue">
        {(q) => (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <MetricCard label="Names in quarantine" metric={q.names} display={n} href="/mapping" />
              <MetricCard label="Sightings held" metric={q.sightings} display={n} href="/mapping" />
            </div>
            <div className="mt-3">
              <Body loaded={proposals} what="proposals">
                {(p) => {
                  const pending = p.filter((x) => x.decision === null);
                  const mine = pending.filter((x) => x.may_approve).length;
                  return (
                    <p className="text-sm text-ink">
                      {count(pending.length)} mapping proposal{pending.length === 1 ? "" : "s"} awaiting a decision
                      {mine > 0 ? `, ${count(mine)} you may approve` : ""}.
                    </p>
                  );
                }}
              </Body>
            </div>
          </>
        )}
      </Body>
    </Frame>
  );
}

const ONBOARDING_LABEL = { live: "Live", setup: "In setup", not_started: "Not started", suspended: "Suspended" } as const;

export function OnboardingWidget() {
  const loaded = useShared("onboarding", (t) => apiGet(t, "/v1/onboarding"));
  return (
    <Frame {...frame("onboarding")} href="/onboarding">
      <Body loaded={loaded} what="onboarding">
        {(rows) => (
          <>
            <BarTable
              caption="Customers by portal onboarding status"
              labelHeader="Status"
              valueHeader="Customers"
              rows={(Object.keys(ONBOARDING_LABEL) as (keyof typeof ONBOARDING_LABEL)[]).map((s) => {
                const k = rows.filter((r) => r.status === s).length;
                return { key: s, label: ONBOARDING_LABEL[s], size: k, text: count(k) };
              })}
            />
            <p className="mt-3 text-xs text-muted">Source: the platform&rsquo;s onboarding record, as of this page&rsquo;s load.</p>
          </>
        )}
      </Body>
    </Frame>
  );
}

export function AiUsageWidget() {
  const loaded = useShared("home.ai", (t) => apiGet(t, "/v1/home/ai-usage"));
  return (
    <Frame {...frame("ai_usage")}>
      <Body loaded={loaded} what="AI gateway usage">
        {(u) => {
          const top = [...u.tenants].sort((a, b) => size(String(b.used_share.value)) - size(String(a.used_share.value))).slice(0, 6);
          return (
            <>
              <BarTable
                caption={`AI tokens used this month (${u.month}) against each tenant's quota`}
                labelHeader="Tenant"
                valueHeader="Of quota"
                extra={["Spend"]}
                rows={top.map((t) => {
                  const pct = Number(t.used_share.value) * 100;
                  return {
                    key: t.tenant.id,
                    label: t.tenant.name,
                    size: size(String(t.used_share.value)),
                    text: percent(String(t.used_share.value), 1),
                    band: band(pct, THRESHOLDS.aiQuotaUsedPercent),
                    title: `${count(Number(t.tokens.value))} of ${count(Number(t.quota.value))} tokens, ${count(Number(t.refused.value))} refused`,
                    cells: [t.spend.length ? t.spend.map((s) => money(String(s.spend.value), s.spend.unit, 2)).join(", ") : "—"],
                  };
                })}
              />
              <Source
                metric={u.tenants[0]?.tokens}
                note={`The gateway warns from ${percent(String(u.alert_share.value), 0)} of a tenant's monthly tokens and refuses at 100%. Spend is Getronics' cost.`}
              />
            </>
          );
        }}
      </Body>
    </Frame>
  );
}

// --- cost and commercial -------------------------------------------------------------

function points(c: CustomerCostHome, currency: string) {
  return c.currencies.find((t) => t.currency === currency)?.points ?? [];
}

/** Customer cost rows: the last complete month, with month to date beside it. */
function costRows(customers: CustomerCostHome[]): Bar[] {
  return customers.flatMap((c) =>
    c.currencies.map((t) => {
      const pts = points(c, t.currency);
      const last = pts.at(-2)?.amortized;
      const mtd = pts.at(-1)?.amortized;
      return {
        key: `${c.tenant.id}-${t.currency}`,
        label: (
          <Link href={`/finops/${c.tenant.id}`} className="link">
            {c.tenant.name} <span className="text-xs text-muted">({t.currency})</span>
          </Link>
        ),
        size: size(last ? String(last.value) : null),
        text: last ? decimal(String(last.value), 0) : "—",
        title: pts.map((p) => `${periodLabel(p.period)}: ${p.amortized ? money(String(p.amortized.value), p.amortized.unit, 0) : "none"}`).join("; "),
        cells: [mtd ? decimal(String(mtd.value), 0) : "—"],
      };
    }),
  );
}

export function CostTrendWidget() {
  const loaded = useCost();
  return (
    <Frame {...frame("cost_trend")} href="/finops">
      <Body loaded={loaded} what="cost">
        {(cost) => {
          const customers = cost.customers.filter((c) => !c.internal && c.currencies.length > 0);
          const previous = cost.months.at(-2) ?? "";
          const first = customers[0]?.currencies[0]?.points.at(-2)?.amortized;
          return customers.length === 0 ? (
            <Gap text="No cloud cost in the last six months." />
          ) : (
            <>
              <BarTable
                caption={`Cloud cost by customer and currency, ${periodLabel(previous)}, with this month to date`}
                labelHeader="Customer"
                valueHeader={periodLabel(previous)}
                extra={["Month to date"]}
                rows={costRows(customers)}
              />
              <Source
                metric={first as MetricDecimal | undefined}
                note="Getronics' amortised cost, in each transaction currency, never summed across currencies. Hover a row for its six months."
              />
            </>
          );
        }}
      </Body>
    </Frame>
  );
}

export function AnomaliesWidget() {
  const loaded = useCost();
  return (
    <Frame {...frame("anomalies")} href="/finops">
      <Body loaded={loaded} what="anomalies">
        {(cost) => {
          const rows = cost.customers.filter((c) => Number(c.anomalies_30d.value) > 0);
          return rows.length === 0 ? (
            <Gap text="No consumption anomalies in the last 30 days." />
          ) : (
            <>
              <BarTable
                caption="Consumption anomalies per customer, last 30 days"
                labelHeader="Customer"
                valueHeader="Anomalies"
                extra={["Latest"]}
                rows={rows.map((c) => ({
                  key: c.tenant.id,
                  label: (
                    <Link href={`/finops/${c.tenant.id}`} className="link">
                      {c.tenant.name}
                    </Link>
                  ),
                  size: Number(c.anomalies_30d.value),
                  text: n(c.anomalies_30d),
                  cells: [date(c.last_anomaly_on)],
                }))}
              />
              <Source metric={rows[0]?.anomalies_30d} note="Spikes and drops in consumption against its baseline." />
            </>
          );
        }}
      </Body>
    </Frame>
  );
}

const RECON: Record<CustomerCostHome["reconciliation"][number]["state"], { band: Band; label: string }> = {
  final: { band: "healthy", label: "Final" },
  provisional: { band: "watch", label: "Provisional" },
  unverified: { band: "critical", label: "Unverified" },
  unchecked: { band: "at-risk", label: "Not checked" },
  no_cost: { band: "unknown", label: "No cost" },
};

export function ReconciliationWidget() {
  const loaded = useCost();
  return (
    <Frame {...frame("reconciliation")} href="/finops">
      <Body loaded={loaded} what="reconciliation">
        {(cost) => {
          const months = cost.customers[0]?.reconciliation.map((r) => r.period) ?? [];
          return (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <caption className="sr-only">Reconciliation of each customer&rsquo;s cost, by month</caption>
                  <thead>
                    <tr className="text-left text-xs text-muted">
                      <th scope="col" className="pb-1 font-semibold">Customer</th>
                      {months.map((m) => (
                        <th key={m} scope="col" className="pb-1 font-semibold">
                          {periodLabel(m)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {cost.customers.map((c) => (
                      <tr key={c.tenant.id} className="border-t border-line-subtle">
                        <th scope="row" className="py-1.5 pr-2 text-left font-normal">
                          {c.tenant.name}
                          {c.internal ? <span className="ml-1 text-xs text-muted">(platform)</span> : null}
                        </th>
                        {c.reconciliation.map((r) => (
                          <td key={r.period} className="py-1.5">
                            <HealthBadge band={RECON[r.state].band} label={RECON[r.state].label} />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Source
                metric={cost.customers[0]?.reconciliation.at(-1)?.verified}
                note="Final: checked against the invoice. Provisional: matches the provider's running total, no invoice yet. Not checked: an account with cost has no check."
              />
            </>
          );
        }}
      </Body>
    </Frame>
  );
}

function marginText(m: MarginView): string {
  return m.margin_pct ? percent(String(m.margin_pct.value)) : "—";
}

function marginBandCell(m: MarginView) {
  if (m.not_captured) return <span className="text-xs whitespace-normal text-muted">Not captured: {notCapturedReason(m.not_captured)}</span>;
  if (m.band) return <HealthBadge band={marginBand(m.band)} />;
  return <span className="text-xs text-muted">{statusLabel(m) || BAND_LABEL.unknown}</span>;
}

export function MarginWidget() {
  const loaded = useShared("margin", (t) => apiGet(t, "/v1/commercial/margin"));
  return (
    <Frame {...frame("margin")} href="/margin">
      <Body loaded={loaded} what="margin">
        {(o) => (
          <>
            <BarTable
              caption={`Margin by customer, ${periodLabel(o.period)}, banded against each SOW's target`}
              labelHeader="Customer"
              valueHeader="Margin"
              extra={["Target", "Band"]}
              rows={o.customers.map((m) => {
                const b = m.band ? marginBand(m.band) : "unknown";
                return {
                  key: m.tenant.id,
                  label: (
                    <Link href={`/margin/${m.tenant.id}`} className="link">
                      {m.tenant.name}
                    </Link>
                  ),
                  size: size(m.margin_pct ? String(m.margin_pct.value) : null),
                  text: marginText(m),
                  band: m.band ? b : undefined,
                  cells: [m.target_margin ? percent(String(m.target_margin.value), 0) : "—", marginBandCell(m)],
                };
              })}
            />
            <Source
              metric={o.customers.find((m) => m.margin_pct)?.margin_pct}
              note="In each contract currency's terms. A provisional month is not banded until it closes."
            />
          </>
        )}
      </Body>
    </Frame>
  );
}

export function PlatformCostWidget() {
  const loaded = useCost();
  return (
    <Frame {...frame("platform_cost")} href="/finops">
      <Body loaded={loaded} what="platform cost">
        {(cost) => {
          const internal = cost.customers.find((c) => c.internal);
          if (!internal) return <Gap text="Not captured: no tenant is marked as the platform's own (ADR 0023)." />;
          if (internal.currencies.length === 0) return <Gap text="No platform cost in the last six months." />;
          return (
            <div className="grid gap-3">
              {internal.currencies.map((t) => {
                const pts = t.points;
                const last = pts.at(-2);
                const mtd = pts.at(-1);
                return (
                  <div key={t.currency} className="grid gap-3 sm:grid-cols-2">
                    <MetricCard
                      label={`${last ? periodLabel(last.period) : "Last month"} (${t.currency})`}
                      metric={last?.amortized}
                      display={(m) => money(String(m.value), m.unit, 0)}
                      href={`/finops/${internal.tenant.id}`}
                      unavailable="No cost that month"
                    />
                    <MetricCard
                      label={`Month to date (${t.currency})`}
                      metric={mtd?.amortized}
                      display={(m) => money(String(m.value), m.unit, 0)}
                      href={`/finops/${internal.tenant.id}`}
                      unavailable="No cost yet this month"
                    />
                  </div>
                );
              })}
            </div>
          );
        }}
      </Body>
    </Frame>
  );
}

// --- the customer's own view ---------------------------------------------------------

export function EstateWidget() {
  const loaded = useShared("customers", (t) => apiGet(t, "/v1/customers"));
  return (
    <Frame {...frame("estate")} href="/customers">
      <Body loaded={loaded} what="your service">
        {(rows) => {
          const c = rows[0];
          if (!c) return <Gap text="No service is set up for your organisation yet." />;
          return (
            <div className="grid gap-3 sm:grid-cols-2">
              <MetricCard label="Resources" metric={c.resources} display={n} href="/resources" />
              <MetricCard
                label="Open incidents"
                metric={c.open_incidents}
                display={n}
                band={band(c.open_incidents?.value, THRESHOLDS.openIncidents)}
                href="/work?kind=Incident"
              />
              <MetricCard
                label="SLA breached, open"
                metric={c.sla_breached_open}
                display={n}
                band={band(c.sla_breached_open?.value, THRESHOLDS.slaBreachedOpen)}
                href="/work?sla=Breached"
              />
              <MetricCard
                label="Critical vulnerabilities"
                metric={c.critical_vulnerabilities}
                display={n}
                band={band(c.critical_vulnerabilities?.value, THRESHOLDS.criticalVulnerabilities)}
                href="/posture"
              />
            </div>
          );
        }}
      </Body>
    </Frame>
  );
}

export function PriceWidget() {
  const loaded = useShared("price", (t) => apiGet(t, "/v1/cost/summary", {}, { months: "6" }));
  return (
    <Frame {...frame("price")} href="/customers">
      <Body loaded={loaded} what="cloud consumption">
        {(s) => {
          // A customer reads its price (ADR 0021); a staff summary has no price lines.
          const lines = "lines" in s ? s.lines.filter((l): l is Extract<typeof l, { period: string }> => "period" in l) : [];
          if (lines.length === 0) return <Gap text="No consumption in the last six closed months." />;
          return (
            <>
              <BarTable
                caption="Cloud consumption at your price, by closed month"
                labelHeader="Month"
                valueHeader="Price"
                rows={[...lines].reverse().map((l) => ({
                  key: `${l.period}-${l.provider}-${l.currency}`,
                  label: (
                    <>
                      {SHORT_MONTH.format(new Date(l.period))} <span className="text-xs text-muted">({l.currency})</span>
                    </>
                  ),
                  size: size(l.price ? String(l.price.value) : null),
                  text: l.price ? decimal(String(l.price.value), 0) : "Not priced",
                  title: l.price ? money(String(l.price.value), l.price.unit) : "Not yet priced: your Getronics service manager can confirm the rate",
                }))}
              />
              <Source metric={lines.find((l) => l.price)?.price} note="Closed calendar months only, in each line's currency." />
            </>
          );
        }}
      </Body>
    </Frame>
  );
}

export function PostureWidget() {
  const loaded = useShared("posture", (t) => apiGet(t, "/v1/posture"));
  return (
    <Frame {...frame("posture")} href="/posture">
      <Body loaded={loaded} what="vulnerabilities">
        {(p) => (
          <>
            <BarTable
              caption="Open vulnerabilities by severity"
              labelHeader="Severity"
              valueHeader="Open"
              rows={p.by_severity.map((s) => ({
                key: String(s.severity),
                label: `Severity ${s.severity}`,
                size: Number(s.open.value),
                text: n(s.open),
                band: band(s.severity, THRESHOLDS.vulnerabilitySeverity),
              }))}
            />
            <div className="mt-3">
              <BarTable
                caption="Open vulnerabilities by age in days"
                labelHeader="Age (days)"
                valueHeader="Open"
                rows={p.by_age.map((a) => ({ key: a.band, label: a.band, size: Number(a.open.value), text: n(a.open) }))}
              />
            </div>
            <Source metric={p.open_total} />
          </>
        )}
      </Body>
    </Frame>
  );
}

export function AvailabilityWidget() {
  return (
    <Frame {...frame("availability")}>
      <Gap text="Not captured: availability needs the telemetry store (LLD L1), which is not connected yet." />
    </Frame>
  );
}

export function ReportWidget() {
  const loaded = useShared("reports", (t) => apiGet(t, "/v1/reports/packs"));
  return (
    <Frame {...frame("report")} href="/reports">
      <Body loaded={loaded} what="report packs">
        {(packs) => {
          const latest = packs[0];
          if (!latest) return <Gap text="No report pack yet." />;
          return (
            <Link href={`/reports/${latest.id}`} className="block rounded-control border border-line-subtle p-3 hover:shadow-raised">
              <span className="font-semibold text-ink">
                {periodLabel(latest.period)} · {latest.tenant.name}
              </span>
              <span className="block text-xs text-muted">
                Version {latest.version}, {latest.status}
                {latest.published_at ? `, published ${date(latest.published_at)}` : `, generated ${date(latest.generated_at)}`}
              </span>
            </Link>
          );
        }}
      </Body>
    </Frame>
  );
}

export function ApprovalsWidget() {
  const loaded = useShared("approvals", (t) => apiGet(t, "/v1/approvals"));
  return (
    <Frame {...frame("approvals")} href="/approvals">
      <Body loaded={loaded} what="approvals">
        {(a) => (
          <>
            <MetricCard label="Changes awaiting approval" metric={a.count} display={n} href="/approvals" />
            <ul className="mt-3 divide-y divide-line-subtle text-sm">
              {a.items.slice(0, 3).map((c) => (
                <li key={c.number} className="py-1.5">
                  <span className="font-medium">{c.number}</span>
                  <span className="block truncate text-xs text-muted">{c.short_description ?? ""}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </Body>
    </Frame>
  );
}

export const WIDGET_COMPONENTS: Record<WidgetId, () => React.JSX.Element> = {
  estate: EstateWidget,
  incidents: IncidentsWidget,
  closest: ClosestWidget,
  triage: TriageWidget,
  changes: ChangesWidget,
  noisy: NoisyWidget,
  sla_trend: SlaTrendWidget,
  backlog_customer: BacklogCustomerWidget,
  backlog_workgroup: BacklogWorkgroupWidget,
  connectors: ConnectorsWidget,
  csat: CsatWidget,
  cost_trend: CostTrendWidget,
  anomalies: AnomaliesWidget,
  reconciliation: ReconciliationWidget,
  margin: MarginWidget,
  platform_cost: PlatformCostWidget,
  ai_usage: AiUsageWidget,
  mapping: MappingWidget,
  onboarding: OnboardingWidget,
  price: PriceWidget,
  posture: PostureWidget,
  availability: AvailabilityWidget,
  report: ReportWidget,
  approvals: ApprovalsWidget,
};
