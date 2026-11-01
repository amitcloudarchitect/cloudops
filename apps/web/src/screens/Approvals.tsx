"use client";

import Link from "next/link";

import type { PendingChange } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { date } from "@/lib/format";
import { priorityBand } from "@/lib/health";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

// Changes awaiting the customer (the approver's landing view, ADR 0018).
// Read-only: approving and rejecting arrive with the approval state machine,
// which lives in the workflow engine, not in this screen.
export function Approvals() {
  useAssistantContext({ screen: "approvals" });
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/approvals"), "approvals");
  if (loaded.status === "loading") return <Loading what="changes awaiting approval" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="changes awaiting approval" />;
  const tickets = can(me, "tickets.read");
  const columns: Column<PendingChange>[] = [
    {
      key: "number",
      header: "Change task",
      value: (c) => c.number,
      render: (c) =>
        tickets ? (
          <Link href={`/work/${encodeURIComponent(c.number)}`} className="link font-medium">
            {c.number}
          </Link>
        ) : (
          c.number
        ),
    },
    { key: "request", header: "Change request", value: (c) => c.change_request },
    { key: "summary", header: "Summary", value: (c) => c.short_description, className: "min-w-64" },
    ...(me.tenants.length > 1 ? [{ key: "customer", header: "Customer", value: (c: PendingChange) => c.tenant.name }] : []),
    {
      key: "priority",
      header: "Priority",
      value: (c) => c.priority,
      render: (c) => <HealthBadge band={priorityBand(c.priority)} label={c.priority ?? "None"} />,
    },
    { key: "ci", header: "Configuration item", value: (c) => c.configuration_item },
    { key: "state", header: "State", value: (c) => c.state },
    { key: "opened", header: "Opened", value: (c) => c.opened_at, render: (c) => date(c.opened_at) },
  ];
  return (
    <>
      <PageHeader title="Approvals" subtitle="Open changes that affect your services, oldest first." />
      <p role="note" className="mb-4 card p-3 text-sm text-muted">
        Read-only for now. Approving or rejecting a change from here arrives with automated remediation; until then,
        approve in ServiceNow as today.
      </p>
      <div className="mb-2">
        <Provenance metric={loaded.data.count} />
      </div>
      <DataTable
        id="approvals"
        caption="Changes awaiting approval"
        rows={loaded.data.items}
        rowKey={(c) => c.number}
        columns={columns}
        role={me.role}
        empty={{ title: "Nothing awaiting approval", next: "Open changes that affect your services appear here." }}
      />
    </>
  );
}
