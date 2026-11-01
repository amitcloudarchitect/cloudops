"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import type { WorkItemSummary } from "@cloudops/contracts";

import { DataTable, type Column, type Facet, type View } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { date } from "@/lib/format";
import { priorityBand } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const KIND_LABEL: Record<string, string> = {
  incident: "Incident",
  change_task: "Change task",
  service_request: "Request",
  project_task: "Project task",
};

const ROUTE_LABEL: Record<string, string> = { human: "Needs a person", ready_for_planning: "Ready for planning" };

// Preset views per role class (HLD: saved views per role). Keyed by the role's
// name as /v1/me reports it; a role without presets starts from "All".
const PRESETS: Record<string, View[]> = {
  Engineer: [
    { name: "Needs a person", query: "", facets: { triage: ROUTE_LABEL.human ?? "" }, sort: { key: "opened", dir: "asc" } },
    { name: "Incidents, oldest first", query: "", facets: { kind: "Incident" }, sort: { key: "opened", dir: "asc" } },
  ],
  "Delivery lead": [
    { name: "SLA breached", query: "", facets: { sla: "Breached" }, sort: { key: "opened", dir: "asc" } },
    { name: "Needs a person", query: "", facets: { triage: ROUTE_LABEL.human ?? "" }, sort: null },
  ],
  "Customer viewer": [{ name: "Incidents", query: "", facets: { kind: "Incident" }, sort: { key: "opened", dir: "desc" } }],
  "Customer engineer": [
    { name: "Incidents", query: "", facets: { kind: "Incident" }, sort: { key: "opened", dir: "desc" } },
    { name: "SLA breached", query: "", facets: { sla: "Breached" }, sort: { key: "opened", dir: "asc" } },
  ],
};

type Status = "open" | "closed" | "all";

export function WorkQueue() {
  useAssistantContext({ screen: "work" });
  const me = useMe();
  const params = useSearchParams();
  const [status, setStatus] = useState<Status>("open");
  const staff = me.kind === "staff";
  const loaded = useApi((token) => apiGet(token, "/v1/work-items", {}, { status }), status);

  const columns: Column<WorkItemSummary>[] = [
    {
      key: "number",
      header: "Number",
      value: (w) => w.number,
      render: (w) => (
        <Link href={`/work/${encodeURIComponent(w.number)}`} className="link font-medium">
          {w.number}
        </Link>
      ),
    },
    { key: "kind", header: "Kind", value: (w) => KIND_LABEL[w.kind] ?? w.kind },
    {
      key: "priority",
      header: "Priority",
      value: (w) => w.priority,
      render: (w) => <HealthBadge band={priorityBand(w.priority)} label={w.priority ?? "None"} />,
    },
    { key: "summary", header: "Summary", value: (w) => w.short_description, className: "min-w-64" },
    ...(me.tenants.length > 1 ? [{ key: "customer", header: "Customer", value: (w: WorkItemSummary) => w.tenant.name }] : []),
    // Getronics workgroups are staff-only: the database refuses them to a customer (ADR 0018).
    ...(staff ? [{ key: "workgroup", header: "Workgroup", value: (w: WorkItemSummary) => w.workgroup }] : []),
    { key: "ci", header: "Configuration item", value: (w) => w.configuration_item },
    { key: "opened", header: "Opened", value: (w) => w.opened_at, render: (w) => date(w.opened_at) },
    {
      key: "sla",
      header: "SLA",
      value: (w) => (w.sla_breached ? "Breached" : w.sla_breached === false ? "Within" : null),
      render: (w) =>
        w.sla_breached ? <HealthBadge band="critical" label="Breached" /> : w.sla_breached === false ? "Within" : "—",
    },
    ...(staff
      ? [
          {
            key: "triage",
            header: "Triage",
            value: (w: WorkItemSummary) => (w.triage_route ? (ROUTE_LABEL[w.triage_route] ?? w.triage_route) : null),
          },
        ]
      : []),
  ];

  const facets: Facet<WorkItemSummary>[] = [
    { key: "kind", label: "Kind", value: (w) => KIND_LABEL[w.kind] ?? w.kind },
    ...(staff ? [{ key: "workgroup", label: "Workgroup", value: (w: WorkItemSummary) => w.workgroup }] : []),
    ...(me.tenants.length > 1 ? [{ key: "customer", label: "Customer", value: (w: WorkItemSummary) => w.tenant.name }] : []),
    { key: "sla", label: "SLA", value: (w) => (w.sla_breached ? "Breached" : null) },
    ...(staff
      ? [{ key: "triage", label: "Triage", value: (w: WorkItemSummary) => (w.triage_route ? (ROUTE_LABEL[w.triage_route] ?? null) : null) }]
      : []),
  ];

  // Links from elsewhere (the customer overview's evidence) arrive as facets.
  const initialFacets: Record<string, string> = {};
  for (const key of ["kind", "customer", "sla", "workgroup"]) {
    const value = params.get(key);
    if (value) initialFacets[key] = value;
  }

  return (
    <>
      <PageHeader
        title="Work queue"
        subtitle="Incidents, changes, requests and tasks from ServiceNow, for your customers."
      >
        <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
          Show
          <select
            className="field"
            value={status}
            onChange={(e) => setStatus(e.target.value as Status)}
          >
            <option value="open">Open</option>
            <option value="closed">Closed</option>
            <option value="all">All</option>
          </select>
        </label>
      </PageHeader>
      {loaded.status === "loading" ? <Loading what="the work queue" /> : null}
      {loaded.status === "error" ? <LoadError error={loaded.error} what="work queue" /> : null}
      {loaded.status === "ready" ? (
        <>
          <div className="mb-2">
            <Provenance metric={loaded.data.count} />
          </div>
          <DataTable
            key={`${status}-${JSON.stringify(initialFacets)}`}
            id="work-queue"
            caption="Work items"
            rows={loaded.data.items}
            rowKey={(w) => `${w.tenant.id}:${w.number}`}
            columns={columns}
            facets={facets}
            presets={PRESETS[me.role] ?? []}
            role={me.role}
            initial={Object.keys(initialFacets).length ? { name: "", facets: initialFacets } : undefined}
            empty={{
              title: status === "open" ? "No open work for your customers" : "No work items",
              next:
                status === "open"
                  ? "New tickets appear as ServiceNow syncs, every few minutes. Choose Show: All to see closed work."
                  : "Tickets appear here once the ServiceNow connector has run for your customers.",
            }}
          />
          {loaded.data.count.value > loaded.data.items.length ? (
            <p className="mt-2 text-sm text-muted">
              The newest {loaded.data.items.length} of {loaded.data.count.value} are shown. Narrow the list with Show, or search
              by number.
            </p>
          ) : null}
        </>
      ) : null}
    </>
  );
}
