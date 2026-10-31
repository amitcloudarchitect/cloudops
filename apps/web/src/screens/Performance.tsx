"use client";

import type { MonthPerformance } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { count, decimal } from "@/lib/format";
import { band, THRESHOLDS } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const MONTH = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });

function attainment(m: MonthPerformance): string {
  return m.sla_attainment ? `${decimal(String(m.sla_attainment.value), 1)}%` : "—";
}

// Service performance (the VP or director's landing view, ADR 0018): SLA
// attainment and ticket volumes by month, and what is open now.
export function Performance() {
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/performance"), "performance");
  if (loaded.status === "loading") return <Loading what="service performance" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="service performance" />;
  const { months, open_incidents, sla_breached_open } = loaded.data;
  const columns: Column<MonthPerformance>[] = [
    { key: "month", header: "Month", value: (m) => m.month, render: (m) => MONTH.format(new Date(m.month)) },
    { key: "opened", header: "Incidents opened", value: (m) => Number(m.incidents_opened.value), className: "tabular-nums" },
    { key: "resolved", header: "Incidents resolved", value: (m) => Number(m.incidents_resolved.value), className: "tabular-nums" },
    {
      key: "sla",
      header: "Resolved within SLA",
      value: (m) => (m.sla_attainment ? Number(m.sla_attainment.value) : null),
      render: attainment,
      className: "tabular-nums",
    },
    { key: "changes", header: "Changes", value: (m) => Number(m.changes.value), className: "tabular-nums" },
    { key: "requests", header: "Service requests", value: (m) => Number(m.service_requests.value), className: "tabular-nums" },
  ];
  return (
    <>
      <PageHeader title="Performance" subtitle="Service levels and ticket volumes over the last six months, by calendar month (UTC)." />
      <div className="mb-6 grid gap-3 sm:grid-cols-2">
        <MetricCard
          label="Open incidents"
          metric={open_incidents}
          display={(m) => count(Number(m.value))}
          band={band(open_incidents.value, THRESHOLDS.openIncidents)}
          href="/work?kind=Incident"
        />
        <MetricCard
          label="SLA breached, open"
          metric={sla_breached_open}
          display={(m) => count(Number(m.value))}
          band={band(sla_breached_open.value, THRESHOLDS.slaBreachedOpen)}
          href="/work?sla=Breached"
        />
      </div>
      <DataTable
        id="performance"
        caption="Monthly performance"
        rows={[...months].reverse()}
        rowKey={(m) => m.month}
        columns={columns}
        role={me.role}
        empty={{ title: "No tickets in the last six months", next: "Figures appear once tickets are synced from ServiceNow." }}
      />
    </>
  );
}
