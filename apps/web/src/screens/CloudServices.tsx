"use client";

// Cloud Services (ADR 0038): the read-only audit of each customer's cloud accounts.
// customer → account → resource group or region → resource, each level with its open
// findings by impact, backup, policy compliance and (staff with cost.read) cost for
// the chosen dates against the period before. Figures are the API's; this screen
// names them and never re-derives one. Titles, names and recommendation text come
// from the provider or the customer: shown as text, never acted on.

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useId } from "react";

import type {
  CloudAccount,
  CloudEvent,
  CloudFinding,
  CloudKind,
  CloudNode,
  CloudPeriod,
  CloudSeverity,
  CloudTrend,
  MetricInt,
} from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard, type Trend } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { count, date, dateTime, money } from "@/lib/format";
import type { Band } from "@/lib/health";
import { customerQuery, useCustomer } from "@/lib/customer";
import { idPath, isUuid } from "@/lib/ids";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

export const KIND_LABEL: Record<CloudKind, string> = {
  backup: "Backup",
  policy: "Policy",
  advisor: "Advisor",
  defender: "Defender for Cloud",
  config: "Configuration",
};

const SEVERITY_LABEL: Record<CloudSeverity, string> = { high: "High", medium: "Medium", low: "Low", info: "Info" };
const SEVERITY_BAND: Record<CloudSeverity, Band> = { high: "critical", medium: "at-risk", low: "watch", info: "unknown" };
const KINDS: readonly CloudKind[] = ["advisor", "defender", "policy", "config", "backup"];
const SEVERITIES: readonly CloudSeverity[] = ["high", "medium", "low", "info"];
const STATUSES = ["open", "resolved", "all"] as const;
type Status = (typeof STATUSES)[number];

export function SeverityBadge({ severity }: { severity: CloudSeverity }) {
  return <HealthBadge band={SEVERITY_BAND[severity]} label={SEVERITY_LABEL[severity]} />;
}

function n(metric: MetricInt): number {
  return Number(metric.value);
}

// --- the date range, kept in the address -------------------------------------------------

const DAY = /^\d{4}-\d{2}-\d{2}$/;

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function useRange(): { from: string | undefined; to: string | undefined; query: Record<string, string | undefined>; set: (from: string | undefined, to: string | undefined) => void } {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const from = params.get("from");
  const to = params.get("to");
  const ok = (v: string | null) => (v !== null && DAY.test(v) ? v : undefined);
  const range = { from: ok(from), to: ok(to) };

  function set(nextFrom: string | undefined, nextTo: string | undefined) {
    const out = new URLSearchParams(params.toString());
    for (const [k, v] of [
      ["from", nextFrom],
      ["to", nextTo],
    ] as const) {
      if (v) out.set(k, v);
      else out.delete(k);
    }
    const qs = out.toString();
    router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
  }

  return { ...range, query: { from: range.from, to: range.to }, set };
}

function RangeControls({ range, period }: { range: ReturnType<typeof useRange>; period: CloudPeriod }) {
  const help = useId();
  const last = (days: number) => {
    const end = new Date();
    const start = new Date(end.getTime() - (days - 1) * 86_400_000);
    range.set(isoDay(start), isoDay(end));
  };
  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
        From
        <input type="date" className="field" value={range.from ?? period.start} max={range.to ?? period.end} aria-describedby={help} onChange={(e) => range.set(e.target.value || undefined, range.to)} />
      </label>
      <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
        To
        <input type="date" className="field" value={range.to ?? period.end} min={range.from ?? period.start} aria-describedby={help} onChange={(e) => range.set(range.from, e.target.value || undefined)} />
      </label>
      <div className="flex gap-1" role="group" aria-label="Quick ranges">
        {[7, 30, 90].map((d) => (
          <button key={d} type="button" className="btn btn-secondary text-sm" onClick={() => last(d)}>
            {d} days
          </button>
        ))}
      </div>
      <p id={help} className="w-full text-xs text-muted">
        Compared with {date(period.previous_start)} – {date(period.previous_end)}, the same length immediately before.
      </p>
    </div>
  );
}

