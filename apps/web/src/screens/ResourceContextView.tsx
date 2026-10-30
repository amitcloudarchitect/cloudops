"use client";

import Link from "next/link";

import type { ResourceContext, WorkItemSummary } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { date } from "@/lib/format";
import { band, priorityBand, THRESHOLDS } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

type Vulnerability = NonNullable<ResourceContext["vulnerabilities"]>[number];

function WorkList({ items, empty }: { items: WorkItemSummary[]; empty: string }) {
  if (items.length === 0) return <p className="text-sm text-muted">{empty}</p>;
  return (
    <ul className="divide-y divide-line text-sm">
      {items.map((w) => (
        <li key={w.number} className="flex flex-wrap items-center gap-2 py-2">
          <Link href={`/work/${encodeURIComponent(w.number)}`} className="font-medium text-accent underline">
            {w.number}
          </Link>
          <HealthBadge band={priorityBand(w.priority)} label={w.priority ?? "None"} />
          <span className="text-ink">{w.short_description}</span>
          <span className="text-muted">{date(w.opened_at)}</span>
        </li>
      ))}
    </ul>
  );
}

export function ResourceContextView({ id }: { id: string }) {
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/resources/{resource_id}/context", { resource_id: id }), id);
  if (loaded.status === "loading") return <Loading what="the resource" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="resource" />;
  const ctx = loaded.data;
  const r = ctx.resource;
  const tags = "tags" in r ? r.tags : null;

  const vulnColumns: Column<Vulnerability>[] = [
    { key: "qid", header: "QID", value: (v) => v.qid },
    { key: "title", header: "Title", value: (v) => v.title, className: "min-w-64" },
    {
      key: "severity",
      header: "Severity",
      value: (v) => v.severity,
      render: (v) => <HealthBadge band={band(v.severity, THRESHOLDS.vulnerabilitySeverity)} label={`Severity ${v.severity}`} />,
    },
    { key: "first", header: "First detected", value: (v) => v.first_detected, render: (v) => date(v.first_detected) },
    {
      key: "status",
      header: "Status",
      value: (v) => (v.remediated_at ? "Remediated" : "Open"),
    },
  ];

  return (
    <>
      <PageHeader title={r.name} subtitle={`${r.type}${r.region ? ` · ${r.region}` : ""}${r.state ? ` · ${r.state}` : ""}`} />
      <div className="grid gap-4 lg:grid-cols-3">
        <MetricCard
          label="Open vulnerabilities"
          metric={ctx.open_vulnerabilities}
          band={ctx.open_vulnerabilities ? band(ctx.open_vulnerabilities.value, THRESHOLDS.criticalVulnerabilities) : undefined}
          href="#vulnerabilities"
        />
        <section aria-labelledby="metrics-heading" className="rounded-card border border-line bg-surface p-4 lg:col-span-2">
          <h2 id="metrics-heading" className="text-sm font-medium text-muted">
            Live metrics
          </h2>
          {ctx.metrics.available ? null : (
            <p className="mt-1 text-sm text-ink">
              {ctx.metrics.reason} Until then, use the cloud provider’s monitoring for this resource.
            </p>
          )}
        </section>
      </div>

      {tags ? (
        <section aria-labelledby="tags-heading" className="mt-4 rounded-card border border-line bg-surface p-4">
          <h2 id="tags-heading" className="mb-2 text-lg font-semibold text-ink">
            Tags
          </h2>
          {Object.keys(tags).length === 0 ? (
            <p className="text-sm text-muted">No tags. Tagging policy expects owner, environment and cost centre; raise it with the customer’s cloud owner.</p>
          ) : (
            <dl className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {Object.entries(tags).map(([k, v]) => (
                <div key={k} className="rounded-control bg-surface-raised px-2 py-1 text-sm">
                  <dt className="font-mono text-xs text-muted">{k}</dt>
                  <dd className="break-words text-ink">{v}</dd>
                </div>
              ))}
            </dl>
          )}
        </section>
      ) : null}

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <section aria-labelledby="incidents-heading" className="rounded-card border border-line bg-surface p-4">
          <h2 id="incidents-heading" className="mb-2 text-lg font-semibold text-ink">
            Open incidents
          </h2>
          {ctx.open_incidents ? (
            <WorkList items={ctx.open_incidents} empty="No open incidents on this resource." />
          ) : (
            <p className="text-sm text-muted">Tickets are not available to your role.</p>
          )}
        </section>
        <section aria-labelledby="changes-heading" className="rounded-card border border-line bg-surface p-4">
          <h2 id="changes-heading" className="mb-2 text-lg font-semibold text-ink">
            Recent changes (30 days)
          </h2>
          {ctx.recent_changes ? (
            <WorkList items={ctx.recent_changes} empty="No change tasks in the last 30 days." />
          ) : (
            <p className="text-sm text-muted">Tickets are not available to your role.</p>
          )}
        </section>
      </div>

      <section id="vulnerabilities" aria-labelledby="vuln-heading" className="mt-4">
        <h2 id="vuln-heading" className="mb-2 text-lg font-semibold text-ink">
          Vulnerabilities
        </h2>
        {ctx.vulnerabilities ? (
          <DataTable
            id="resource-vulnerabilities"
            caption="Vulnerabilities on this resource"
            rows={ctx.vulnerabilities}
            rowKey={(v) => `${v.qid}:${v.first_detected}`}
            columns={vulnColumns}
            facets={[{ key: "status", label: "Status", value: (v) => (v.remediated_at ? "Remediated" : "Open") }]}
            role={me.role}
            initial={{ facets: { status: "Open" }, sort: { key: "severity", dir: "desc" } }}
            empty={{ title: "No findings", next: "Qualys has reported nothing for this resource. If it is not being scanned, check its agent." }}
          />
        ) : (
          <EmptyState title="Not available to your role" next="Vulnerability findings need the vulnerabilities permission." />
        )}
      </section>
    </>
  );
}
