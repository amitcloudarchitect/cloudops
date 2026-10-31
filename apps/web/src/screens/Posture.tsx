"use client";

import Link from "next/link";

import type { OpenFinding } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { count, date } from "@/lib/format";
import { band, THRESHOLDS } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const SEVERITY_LABEL: Record<number, string> = { 5: "Urgent", 4: "Critical", 3: "Serious", 2: "Medium", 1: "Minimal" };
const AGE_LABEL: Record<string, string> = { "0-30": "Open up to 30 days", "31-90": "Open 31 to 90 days", "91+": "Open over 90 days" };

// Security posture (the security officer's landing view, ADR 0018): open
// vulnerabilities by severity and age, most severe and oldest first.
export function Posture() {
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/posture"), "posture");
  if (loaded.status === "loading") return <Loading what="security posture" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="security posture" />;
  const { by_severity, by_age, findings, open_total } = loaded.data;
  const columns: Column<OpenFinding>[] = [
    {
      key: "severity",
      header: "Severity",
      value: (f) => f.severity,
      render: (f) => <HealthBadge band={band(f.severity, THRESHOLDS.vulnerabilitySeverity)} label={`${f.severity} · ${SEVERITY_LABEL[f.severity]}`} />,
    },
    { key: "title", header: "Finding", value: (f) => f.title },
    { key: "qid", header: "QID", value: (f) => f.qid },
    {
      key: "asset",
      header: "Asset",
      value: (f) => f.asset_name,
      render: (f) =>
        f.resource_id ? (
          <Link href={`/resources/${f.resource_id}`} className="link font-medium">
            {f.asset_name ?? "Resource"}
          </Link>
        ) : (
          (f.asset_name ?? "—")
        ),
    },
    { key: "first", header: "First detected", value: (f) => f.first_detected, render: (f) => date(f.first_detected) },
    { key: "age", header: "Age (days)", value: (f) => Number(f.age.value), className: "tabular-nums" },
  ];
  return (
    <>
      <PageHeader title="Security posture" subtitle="Open vulnerability findings from Qualys, by severity and by how long they have been open." />
      <h2 className="sr-only">By severity</h2>
      <div className="mb-3 grid gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <MetricCard label="Open findings" metric={open_total} display={(m) => count(Number(m.value))} />
        {by_severity.map((s) => (
          <MetricCard
            key={s.severity}
            label={`Severity ${s.severity} · ${SEVERITY_LABEL[s.severity]}`}
            metric={s.open}
            display={(m) => count(Number(m.value))}
            band={s.severity >= 4 ? band(s.open.value, THRESHOLDS.criticalVulnerabilities) : undefined}
          />
        ))}
      </div>
      <h2 className="sr-only">By age</h2>
      <div className="mb-6 grid gap-3 sm:grid-cols-3">
        {by_age.map((a) => (
          <MetricCard key={a.band} label={AGE_LABEL[a.band] ?? a.band} metric={a.open} display={(m) => count(Number(m.value))} />
        ))}
      </div>
      <DataTable
        id="posture"
        caption="Open findings"
        rows={findings}
        rowKey={(f) => `${f.qid}:${f.asset_name ?? ""}:${f.first_detected}`}
        columns={columns}
        facets={[{ key: "severity", label: "Severity", value: (f) => `${f.severity} · ${SEVERITY_LABEL[f.severity]}` }]}
        role={me.role}
        empty={{ title: "No open findings", next: "Findings appear here when the scanner reports them, and leave when they are remediated." }}
      />
    </>
  );
}