// --- figures -------------------------------------------------------------------------------

function trendOf(now: number, before: number, upIsGood: boolean): Trend {
  if (now === before) return { direction: "flat", text: "same as the previous period", better: true };
  const direction = now > before ? "up" : "down";
  return { direction, text: `${count(Math.abs(now - before))} ${direction === "up" ? "more" : "fewer"} than the previous period`, better: (direction === "up") === upIsGood };
}

function TrendCards({ trend }: { trend: CloudTrend }) {
  return (
    <>
      <MetricCard label="Findings opened" metric={trend.opened} display={(m) => count(Number(m.value))} trend={trendOf(n(trend.opened), n(trend.previous_opened), false)} />
      <MetricCard label="Findings resolved" metric={trend.resolved} display={(m) => count(Number(m.value))} trend={trendOf(n(trend.resolved), n(trend.previous_resolved), true)} />
    </>
  );
}

function Cost({ node }: { node: CloudNode }) {
  if (node.cost.length === 0) return <span className="text-muted">—</span>;
  return (
    <span className="flex flex-col">
      {node.cost.map((c) => (
        <span key={c.currency} className="tabular-nums">
          {money(c.amount.value, c.currency)}
          <span className="ml-1 text-xs text-muted">(prev. {money(c.previous.value, c.currency)})</span>
        </span>
      ))}
    </span>
  );
}

function Issues({ node }: { node: CloudNode }) {
  const i = node.issues;
  return (
    <span className="flex flex-wrap gap-1">
      {n(i.high) > 0 ? <HealthBadge band="critical" label={`${count(n(i.high))} high`} /> : null}
      {n(i.medium) > 0 ? <HealthBadge band="at-risk" label={`${count(n(i.medium))} medium`} /> : null}
      {n(i.low) > 0 ? <HealthBadge band="watch" label={`${count(n(i.low))} low`} /> : null}
      {n(i.total) === 0 ? <span className="text-muted">None open</span> : null}
    </span>
  );
}

function nodeColumns(costVisible: boolean, link: (node: CloudNode) => string | null, labelHeader: string): Column<CloudNode>[] {
  const columns: Column<CloudNode>[] = [
    {
      key: "label",
      header: labelHeader,
      value: (r) => r.label,
      className: "min-w-48",
      render: (r) => {
        const href = link(r);
        return href ? (
          <Link href={href} className="link font-medium break-all">
            {r.label}
          </Link>
        ) : (
          <span className="break-all">{r.label}</span>
        );
      },
    },
    { key: "issues", header: "Open findings", value: (r) => n(r.issues.high) * 1e6 + n(r.issues.medium) * 1e3 + n(r.issues.low), render: (r) => <Issues node={r} /> },
    { key: "resources", header: "Resources", value: (r) => n(r.resources), className: "tabular-nums" },
    { key: "protected", header: "Backed up", value: (r) => n(r.backup.protected), className: "tabular-nums" },
    { key: "failing", header: "Backup failing", value: (r) => n(r.backup.failing), className: "tabular-nums" },
    { key: "unprotected", header: "VMs without backup", value: (r) => n(r.backup.unprotected), className: "tabular-nums" },
    { key: "policy", header: "Policy non-compliant", value: (r) => n(r.policy.non_compliant), className: "tabular-nums" },
  ];
  if (costVisible) columns.push({ key: "cost", header: "Cost (vs previous)", value: (r) => r.cost.map((c) => `${c.currency} ${c.amount.value}`).join("; ") || null, render: (r) => <Cost node={r} /> });
  return columns;
}

