"use client";

// The SecOps workspace (HLD M13, ADR 0036): staff only. Open vulnerabilities aged
// against their severity's remediation target, scanner coverage, and a posture
// score per customer. The targets and the score are modelled, because the HLD and
// LLD set none: every such figure carries the Modelled mark and the rule is stated
// beside it. Bands are the API's; this screen names them and never re-derives one.

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import type { ReactNode } from "react";

import type {
  CustomerPostureScore,
  MetricDecimal,
  MetricInt,
  QuarantinedHost,
  SecOpsFinding,
  SeverityAgeing,
  SlaBand,
  UnmatchedResource,
} from "@cloudops/contracts";

import { BarTable } from "@/components/BarTable";
import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { ModelledMark, Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { count, date, dateTime, decimal } from "@/lib/format";
import { band, THRESHOLDS, type Band } from "@/lib/health";
import { customerQuery, useCustomer } from "@/lib/customer";
import { idPath } from "@/lib/ids";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

export const SEVERITY_LABEL: Record<number, string> = { 5: "Urgent", 4: "Critical", 3: "Serious", 2: "Medium", 1: "Minimal" };

export const SLA_LABEL: Record<SlaBand, string> = {
  breached: "Breached",
  at_risk: "At risk",
  within: "Within target",
  no_target: "No target",
};

export const SLA_BAND: Record<SlaBand, Band> = {
  breached: "critical",
  at_risk: "at-risk",
  within: "healthy",
  no_target: "unknown",
};

const SLA_ORDER: readonly SlaBand[] = ["breached", "at_risk", "within", "no_target"];

export function severityLabel(severity: number): string {
  return `${severity} · ${SEVERITY_LABEL[severity] ?? "Unknown"}`;
}

export function SeverityBadge({ severity }: { severity: number }) {
  return <HealthBadge band={band(severity, THRESHOLDS.vulnerabilitySeverity)} label={severityLabel(severity)} />;
}

export function SlaBadge({ sla }: { sla: SlaBand }) {
  return <HealthBadge band={SLA_BAND[sla]} label={SLA_LABEL[sla]} />;
}

/** A link to a record by its id; plain text if the id is not a UUID. */
export function IdLink({ base, id, children }: { base: string; id: string; children: ReactNode }) {
  const href = idPath(base, id);
  return href ? (
    <Link href={href} className="link font-medium">
      {children}
    </Link>
  ) : (
    <>{children}</>
  );
}

/** A figure or "—" where the API says there is none: never zero. */
function figure(metric: MetricInt | null | undefined): string {
  return metric ? count(Number(metric.value)) : "—";
}

function points(metric: MetricDecimal | null | undefined, suffix = ""): string {
  return metric ? `${decimal(metric.value, 1)}${suffix}` : "—";
}

const SECTIONS = [
  { href: "/secops", label: "Vulnerabilities" },
  { href: "/secops/coverage", label: "Scanner coverage" },
  { href: "/secops/score", label: "Posture score" },
] as const;

function SecOpsHeader({ subtitle }: { subtitle: string }) {
  const path = usePathname();
  return (
    <>
      <PageHeader title="SecOps" subtitle={subtitle} />
      <nav aria-label="SecOps sections" className="-mt-3 mb-5">
        <ul className="flex flex-wrap gap-2">
          {SECTIONS.map((s) => {
            const current = path === s.href;
            return (
              <li key={s.href}>
                <Link
                  href={s.href}
                  aria-current={current ? "page" : undefined}
                  className={`btn ${current ? "btn-primary" : "btn-secondary"} text-sm`}
                >
                  {s.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </>
  );
}

/** The rule behind every modelled ageing figure, in words. */
function TargetRule({ severities, fraction }: { severities: SeverityAgeing[]; fraction: MetricDecimal }) {
  const targets = severities
    .filter((s) => s.target_days)
    .map((s) => `severity ${s.severity} ${s.target_days?.value} days`)
    .join(", ");
  const share = decimal(String(Number(fraction.value) * 100), 0);
  return (
    <p className="text-sm text-muted">
      <ModelledMark /> The HLD and LLD set no remediation target, so these are the platform’s modelled targets (ADR 0036): {targets}; severity 1 has
      none. A finding is <strong className="text-ink">breached</strong> once more than its target has passed since Qualys first detected it, and{" "}
      <strong className="text-ink">at risk</strong> from {share}% of it.
    </p>
  );
}

// --- vulnerabilities ---------------------------------------------------------------------

function filterHref(severity: number | null, sla: SlaBand | null): string {
  const q = new URLSearchParams();
  if (severity !== null) q.set("severity", String(severity));
  if (sla !== null) q.set("sla", sla);
  const s = q.toString();
  return `/secops${s ? `?${s}` : ""}#findings`;
}

function isSla(value: string | null): value is SlaBand {
  return value !== null && (SLA_ORDER as readonly string[]).includes(value);
}

/** A count that opens the findings it counts. */
function Drill({ metric, severity, sla, what }: { metric: MetricInt | null; severity: number; sla: SlaBand | null; what: string }) {
  if (!metric) return <span aria-label="No target">—</span>;
  const n = Number(metric.value);
  if (n === 0) return <>0</>;
  return (
    <Link href={filterHref(severity, sla)} className="link tabular-nums" aria-label={`${count(n)} ${what}, severity ${severityLabel(severity)}: show them`}>
      {count(n)}
    </Link>
  );
}

function AgeingTable({ rows }: { rows: SeverityAgeing[] }) {
  const cell = "px-2 py-1.5 text-right tabular-nums";
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[44rem] border-collapse text-sm">
        <caption className="sr-only">Open findings by severity, aged against the modelled target and by days open</caption>
        <thead>
          <tr className="text-left text-xs text-muted">
            <th scope="col" className="py-1 pr-2 font-semibold">
              Severity
            </th>
            <th scope="col" className={`${cell} font-semibold`}>
              Target (days)
            </th>
            <th scope="col" className={`${cell} font-semibold`}>
              Open
            </th>
            <th scope="col" className={`${cell} font-semibold`}>
              Within
            </th>
            <th scope="col" className={`${cell} font-semibold`}>
              At risk
            </th>
            <th scope="col" className={`${cell} font-semibold`}>
              Breached
            </th>
            {rows[0]?.by_age.map((a) => (
              <th key={a.band} scope="col" className={`${cell} font-semibold`}>
                {a.band} days
              </th>
            ))}
            <th scope="col" className={`${cell} font-semibold`}>
              Oldest (days)
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.severity} className="border-t border-line-subtle">
              <th scope="row" className="py-1.5 pr-2 text-left font-normal">
                <SeverityBadge severity={s.severity} />
              </th>
              <td className={cell}>{figure(s.target_days)}</td>
              <td className={cell}>
                <Drill metric={s.open} severity={s.severity} sla={null} what="open" />
              </td>
              <td className={cell}>
                <Drill metric={s.within} severity={s.severity} sla="within" what="within target" />
              </td>
              <td className={cell}>
                <Drill metric={s.at_risk} severity={s.severity} sla="at_risk" what="at risk" />
              </td>
              <td className={cell}>
                <Drill metric={s.breached} severity={s.severity} sla="breached" what="breached" />
              </td>
              {s.by_age.map((a) => (
                <td key={a.band} className={cell}>
                  {figure(a.open)}
                </td>
              ))}
              <td className={cell}>{figure(s.oldest_days)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SecOpsVulnerabilities() {
  useAssistantContext({ screen: "posture" });
  const me = useMe();
  const params = useSearchParams();
  const rawSeverity = params.get("severity");
  const severity = rawSeverity && /^[1-5]$/.test(rawSeverity) ? Number(rawSeverity) : null;
  const rawSla = params.get("sla");
  const sla = isSla(rawSla) ? rawSla : null;
  const query = { severity: severity === null ? undefined : String(severity), sla: sla ?? undefined };
  const { tenantId } = useCustomer();
  const loaded = useApi(
    (token) => apiGet(token, "/v1/secops/vulnerabilities", {}, { ...query, ...customerQuery(tenantId) }),
    `secops:${severity}:${sla}:${tenantId ?? "all"}`,
  );
  if (loaded.status === "loading") return <Loading what="vulnerabilities" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="vulnerabilities" />;
  const v = loaded.data;
  const filtered = severity !== null || sla !== null;

  const columns: Column<SecOpsFinding>[] = [
    { key: "severity", header: "Severity", value: (f) => f.severity, render: (f) => <SeverityBadge severity={f.severity} /> },
    {
      key: "title",
      header: "Finding",
      value: (f) => f.title,
      className: "min-w-64",
      render: (f) => (
        <IdLink base="/secops/findings" id={f.id}>
          {f.title}
        </IdLink>
      ),
    },
    { key: "customer", header: "Customer", value: (f) => f.tenant.name },
    {
      key: "asset",
      header: "Asset",
      value: (f) => f.asset_name,
      render: (f) => (
        <span className="flex flex-col">
          <span className="break-all">{f.asset_name}</span>
          {f.resource_id ? null : <span className="text-xs text-muted">No resource match</span>}
        </span>
      ),
    },
    { key: "age", header: "Age (days)", value: (f) => Number(f.age.value), className: "tabular-nums" },
    { key: "target", header: "Target (days)", value: (f) => (f.target_days ? Number(f.target_days.value) : null), render: (f) => figure(f.target_days), className: "tabular-nums" },
    { key: "sla", header: "Against target", value: (f) => SLA_LABEL[f.sla], render: (f) => <SlaBadge sla={f.sla} /> },
    { key: "first", header: "First detected", value: (f) => f.first_detected, render: (f) => date(f.first_detected) },
    { key: "qid", header: "QID", value: (f) => f.qid },
  ];
  const listed = Number(v.listed.value);

  return (
    <>
      <SecOpsHeader subtitle="Open Qualys findings across your customers, aged against their remediation target, most overdue first." />
      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <MetricCard label="Open findings" metric={v.open_total} display={(m) => count(Number(m.value))} />
        <MetricCard
          label="Breached target"
          metric={v.breached_total}
          display={(m) => count(Number(m.value))}
          band={band(Number(v.breached_total.value), THRESHOLDS.criticalVulnerabilities)}
          href={filterHref(null, "breached")}
        />
        <MetricCard label="At risk" metric={v.at_risk_total} display={(m) => count(Number(m.value))} href={filterHref(null, "at_risk")} />
      </div>

      <section aria-labelledby="ageing-heading" className="card mb-4 p-4">
        <h2 id="ageing-heading" className="mb-2 section-title">
          Ageing against target, by severity
        </h2>
        <div className="mb-3">
          <TargetRule severities={v.by_severity} fraction={v.at_risk_fraction} />
        </div>
        <AgeingTable rows={v.by_severity} />
        <div className="mt-2">
          <Provenance metric={v.open_total} />
        </div>
      </section>

      <section aria-labelledby="customers-heading" className="card mb-4 p-4">
        <h2 id="customers-heading" className="mb-2 section-title">
          By customer
        </h2>
        <BarTable
          caption="Open findings by customer"
          labelHeader="Customer"
          valueHeader="Open findings"
          extra={["Urgent or critical", "At risk", "Breached"]}
          rows={[...v.by_customer]
            .sort((a, b) => Number(b.open.value) - Number(a.open.value) || a.tenant.name.localeCompare(b.tenant.name))
            .map((c) => ({
              key: c.tenant.id,
              label: c.tenant.name,
              size: Number(c.open.value),
              text: count(Number(c.open.value)),
              band: Number(c.breached.value) > 0 ? "critical" : undefined,
              cells: [count(Number(c.urgent_or_critical.value)), count(Number(c.at_risk.value)), count(Number(c.breached.value))],
              title: `${c.tenant.name}: ${count(Number(c.open.value))} open, ${count(Number(c.breached.value))} breached`,
            }))}
        />
      </section>

      <section id="findings" aria-labelledby="findings-heading">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="findings-heading" className="section-title">
            {filtered
              ? `Findings: ${[severity !== null ? `severity ${severityLabel(severity)}` : null, sla ? SLA_LABEL[sla].toLowerCase() : null].filter(Boolean).join(", ")}`
              : "Findings"}
          </h2>
          {filtered ? (
            <Link href="/secops#findings" className="link text-sm">
              Show every open finding
            </Link>
          ) : null}
        </div>
        {listed >= 500 ? (
          <p role="status" className="mb-2 text-sm text-muted">
            Showing the {count(listed)} most overdue. Narrow by severity or target band above to see the rest.
          </p>
        ) : null}
        <DataTable
          id="secops-findings"
          caption="Open findings, most overdue first"
          rows={v.findings}
          rowKey={(f) => f.id}
          columns={columns}
          facets={[
            { key: "sla", label: "Against target", value: (f) => SLA_LABEL[f.sla] },
            { key: "severity", label: "Severity", value: (f) => severityLabel(f.severity) },
            { key: "customer", label: "Customer", value: (f) => f.tenant.name },
          ]}
          role={me.role}
          empty={{
            title: filtered ? "No open findings match" : "No open findings",
            next: filtered ? "Clear the filter to see every open finding." : "Findings appear after the Qualys connector’s daily sync, and leave when they are remediated.",
          }}
        />
      </section>
    </>
  );
}

// --- coverage --------------------------------------------------------------------------

export function SecOpsCoverage() {
  useAssistantContext({ screen: "posture" });
  const me = useMe();
  const { tenantId } = useCustomer();
  const loaded = useApi((token) => apiGet(token, "/v1/secops/coverage", {}, customerQuery(tenantId)), `secops-coverage:${tenantId ?? "all"}`);
  if (loaded.status === "loading") return <Loading what="scanner coverage" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="scanner coverage" />;
  const c = loaded.data;
  const types = c.scannable_types.join(", ");

  const resourceColumns: Column<UnmatchedResource>[] = [
    {
      key: "name",
      header: "Resource",
      value: (r) => r.name,
      render: (r) => (
        <IdLink base="/resources" id={r.id}>
          {r.name}
        </IdLink>
      ),
    },
    { key: "customer", header: "Customer", value: (r) => r.tenant.name },
    { key: "fqdn", header: "FQDN", value: (r) => r.fqdn, render: (r) => r.fqdn ?? "—" },
    { key: "ips", header: "Addresses", value: (r) => r.ip_addresses.join(", "), render: (r) => (r.ip_addresses.length ? r.ip_addresses.join(", ") : "—") },
    { key: "region", header: "Region", value: (r) => r.region, render: (r) => r.region ?? "—" },
    { key: "seen", header: "Last seen", value: (r) => r.last_seen, render: (r) => date(r.last_seen) },
    {
      key: "hint",
      header: "Possible Qualys hosts",
      value: (r) => Number(r.possible_hosts.value),
      className: "tabular-nums",
    },
  ];
  const hostColumns: Column<QuarantinedHost>[] = [
    { key: "name", header: "Qualys host", value: (h) => h.name, render: (h) => <span className="break-all">{h.name ?? "—"}</span> },
    { key: "customer", header: "Customer", value: (h) => h.tenant.name },
    { key: "ip", header: "Address", value: (h) => h.ip_address, render: (h) => h.ip_address ?? "—" },
    { key: "open", header: "Open findings", value: (h) => Number(h.open_findings.value), className: "tabular-nums" },
    { key: "seen", header: "Sightings", value: (h) => Number(h.occurrences.value), className: "tabular-nums" },
    { key: "last", header: "Last seen", value: (h) => h.last_seen, render: (h) => dateTime(h.last_seen) },
    { key: "hint", header: "Possible resources", value: (h) => Number(h.possible_resources.value), className: "tabular-nums" },
    { key: "key", header: "Qualys key", value: (h) => h.external_key },
  ];

  return (
    <>
      <SecOpsHeader subtitle="Which scannable resources Qualys reports on, and which Qualys hosts the inventory does not hold." />
      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <MetricCard label="Scannable resources" metric={c.scannable_total} display={(m) => count(Number(m.value))} />
        <MetricCard
          label="With no Qualys match"
          metric={c.unmatched_total}
          display={(m) => count(Number(m.value))}
          band={Number(c.unmatched_total.value) > 0 ? "watch" : "healthy"}
          href="#unmatched"
        />
        <MetricCard
          label="Qualys hosts in quarantine"
          metric={c.quarantined_total}
          display={(m) => count(Number(m.value))}
          band={Number(c.quarantined_total.value) > 0 ? "watch" : "healthy"}
          href="#quarantine"
        />
      </div>

      <section aria-labelledby="coverage-heading" className="card mb-4 p-4">
        <h2 id="coverage-heading" className="mb-2 section-title">
          Coverage by customer
        </h2>
        <p className="mb-3 text-sm text-muted">
          Scannable means a resource of type {types}. A resource is matched when a Qualys finding is linked to it, or its inventory record carries a Qualys
          asset id. Matching follows the LLD: external key, then the host name without its domain plus an address, then the name alone. The connector reads
          host detections, so a host Qualys scanned clean looks the same as one it never scanned.
        </p>
        <BarTable
          caption="Scanner coverage by customer"
          labelHeader="Customer"
          valueHeader="Coverage"
          extra={["Scannable", "Detected", "Asset id only", "No match", "Hosts in quarantine"]}
          rows={c.by_customer.map((x) => ({
            key: x.tenant.id,
            label: x.tenant.name,
            size: x.coverage_percent ? Number(x.coverage_percent.value) : 0,
            text: x.coverage_percent ? `${decimal(x.coverage_percent.value, 1)}%` : "—",
            cells: [
              count(Number(x.scannable.value)),
              count(Number(x.detected.value)),
              count(Number(x.asset_id_only.value)),
              count(Number(x.unmatched.value)),
              count(Number(x.quarantined_hosts.value)),
            ],
            title: x.coverage_percent ? `${x.tenant.name}: ${decimal(x.coverage_percent.value, 1)}% covered` : `${x.tenant.name}: nothing scannable`,
          }))}
        />
        <div className="mt-2">
          <Provenance metric={c.scannable_total} />
        </div>
      </section>

      <section id="unmatched" aria-labelledby="unmatched-heading" className="mb-4">
        <h2 id="unmatched-heading" className="mb-1 section-title">
          Resources with no Qualys host match
        </h2>
        <p className="mb-2 text-sm text-muted">
          A possible host shares the resource’s host name or an address with a Qualys host in quarantine. It is a hint for a person to confirm, never a link.
        </p>
        <DataTable
          id="secops-unmatched"
          caption="Scannable resources with no Qualys host match"
          rows={c.unmatched_resources}
          rowKey={(r) => r.id}
          columns={resourceColumns}
          facets={[{ key: "customer", label: "Customer", value: (r) => r.tenant.name }]}
          role={me.role}
          empty={{ title: "Every scannable resource has a Qualys match", next: "New resources appear here until Qualys reports on them." }}
        />
      </section>

      <section id="quarantine" aria-labelledby="quarantine-heading">
        <h2 id="quarantine-heading" className="mb-1 section-title">
          Qualys hosts with no resource match
        </h2>
        <p className="mb-2 text-sm text-muted">
          The resource quarantine: hosts Qualys reports that matched no resource in the customer’s inventory, with how often they were seen. Their findings are
          kept, unlinked, and count in the vulnerability figures.
        </p>
        <DataTable
          id="secops-quarantine"
          caption="Qualys hosts in the resource quarantine"
          rows={c.quarantined_hosts}
          rowKey={(h) => h.id}
          columns={hostColumns}
          facets={[{ key: "customer", label: "Customer", value: (h) => h.tenant.name }]}
          role={me.role}
          empty={{ title: "No Qualys host is in quarantine", next: "A host appears here when Qualys reports one the inventory does not hold." }}
        />
      </section>
    </>
  );
}

// --- posture score ---------------------------------------------------------------------

const PART_LABEL: Record<string, string> = { sla_compliance: "SLA compliance", coverage: "Coverage", exposure: "Exposure" };

// Higher is better: the band is on the inverse of the score's distance from 100.
function scoreBand(metric: MetricDecimal | null): Band {
  return metric ? band(100 - Number(metric.value), [10, 25, 50]) : "unknown";
}

export function SecOpsPostureScore() {
  useAssistantContext({ screen: "posture" });
  const me = useMe();
  const { tenantId } = useCustomer();
  const loaded = useApi((token) => apiGet(token, "/v1/secops/posture-score", {}, customerQuery(tenantId)), `secops-score:${tenantId ?? "all"}`);
  if (loaded.status === "loading") return <Loading what="posture scores" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="posture scores" />;
  const s = loaded.data;
  const columns: Column<CustomerPostureScore>[] = [
    { key: "customer", header: "Customer", value: (c) => c.tenant.name },
    {
      key: "score",
      header: "Posture score",
      value: (c) => (c.score ? Number(c.score.value) : null),
      render: (c) => (c.score ? <HealthBadge band={scoreBand(c.score)} label={points(c.score)} /> : <span className="text-muted">No scanner data</span>),
    },
    { key: "sla", header: "SLA compliance", value: (c) => (c.sla_compliance ? Number(c.sla_compliance.value) : null), render: (c) => points(c.sla_compliance), className: "tabular-nums" },
    { key: "coverage", header: "Coverage", value: (c) => (c.coverage ? Number(c.coverage.value) : null), render: (c) => points(c.coverage), className: "tabular-nums" },
    { key: "exposure", header: "Exposure", value: (c) => (c.exposure ? Number(c.exposure.value) : null), render: (c) => points(c.exposure), className: "tabular-nums" },
    { key: "hosts", header: "Hosts reported", value: (c) => Number(c.hosts.value), className: "tabular-nums" },
    { key: "open", header: "Open findings", value: (c) => Number(c.open_findings.value), className: "tabular-nums" },
  ];
  const sample = s.customers.find((c) => c.score)?.score ?? null;
  return (
    <>
      <SecOpsHeader subtitle="A posture score per customer, from 0 to 100, with the parts it is made of." />
      <section aria-labelledby="formula-heading" className="card mb-4 border border-dashed border-modelled p-4">
        <h2 id="formula-heading" className="mb-2 flex flex-wrap items-center gap-2 section-title">
          How the score is made <ModelledMark />
        </h2>
        <p className="text-sm text-ink">{s.formula}</p>
        <ul className="mt-2 flex flex-wrap gap-2 text-sm">
          {s.weights.map((w) => (
            <li key={w.part} className="rounded-control bg-surface-raised px-2 py-1">
              {PART_LABEL[w.part] ?? w.part}: {decimal(String(Number(w.weight.value) * 100), 0)}%
            </li>
          ))}
        </ul>
        <p className="mt-2 text-sm text-muted">
          The platform’s model, not a Qualys figure or a contract measure (ADR 0036). It uses the modelled remediation targets on the Vulnerabilities tab.
        </p>
        {sample ? (
          <div className="mt-2">
            <Provenance metric={sample} />
          </div>
        ) : null}
      </section>
      <DataTable
        id="secops-score"
        caption="Posture score by customer"
        rows={s.customers}
        rowKey={(c) => c.tenant.id}
        columns={columns}
        role={me.role}
        empty={{ title: "No customers in your scope", next: "Ask a platform administrator to assign you the customers you work on." }}
      />
    </>
  );
}
