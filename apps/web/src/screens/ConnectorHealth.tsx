"use client";

// Connector health (step 15; LLD, Connector contract): every scheduled source and
// entity, when it last succeeded, how its last day of runs went, what it holds in
// quarantine and dead letters, and schema drift waiting for a person. The band is
// the API's (cloudops_api.connectors.assess), on the console's one scale; this
// screen names why, and never computes a band of its own. Staff only.

import Link from "next/link";
import { useState } from "react";

import type { ConnectorHealth as Connector, ConnectorHealthList, DriftList, DriftView } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { ApiError, apiGet, apiPostEmpty } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { count, dateTime, decimal, duration, percent } from "@/lib/format";
import type { Band } from "@/lib/health";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

type Reason = Connector["reasons"][number];
type Message = { kind: "ok" | "error"; text: string } | null;

const REASON: Record<Reason, string> = {
  outage: "no success for more than the outage threshold",
  late: "no success within its interval",
  never_run: "has not run yet",
  no_success: "has run, but never succeeded",
  failing: "most runs failed in the last 24 hours",
  failed_runs: "a run failed in the last 24 hours",
  last_run_failed: "the last run failed",
  rejected_records: "rejected malformed records in the last 24 hours",
  dead_letters: "dead letters wait for a replay",
  schema_drift: "schema drift waits to be acknowledged",
};

/** The badge's words: the most serious cause, so the band is never colour alone. */
function status(c: Connector): string {
  if (c.timeliness === "outage") return "Outage";
  if (c.timeliness === "late") return "Late";
  if (c.reasons.includes("failing")) return "Failing";
  if (c.band === "watch") return "Needs attention";
  if (c.timeliness === "never_run") return "Never run";
  return "Healthy";
}

const BAND_ORDER: Record<Band, number> = { critical: 0, "at-risk": 1, watch: 2, unknown: 3, healthy: 4 };

function connectorName(source: string, entity: string): string {
  return `${source} · ${entity.replace(/_/g, " ")}`;
}

function Feedback({ message }: { message: Message }) {
  if (!message) return null;
  return (
    <p role={message.kind === "error" ? "alert" : "status"} className={`text-sm ${message.kind === "error" ? "text-band-critical" : "text-band-healthy"}`}>
      {message.text}
    </p>
  );
}

function AcknowledgeButton({ drift, onDone }: { drift: DriftView; onDone: (message: Message) => void }) {
  const { token } = useAuth();
  const [busy, setBusy] = useState(false);
  const name = `${drift.field} on ${connectorName(drift.source_system, drift.entity)}`;

  async function acknowledge() {
    setBusy(true);
    try {
      await apiPostEmpty(await token(), "/v1/connectors/drift/acknowledge", {}, { source: drift.source_system, entity: drift.entity, field: drift.field });
      onDone({ kind: "ok", text: `Drift on ${name} acknowledged. It is raised again if the source changes this field again.` });
    } catch (error) {
      const text =
        error instanceof ApiError && error.status === 404
          ? `Drift on ${name} was already acknowledged by someone else.`
          : error instanceof ApiError && error.status === 403
            ? "Acknowledging drift is not available to your role."
            : "The acknowledgement was not saved. Try again in a moment.";
      onDone({ kind: error instanceof ApiError && error.status === 404 ? "ok" : "error", text });
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => void acknowledge()}
      aria-label={`Acknowledge ${name}`}
      className="rounded-control border border-line px-2 py-1 text-sm font-medium text-ink disabled:opacity-60"
    >
      Acknowledge
    </button>
  );
}

interface Data {
  health: ConnectorHealthList;
  drift: DriftList;
}

async function load(token: string): Promise<Data> {
  const [health, drift] = await Promise.all([apiGet(token, "/v1/connectors/health"), apiGet(token, "/v1/connectors/drift")]);
  return { health, drift };
}