function Summary({ node, costVisible }: { node: CloudNode; costVisible: boolean }) {
  const cost = node.cost[0];
  return (
    <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <MetricCard label="Open findings" metric={node.issues.total} display={(m) => count(Number(m.value))} band={n(node.issues.high) > 0 ? "critical" : n(node.issues.medium) > 0 ? "at-risk" : "healthy"} />
      <MetricCard label="High impact" metric={node.issues.high} display={(m) => count(Number(m.value))} />
      <MetricCard label="VMs without backup" metric={node.backup.unprotected} display={(m) => count(Number(m.value))} band={n(node.backup.unprotected) > 0 ? "at-risk" : "healthy"} />
      <MetricCard label="Backup failing" metric={node.backup.failing} display={(m) => count(Number(m.value))} band={n(node.backup.failing) > 0 ? "critical" : "healthy"} />
      <MetricCard label="Policy non-compliant" metric={node.policy.non_compliant} display={(m) => count(Number(m.value))} />
      {costVisible && cost
        ? node.cost.map((c) => (
            <MetricCard
              key={c.currency}
              label={`Cost (${c.currency})`}
              metric={c.amount}
              display={(m) => money(String(m.value), c.currency)}
              trend={{
                direction: Number(c.amount.value) > Number(c.previous.value) ? "up" : Number(c.amount.value) < Number(c.previous.value) ? "down" : "flat",
                text: `previous period ${money(c.previous.value, c.currency)}`,
                better: Number(c.amount.value) <= Number(c.previous.value),
              }}
            />
          ))
        : null}
    </div>
  );
}

function checkColumns(withCustomer: boolean): Column<CloudFinding>[] {
  const columns: Column<CloudFinding>[] = [
    { key: "severity", header: "Impact", value: (c) => SEVERITIES.indexOf(c.severity), render: (c) => (c.state === "issue" ? <SeverityBadge severity={c.severity} /> : <HealthBadge band={c.state === "ok" ? "healthy" : "unknown"} label={c.state === "ok" ? "OK" : "Not applicable"} />) },
    { key: "kind", header: "Source", value: (c) => KIND_LABEL[c.kind] },
    { key: "category", header: "Category", value: (c) => c.category.replace(/_/g, " ") },
    {
      key: "title",
      header: "Finding",
      value: (c) => c.title,
      className: "min-w-64",
      render: (c) => (
        <span className="flex flex-col">
          <span>{c.title}</span>
          {typeof c.detail.solution === "string" && c.detail.solution ? <span className="text-xs text-muted">{c.detail.solution}</span> : null}
        </span>
      ),
    },
    {
      key: "resource",
      header: "Resource",
      value: (c) => c.resource_native_id,
      render: (c) => {
        const name = c.resource_native_id?.split("/").pop() ?? "Subscription";
        const href = idPath("/cloud-services/resources", c.resource_id);
        return href ? (
          <Link href={href} className="link break-all">
            {name}
          </Link>
        ) : (
          <span className="break-all">{name}</span>
        );
      },
    },
    { key: "group", header: "Resource group", value: (c) => c.resource_group },
    { key: "seen", header: "Last seen", value: (c) => c.last_seen, render: (c) => dateTime(c.last_seen) },
  ];
  if (withCustomer) columns.splice(3, 0, { key: "customer", header: "Customer", value: (c) => c.tenant.name });
  return columns;
}

function CheckTable({ id, caption, rows, withCustomer = false }: { id: string; caption: string; rows: CloudFinding[]; withCustomer?: boolean }) {
  const me = useMe();
  return (
    <DataTable
      id={id}
      caption={caption}
      rows={rows}
      rowKey={(c) => c.id}
      columns={checkColumns(withCustomer)}
      facets={[
        { key: "kind", label: "Source", value: (c) => KIND_LABEL[c.kind] },
        { key: "severity", label: "Impact", value: (c) => SEVERITY_LABEL[c.severity] },
        { key: "category", label: "Category", value: (c) => c.category.replace(/_/g, " ") },
      ]}
      role={me.role}
      empty={{ title: "No findings", next: "Nothing matches. Widen the filters or the dates." }}
    />
  );
}

