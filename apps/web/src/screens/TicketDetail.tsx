"use client";

import Link from "next/link";

import type { WorkItemDetail } from "@cloudops/contracts";

import { EmptyState } from "@/components/EmptyState";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { priorityBand } from "@/lib/health";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

import { TriagePanel } from "./TriagePanel";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium text-muted">{label}</dt>
      <dd className="text-sm text-ink">{children ?? "—"}</dd>
    </div>
  );
}

function hours(seconds: number): string {
  return `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min`;
}

export function TicketDetail({ number }: { number: string }) {
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/work-items/{number}", { number }), number);

  if (loaded.status === "loading") return <Loading what={number} />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="ticket" />;
  const w: WorkItemDetail = loaded.data;
  const staff = me.kind === "staff";

  return (
    <>
      <p className="mb-2 text-sm">
        <Link href="/work" className="text-accent underline">
          ← Work queue
        </Link>
      </p>
      <PageHeader title={w.number} subtitle={w.short_description ?? undefined}>
        <HealthBadge band={priorityBand(w.priority)} label={`Priority ${w.priority ?? "none"}`} />
      </PageHeader>
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
        <section aria-labelledby="ticket-heading" className="rounded-card border border-line bg-surface p-4">
          <h2 id="ticket-heading" className="mb-3 text-lg font-semibold text-ink">
            Ticket
          </h2>
          <dl className="grid grid-cols-2 gap-3">
            <Field label="Customer">{w.tenant.name}</Field>
            <Field label="State">{w.state}</Field>
            <Field label="Workgroup">{w.workgroup}</Field>
            {staff ? <Field label="Assigned to">{w.assigned_to}</Field> : null}
            <Field label="Opened">{dateTime(w.opened_at)}</Field>
            <Field label="Closed">{w.closed_at ? dateTime(w.closed_at) : "Open"}</Field>
            <Field label="SLA">{w.sla_breached ? <HealthBadge band="critical" label="Breached" /> : w.sla_breached === false ? "Within" : "—"}</Field>
            <Field label="Change request">{w.change_request}</Field>
            <Field label="Configuration item">
              {w.resource_id && can(me, "inventory.read") ? (
                <Link href={`/resources/${w.resource_id}`} className="text-accent underline">
                  {w.configuration_item ?? "Resource"}
                </Link>
              ) : (
                w.configuration_item
              )}
            </Field>
            <Field label="Resolve time">{w.resolve_time ? hours(w.resolve_time.value) : null}</Field>
            <Field label="Closure code">{w.closure_code}</Field>
          </dl>
          {w.close_notes ? (
            <div className="mt-3">
              <h3 className="text-xs font-medium text-muted">Close notes</h3>
              <p className="whitespace-pre-wrap text-sm text-ink">{w.close_notes}</p>
            </div>
          ) : null}
        </section>
        {staff ? (
          w.triage ? (
            <TriagePanel number={w.number} triage={w.triage} onReviewed={loaded.reload} />
          ) : (
            <EmptyState
              title="Not triaged yet"
              next="The agents triage new incidents as they arrive. Work this ticket as usual; triage will appear here when it runs."
            />
          )
        ) : null}
      </div>
    </>
  );
}