export function ConnectorHealth() {
  const me = useMe();
  const loaded = useApi(load, "connectors");
  const [message, setMessage] = useState<Message>(null);
  const manage = can(me, "connectors.manage");
  const mapping = can(me, "mapping.manage");

  if (loaded.status === "loading") return <Loading what="connector health" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="connector health" />;
  const { health, drift } = loaded.data;
  const rules = health.rules;
  const late = rules.late_after_intervals.value;
  const outage = rules.outage_after_intervals.value;

  function acknowledged(next: Message) {
    setMessage(next);
    loaded.reload();
  }

  const columns: Column<Connector>[] = [
    {
      key: "connector",
      header: "Connector",
      value: (c) => connectorName(c.source_system, c.entity),
      render: (c) => (
        <span className="font-medium">
          {c.source_system} <span className="text-muted">· {c.entity.replace(/_/g, " ")}</span>
        </span>
      ),
      sortable: true,
    },
    {
      key: "health",
      header: "Health",
      value: (c) => BAND_ORDER[c.band],
      render: (c) => (
        <span className="flex flex-col items-start gap-1">
          <HealthBadge band={c.band} label={status(c)} />
          {c.reasons.length > 0 ? <span className="text-xs text-muted">{c.reasons.map((r) => REASON[r]).join("; ")}</span> : null}
        </span>
      ),
      sortable: true,
    },
    {
      key: "success",
      header: "Last success",
      value: (c) => c.last_success_age?.value ?? null,
      render: (c) =>
        c.last_success_age ? (
          <span className="tabular-nums">
            <span title={dateTime(c.last_success_at)}>{duration(c.last_success_age.value)} ago</span>
            {c.intervals_since_success ? <span className="block text-xs text-muted">{decimal(c.intervals_since_success.value, 1)} intervals</span> : null}
          </span>
        ) : (
          <span className="text-muted">Never</span>
        ),
      sortable: true,
    },
    {
      key: "every",
      header: "Expected every",
      value: (c) => c.expected_every.value,
      render: (c) => duration(c.expected_every.value),
      sortable: true,
      className: "tabular-nums",
    },
    {
      key: "runs",
      header: "Failed runs, 24 h",
      value: (c) => c.failed_runs_24h.value,
      render: (c) =>
        c.failed_run_rate_24h ? (
          <span className="tabular-nums">
            {count(c.failed_runs_24h.value)} of {count(c.runs_24h.value)}
            <span className="block text-xs text-muted">{percent(c.failed_run_rate_24h.value)}</span>
          </span>
        ) : (
          <span className="text-muted">No runs</span>
        ),
      sortable: true,
    },
    {
      key: "rejected",
      header: "Rejected records, 24 h",
      value: (c) => c.rejected_24h.value,
      render: (c) =>
        c.rejected_record_rate_24h ? (
          <span className="tabular-nums">
            {count(c.rejected_24h.value)} of {count(c.records_24h.value)}
            <span className="block text-xs text-muted">{percent(c.rejected_record_rate_24h.value)}</span>
          </span>
        ) : (
          <span className="text-muted">No records</span>
        ),
      sortable: true,
    },
    {
      key: "quarantine",
      header: "Quarantine",
      value: (c) => c.alias_quarantine_depth.value + c.resource_quarantine_depth.value,
      render: (c) => (
        <span className="tabular-nums">
          {mapping && c.alias_quarantine_depth.value > 0 ? (
            <Link href="/mapping" className="font-medium text-accent underline" aria-label={`${count(c.alias_quarantine_depth.value)} names held from ${c.source_system}: map them`}>
              {count(c.alias_quarantine_depth.value)} names
            </Link>
          ) : (
            <span>{count(c.alias_quarantine_depth.value)} names</span>
          )}
          <span className="block text-xs text-muted">{count(c.resource_quarantine_depth.value)} resources</span>
        </span>
      ),
      sortable: true,
    },
    { key: "dead", header: "Dead letters", value: (c) => c.dead_letters_open.value, render: (c) => count(c.dead_letters_open.value), sortable: true, className: "tabular-nums" },
    { key: "drift", header: "Open drift", value: (c) => c.drift_open.value, render: (c) => count(c.drift_open.value), sortable: true, className: "tabular-nums" },
  ];

  const driftColumns: Column<DriftView>[] = [
    { key: "connector", header: "Connector", value: (d) => connectorName(d.source_system, d.entity), sortable: true },
    { key: "field", header: "Field", value: (d) => d.field, render: (d) => <code className="break-all">{d.field}</code>, sortable: true },
    {
      key: "drift",
      header: "Change",
      value: (d) => d.drift,
      render: (d) => <HealthBadge band="watch" label={d.drift === "added" ? "New field" : "Field missing"} />,
      sortable: true,
    },
    { key: "since", header: "Last seen", value: (d) => d.drift_at, render: (d) => dateTime(d.drift_at), sortable: true },
    { key: "seen", header: "Sightings", value: (d) => d.occurrences.value, render: (d) => count(d.occurrences.value), sortable: true, className: "tabular-nums" },
    ...(manage
      ? [
          {
            key: "action",
            header: "Action",
            value: () => null,
            render: (d: DriftView) => <AcknowledgeButton drift={d} onDone={acknowledged} />,
            sortable: false,
          } satisfies Column<DriftView>,
        ]
      : []),
  ];

  return (
    <>
      <PageHeader
        title="Connectors"
        subtitle="Ingest health for every scheduled source: when it last succeeded, how its runs went over the last 24 hours, and what waits for a person."
      >
        <button type="button" onClick={loaded.reload} className="rounded-control border border-line px-3 py-1.5 text-sm font-medium text-ink">
          Refresh
        </button>
      </PageHeader>

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard label="Outages" metric={health.outages} band={health.outages.value > 0 ? "critical" : "healthy"} display={(m) => count(Number(m.value))} />
        <MetricCard label="Late or failing" metric={health.late} band={health.late.value > 0 ? "at-risk" : "healthy"} display={(m) => count(Number(m.value))} />
        <MetricCard label="Need attention" metric={health.attention} band={health.attention.value > 0 ? "watch" : "healthy"} display={(m) => count(Number(m.value))} />
        <MetricCard label="Never run" metric={health.never_run} display={(m) => count(Number(m.value))} />
      </div>

      <details className="mb-6 rounded-card border border-line bg-surface p-3 text-sm text-ink">
        <summary className="cursor-pointer font-medium">How health is decided</summary>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-muted">
          <li>
            <strong className="text-ink">Late</strong>: more than {late} interval{late === 1 ? "" : "s"} since the last successful scheduled run.
          </li>
          <li>
            <strong className="text-ink">Outage</strong>: more than {outage} intervals, or runs that never succeeded. On-call is paged (P2).
          </li>
          <li>
            <strong className="text-ink">Failing</strong>: {percent(rules.failing_run_rate.value, 0)} or more of the last 24 hours’ runs failed.
          </li>
          <li>
            <strong className="text-ink">Needs attention</strong>: a failed run, rejected records or open dead letters in the last 24 hours, or schema drift
            nobody has acknowledged.
          </li>
          <li>
            <strong className="text-ink">Never run</strong>: scheduled, but no run is recorded yet.
          </li>
        </ul>
        <p className="mt-2 text-xs text-muted">
          Replays do not count as scheduled runs. Resource quarantine counts only your customers; held names have no customer and are counted per source.
        </p>
      </details>

      <div className="space-y-6">
        <section aria-labelledby="health-heading">
          <h2 id="health-heading" className="mb-2 text-lg font-semibold text-ink">
            Sources
          </h2>
          <div className="mb-2">
            <Provenance metric={health.outages} />
          </div>
          <DataTable
            id="connector-health"
            caption="Connector health"
            rows={health.items}
            rowKey={(c) => `${c.source_system}:${c.entity}`}
            columns={columns}
            facets={[
              { key: "source", label: "Source", value: (c) => c.source_system },
              { key: "status", label: "Status", value: (c) => status(c) },
            ]}
            role={me.role}
            initial={{ sort: { key: "health", dir: "asc" } }}
            empty={{ title: "No connectors scheduled", next: "Connectors appear here once their schedule is configured by a platform administrator." }}
          />
        </section>

        <section aria-labelledby="drift-heading">
          <h2 id="drift-heading" className="mb-2 text-lg font-semibold text-ink">
            Schema drift ({count(drift.open.value)})
          </h2>
          <p className="mb-2 text-sm text-muted">
            A field a source started sending (kept, unmapped, beside the record) or an expected field missing from a whole page. Field names only, never
            values.{" "}
            {manage
              ? "Acknowledge once the mapping is updated, or once you decide it needs no change."
              : "A platform administrator acknowledges drift once the mapping is updated."}
          </p>
          <div className="mb-2 space-y-2">
            <Feedback message={message} />
          </div>
          <DataTable
            id="connector-drift"
            caption="Schema drift"
            rows={drift.items}
            rowKey={(d) => `${d.source_system}:${d.entity}:${d.field}`}
            columns={driftColumns}
            facets={[{ key: "source", label: "Source", value: (d) => d.source_system }]}
            role={me.role}
            empty={{ title: "No open drift", next: "Every source sends the fields its mapping expects. New drift appears here when a source changes." }}
          />
        </section>
      </div>
    </>
  );
}