function Header({ title, subtitle, range, period }: { title: string; subtitle: string; range: ReturnType<typeof useRange>; period: CloudPeriod }) {
  const path = usePathname();
  const sections = [
    { href: "/cloud-services", label: "Overview" },
    { href: "/cloud-services/findings", label: "Findings" },
  ];
  return (
    <>
      <PageHeader title={title} subtitle={subtitle}>
        <RangeControls range={range} period={period} />
      </PageHeader>
      <nav aria-label="Cloud Services sections" className="-mt-3 mb-5">
        <ul className="flex flex-wrap gap-2">
          {sections.map((s) => (
            <li key={s.href}>
              <Link href={s.href} aria-current={path === s.href ? "page" : undefined} className={`btn ${path === s.href ? "btn-primary" : "btn-secondary"} text-sm`}>
                {s.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </>
  );
}

function CostNote({ visible }: { visible: boolean }) {
  return visible ? null : <p className="mb-4 text-sm text-muted">Provider cost is shown to Getronics staff with cost access only.</p>;
}

// --- overview ------------------------------------------------------------------------------

export function CloudServicesOverview() {
  const range = useRange();
  const { tenantId } = useCustomer();
  const me = useMe();
  const loaded = useApi(
    (token) => apiGet(token, "/v1/cloud-services/overview", {}, { ...range.query, ...customerQuery(tenantId) }),
    `cloud:overview:${range.from}:${range.to}:${tenantId ?? "all"}`,
  );
  if (loaded.status === "loading") return <Loading what="cloud services" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="cloud services" />;
  const v = loaded.data;
  return (
    <>
      <Header title="Cloud Services" subtitle="Backup, configuration, policy and best-practice findings for each cloud account, read-only, every six hours." range={range} period={v.period} />
      <CostNote visible={v.cost_visible} />
      <Summary node={v.total} costVisible={v.cost_visible} />
      <div className="mb-4 grid gap-3 sm:grid-cols-2">
        <TrendCards trend={v.trend} />
      </div>
      {v.accounts.length === 0 ? (
        <EmptyState title="No cloud accounts yet" next="A customer's accounts appear here once its cloud is onboarded and the first audit has run." />
      ) : (
        <>
          {me.kind === "staff" && v.customers.length > 1 ? (
            <section aria-labelledby="customers-heading" className="card mb-4 p-4">
              <h2 id="customers-heading" className="mb-2 section-title">
                By customer
              </h2>
              <DataTable id="cloud-customers" caption="Cloud Services by customer" rows={v.customers} rowKey={(r) => r.key} columns={nodeColumns(v.cost_visible, () => null, "Customer")} role={me.role} empty={{ title: "No customers", next: "No customer in your scope has a cloud account." }} />
            </section>
          ) : null}
          <section aria-labelledby="accounts-heading" className="card mb-4 p-4">
            <h2 id="accounts-heading" className="mb-2 section-title">
              Accounts
            </h2>
            <DataTable
              id="cloud-accounts"
              caption="Cloud accounts"
              rows={v.accounts.map((a) => ({ ...a.summary, label: `${a.account.display_name} · ${a.account.tenant.name}` }))}
              rowKey={(r) => r.key}
              columns={nodeColumns(v.cost_visible, (r) => idPath("/cloud-services/accounts", r.key), "Account")}
              role={me.role}
              empty={{ title: "No accounts", next: "No account in your scope." }}
            />
          </section>
        </>
      )}
      <section aria-labelledby="highlights-heading" className="card mb-4 p-4">
        <h2 id="highlights-heading" className="mb-2 section-title">
          Highest-impact open findings
        </h2>
        <CheckTable id="cloud-highlights" caption="Highest-impact open findings" rows={v.highlights} withCustomer={me.kind === "staff"} />
        <p className="mt-2 text-sm">
          <Link href="/cloud-services/findings" className="link">
            All findings
          </Link>
        </p>
        <div className="mt-2">
          <Provenance metric={v.total.issues.total} />
        </div>
      </section>
    </>
  );
}

// --- account -------------------------------------------------------------------------------

function groupHref(id: string, by: CloudAccount["group_by"], key: string): string {
  const q = new URLSearchParams({ group_by: by, group: key });
  return `/cloud-services/accounts/${encodeURIComponent(id)}?${q.toString()}#resources`;
}

export function CloudAccountView({ id }: { id: string }) {
  const range = useRange();
  const params = useSearchParams();
  const me = useMe();
  const by = params.get("group_by") === "region" ? "region" : "resource_group";
  const group = params.get("group");
  const loaded = useApi(
    (token) => apiGet(token, "/v1/cloud-services/accounts/{account_id}", { account_id: id }, { ...range.query, group_by: by }),
    `cloud:account:${id}:${by}:${range.from}:${range.to}`,
  );
  if (loaded.status === "loading") return <Loading what="the account" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="the account" />;
  const v = loaded.data;
  const a = v.account;
  return (
    <>
      <Header title={a.display_name} subtitle={`${a.tenant.name} · ${a.provider === "azure" ? "Azure subscription" : "AWS account"} ${a.native_id}`} range={range} period={v.period} />
      <p className="-mt-2 mb-4 text-sm">
        <Link href="/cloud-services" className="link">
          Cloud Services
        </Link>{" "}
        › {a.display_name}
      </p>
      <CostNote visible={v.cost_visible} />
      <Summary node={v.summary} costVisible={v.cost_visible} />
      <section aria-labelledby="groups-heading" className="card mb-4 p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h2 id="groups-heading" className="section-title">
            By {by === "region" ? "region" : "resource group"}
          </h2>
          <div className="flex gap-1" role="group" aria-label="Group by">
            {(["resource_group", "region"] as const).map((g) => (
              <Link key={g} href={`/cloud-services/accounts/${encodeURIComponent(id)}?group_by=${g}`} aria-current={by === g ? "true" : undefined} className={`btn ${by === g ? "btn-primary" : "btn-secondary"} text-sm`}>
                {g === "region" ? "Region" : "Resource group"}
              </Link>
            ))}
          </div>
        </div>
        <DataTable id="cloud-groups" caption={`Account by ${by === "region" ? "region" : "resource group"}`} rows={v.groups} rowKey={(r) => r.key || "(none)"} columns={nodeColumns(v.cost_visible, (r) => groupHref(id, by, r.key), by === "region" ? "Region" : "Resource group")} role={me.role} empty={{ title: "Nothing audited yet", next: "The first audit of this account has not run yet." }} />
      </section>
      {group !== null ? <GroupResources account={id} by={by} group={group} /> : null}
    </>
  );
}

function GroupResources({ account, by, group }: { account: string; by: CloudAccount["group_by"]; group: string }) {
  const range = useRange();
  const me = useMe();
  const query = { ...range.query, account_id: account, ...(by === "region" ? { region: group } : { resource_group: group }) };
  const loaded = useApi((token) => apiGet(token, "/v1/cloud-services/resources", {}, query), `cloud:resources:${account}:${by}:${group}:${range.from}:${range.to}`);
  return (
    <section id="resources" aria-labelledby="resources-heading" className="card mb-4 p-4">
      <h2 id="resources-heading" className="mb-2 section-title">
        Resources in {group || "(none)"}
      </h2>
      {loaded.status === "loading" ? <Loading what="resources" /> : null}
      {loaded.status === "error" ? <LoadError error={loaded.error} what="resources" /> : null}
      {loaded.status === "ready" ? (
        <>
          <DataTable id="cloud-resources" caption={`Resources in ${group || "(none)"}`} rows={loaded.data.items} rowKey={(r) => r.key} columns={nodeColumns(loaded.data.cost_visible, (r) => idPath("/cloud-services/resources", r.key), "Resource")} role={me.role} empty={{ title: "No resources", next: "Inventory holds no resource here; findings without one are under Findings." }} />
          <p className="mt-2 text-xs text-muted">
            {count(n(loaded.data.count))} resources{n(loaded.data.count) > loaded.data.items.length ? `, the ${loaded.data.items.length} with most findings shown` : ""}.
          </p>
        </>
      ) : null}
    </section>
  );
}

// --- resource ------------------------------------------------------------------------------

const CHANGE_LABEL: Record<CloudEvent["change"], string> = { opened: "Opened", changed: "Changed", resolved: "Resolved", reopened: "Reopened" };

export function CloudResourceView({ id }: { id: string }) {
  const range = useRange();
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/cloud-services/resources/{resource_id}", { resource_id: id }, range.query), `cloud:resource:${id}:${range.from}:${range.to}`);
  if (loaded.status === "loading") return <Loading what="the resource" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="the resource" />;
  const v = loaded.data;
  const r = v.resource;
  const settings = Object.entries(r.settings);
  const accountHref = isUuid(r.account.id) ? `/cloud-services/accounts/${encodeURIComponent(r.account.id)}` : null;
  return (
    <>
      <Header title={r.name} subtitle={`${r.type} · ${r.region ?? "no region"} · ${r.tenant.name}`} range={range} period={v.period} />
      <p className="-mt-2 mb-4 text-sm">
        <Link href="/cloud-services" className="link">
          Cloud Services
        </Link>{" "}
        ›{" "}
        {accountHref ? (
          <Link href={accountHref} className="link">
            {r.account.display_name}
          </Link>
        ) : (
          r.account.display_name
        )}
        {r.resource_group && accountHref ? (
          <>
            {" "}
            ›{" "}
            <Link href={groupHref(r.account.id, "resource_group", r.resource_group)} className="link">
              {r.resource_group}
            </Link>
          </>
        ) : null}{" "}
        › {r.name}
      </p>
      <CostNote visible={v.cost_visible} />
      <Summary node={v.summary} costVisible={v.cost_visible} />
      <div className="mb-4 grid gap-4 lg:grid-cols-2">
        <section aria-labelledby="config-heading" className="card p-4">
          <h2 id="config-heading" className="mb-2 section-title">
            Configuration
          </h2>
          {settings.length === 0 ? (
            <p className="text-sm text-muted">No configuration read for this type.</p>
          ) : (
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              {settings.map(([k, val]) => (
                <div key={k} className="contents">
                  <dt className="text-muted">{k}</dt>
                  <dd className="break-all">{val === null ? "—" : Array.isArray(val) ? val.join(", ") : String(val)}</dd>
                </div>
              ))}
            </dl>
          )}
          {r.settings_seen ? <p className="mt-2 text-xs text-muted">Read {dateTime(r.settings_seen)}</p> : null}
        </section>
        <section aria-labelledby="identity-heading" className="card p-4">
          <h2 id="identity-heading" className="mb-2 section-title">
            Identity
          </h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted">Provider id</dt>
            <dd className="break-all">{r.native_id}</dd>
            <dt className="text-muted">Type</dt>
            <dd>{r.type_native}</dd>
            <dt className="text-muted">State</dt>
            <dd>{r.state ?? "—"}</dd>
            {me.kind === "staff" ? (
              <>
                <dt className="text-muted">CMDB CI</dt>
                <dd>{r.ci_sys_id ?? "Not linked"}</dd>
              </>
            ) : null}
          </dl>
          <p className="mt-2 text-sm">
            {idPath("/resources", r.id) ? (
              <Link href={idPath("/resources", r.id) ?? "/resources"} className="link">
                Tickets and context
              </Link>
            ) : null}
          </p>
        </section>
      </div>
      <section aria-labelledby="checks-heading" className="card mb-4 p-4">
        <h2 id="checks-heading" className="mb-2 section-title">
          Checks
        </h2>
        <CheckTable id="cloud-resource-checks" caption="Checks on this resource" rows={v.checks} />
      </section>
      {v.cost_visible ? (
        <section aria-labelledby="cost-heading" className="card mb-4 p-4">
          <h2 id="cost-heading" className="mb-2 section-title">
            Cost by day
          </h2>
          {v.daily_cost.length === 0 ? (
            <p className="text-sm text-muted">No cost allocated to this resource in the period.</p>
          ) : (
            <table className="w-full max-w-xl text-sm">
              <caption className="sr-only">Cost by day, in transaction currency</caption>
              <thead>
                <tr className="text-left text-xs text-muted">
                  <th scope="col" className="py-1 font-semibold">
                    Day
                  </th>
                  <th scope="col" className="py-1 text-right font-semibold">
                    Cost
                  </th>
                </tr>
              </thead>
              <tbody>
                {v.daily_cost.map((d) => (
                  <tr key={`${d.day}-${d.currency}`} className="border-t border-line-subtle">
                    <td className="py-1">{date(d.day)}</td>
                    <td className="py-1 text-right tabular-nums">{money(d.amount.value, d.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ) : null}
      <section aria-labelledby="history-heading" className="card mb-4 p-4">
        <h2 id="history-heading" className="mb-2 section-title">
          History of changes
        </h2>
        {v.history.length === 0 ? (
          <p className="text-sm text-muted">No change recorded yet.</p>
        ) : (
          <ol className="flex flex-col gap-2 text-sm">
            {v.history.map((h) => (
              <li key={`${h.check_id}-${h.at}-${h.change}`} className="border-l-2 border-line pl-3">
                <span className="font-semibold">{CHANGE_LABEL[h.change]}</span> · {h.title} <span className="text-xs text-muted">{dateTime(h.at)}</span>
                {h.changed.length > 0 ? <span className="block text-xs text-muted">{h.changed.join(", ")}</span> : null}
              </li>
            ))}
          </ol>
        )}
      </section>
    </>
  );
}

// --- findings ------------------------------------------------------------------------------

function pick<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : undefined;
}

export function CloudFindingsView() {
  const range = useRange();
  const params = useSearchParams();
  const { tenantId } = useCustomer();
  const me = useMe();
  const kind = pick(params.get("kind"), KINDS);
  const severity = pick(params.get("severity"), SEVERITIES);
  const status: Status = pick(params.get("status"), STATUSES) ?? "open";
  const account = params.get("account_id");
  const query = { ...range.query, ...customerQuery(tenantId), kind, severity, status, account_id: isUuid(account) ? account : undefined };
  const loaded = useApi((token) => apiGet(token, "/v1/cloud-services/findings", {}, query), `cloud:findings:${JSON.stringify(query)}`);
  const filter = (k: string, v: string | undefined) => {
    const q = new URLSearchParams(params.toString());
    if (v) q.set(k, v);
    else q.delete(k);
    return `/cloud-services/findings?${q.toString()}`;
  };
  if (loaded.status === "loading") return <Loading what="findings" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="findings" />;
  const v = loaded.data;
  return (
    <>
      <Header title="Cloud findings" subtitle="Every check from Azure Advisor, Defender for Cloud, Azure Policy, Azure Backup and the platform's configuration rules, highest impact first." range={range} period={v.period} />
      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <MetricCard label="Matching" metric={v.count} display={(m) => count(Number(m.value))} />
        <TrendCards trend={v.trend} />
      </div>
      <nav aria-label="Finding filters" className="mb-4 flex flex-col gap-2 text-sm">
        {(
          [
            ["status", "Status", STATUSES, status, (s: string) => s[0]?.toUpperCase() + s.slice(1)],
            ["kind", "Source", KINDS, kind, (s: string) => KIND_LABEL[s as CloudKind]],
            ["severity", "Impact", SEVERITIES, severity, (s: string) => SEVERITY_LABEL[s as CloudSeverity]],
          ] as const
        ).map(([key, label, values, current, show]) => (
          <div key={key} className="flex flex-wrap items-center gap-1">
            <span className="w-16 text-xs font-semibold text-muted">{label}</span>
            {key !== "status" ? (
              <Link href={filter(key, undefined)} aria-current={current === undefined ? "true" : undefined} className={`btn ${current === undefined ? "btn-primary" : "btn-secondary"} text-xs`}>
                Any
              </Link>
            ) : null}
            {values.map((val) => (
              <Link key={val} href={filter(key, val)} aria-current={current === val ? "true" : undefined} className={`btn ${current === val ? "btn-primary" : "btn-secondary"} text-xs`}>
                {show(val)}
              </Link>
            ))}
          </div>
        ))}
      </nav>
      <section aria-labelledby="findings-heading" className="card mb-4 p-4">
        <h2 id="findings-heading" className="mb-2 section-title">
          Findings
        </h2>
        <CheckTable id="cloud-findings" caption="Cloud findings" rows={v.items} withCustomer={me.kind === "staff"} />
        {n(v.count) > v.items.length ? <p className="mt-2 text-xs text-muted">The {count(v.items.length)} highest-impact of {count(n(v.count))} shown: narrow the filters to see the rest.</p> : null}
        <div className="mt-2">
          <Provenance metric={v.count} />
        </div>
      </section>
    </>
  );
}
