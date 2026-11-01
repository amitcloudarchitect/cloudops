"use client";

// One finding (ADR 0036): what Qualys says about it, how it stands against its
// modelled target, the resource it was matched to and the tickets on that
// resource. A finding on a host that matched no resource says so: there is then
// nothing to link a ticket through, which is not the same as no tickets.

import Link from "next/link";

import { EmptyState } from "@/components/EmptyState";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { ModelledMark } from "@/components/Provenance";
import { ApiError, apiGet } from "@/lib/api";
import { idPath, isUuid } from "@/lib/ids";
import { useAssistantContext } from "@/lib/assistant";
import { count, date, dateTime } from "@/lib/format";
import { priorityBand } from "@/lib/health";
import { useApi } from "@/lib/useApi";

import { IdLink, SeverityBadge, SLA_LABEL, SlaBadge } from "./SecOps";

function Detail({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-semibold text-muted">{term}</dt>
      <dd className="mt-0.5 break-words text-sm text-ink">{children}</dd>
    </div>
  );
}

export function SecOpsFindingView({ id }: { id: string }) {
  const loaded = useApi((token) => apiGet(token, "/v1/secops/findings/{finding_id}", { finding_id: id }), id);
  const linked = loaded.status === "ready" ? (loaded.data.resource?.id ?? null) : null;
  const resourceId = isUuid(linked) ? linked : null;
  useAssistantContext({
    screen: "posture",
    tenantId: loaded.status === "ready" ? loaded.data.finding.tenant.id : null,
    record: resourceId ? { type: "resource", id: resourceId } : undefined,
  });
  if (loaded.status === "loading") return <Loading what="the finding" />;
  if (loaded.status === "error") {
    if (loaded.error instanceof ApiError && loaded.error.status === 404) {
      return <EmptyState title="Finding not found" next="It may have been remediated and removed, or it is not one of your customers’." action={{ label: "Back to SecOps", href: "/secops" }} />;
    }
    return <LoadError error={loaded.error} what="finding" />;
  }
  const d = loaded.data;
  const f = d.finding;
  const resourceHref = d.resource ? idPath("/resources", d.resource.id) : null;
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/secops" className="link">
          SecOps
        </Link>
        <span aria-hidden="true" className="px-1 text-muted">
          /
        </span>
        <span className="text-muted">Finding QID {f.qid}</span>
      </nav>
      <PageHeader title={f.title} subtitle={`${f.tenant.name} · ${f.asset_name}`}>
        <div className="flex flex-wrap gap-2">
          <SeverityBadge severity={f.severity} />
          <SlaBadge sla={f.sla} />
        </div>
      </PageHeader>

      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <MetricCard label={d.remediated_at ? "Days to fix" : "Days open"} metric={f.age} display={(m) => count(Number(m.value))} />
        <MetricCard
          label="Remediation target (days)"
          metric={f.target_days}
          display={(m) => count(Number(m.value))}
          unavailable="No target at this severity"
        />
        <MetricCard label="Other open findings on this asset" metric={d.other_open_on_asset} display={(m) => count(Number(m.value))} />
      </div>
      <p className="mb-4 text-sm text-muted">
        <ModelledMark /> {SLA_LABEL[f.sla]} against the platform’s modelled target for severity {f.severity} (ADR 0036), counted from when Qualys first detected
        it.
      </p>

      <section aria-labelledby="finding-heading" className="card mb-4 p-4">
        <h2 id="finding-heading" className="mb-3 section-title">
          The finding
        </h2>
        <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Detail term="QID">{f.qid}</Detail>
          <Detail term="Scope">{d.scope === "os" ? "Operating system" : "Application"}</Detail>
          <Detail term="Detection">{d.detection_type ?? "—"}</Detail>
          <Detail term="Status in Qualys">{f.status ?? "—"}</Detail>
          <Detail term="First detected">{dateTime(f.first_detected)}</Detail>
          <Detail term="Last detected">{f.last_detected ? dateTime(f.last_detected) : "—"}</Detail>
          <Detail term="Remediated">{d.remediated_at ? dateTime(d.remediated_at) : "Open"}</Detail>
          <Detail term="Asset">{f.asset_name}</Detail>
          <Detail term="Address">{f.asset_ipv4 ?? "—"}</Detail>
          <Detail term="Operating system">{d.operating_system ?? "—"}</Detail>
        </dl>
        {d.threat ? (
          <div className="mt-4">
            <h3 className="text-xs font-semibold text-muted">Threat</h3>
            {/* Scanner text is data: shown as text, never followed or rendered as markup. */}
            <p className="mt-0.5 text-sm whitespace-pre-line text-ink">{d.threat}</p>
          </div>
        ) : null}
        {d.solution ? (
          <div className="mt-4">
            <h3 className="text-xs font-semibold text-muted">Solution</h3>
            <p className="mt-0.5 text-sm whitespace-pre-line text-ink">{d.solution}</p>
          </div>
        ) : null}
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section aria-labelledby="resource-heading" className="card p-4">
          <h2 id="resource-heading" className="mb-2 section-title">
            Resource
          </h2>
          {d.resource ? (
            <>
              <dl className="grid gap-3 sm:grid-cols-2">
                <Detail term="Name">
                  <IdLink base="/resources" id={d.resource.id}>
                    {d.resource.name}
                  </IdLink>
                </Detail>
                <Detail term="Type">{d.resource.type}</Detail>
                <Detail term="Region">{d.resource.region ?? "—"}</Detail>
                <Detail term="State">{d.resource.state ?? "—"}</Detail>
              </dl>
              {resourceHref ? (
                <p className="mt-3 text-sm">
                  <Link href={resourceHref} className="link">
                    Open the resource’s context: its incidents, changes and every finding on it
                  </Link>
                </p>
              ) : null}
            </>
          ) : (
            <p className="text-sm text-muted">
              Qualys reported this host, but it matched no resource in the customer’s inventory. It is in the resource quarantine until the inventory or the
              naming is fixed.{" "}
              <Link href="/secops/coverage#quarantine" className="link">
                See scanner coverage
              </Link>
            </p>
          )}
        </section>

        <section aria-labelledby="tickets-heading" className="card p-4">
          <h2 id="tickets-heading" className="mb-2 section-title">
            Linked tickets
          </h2>
          {d.tickets === null ? (
            <p className="text-sm text-muted">No resource, so no ticket can be linked to this finding.</p>
          ) : d.tickets.length === 0 ? (
            <p className="text-sm text-muted">No open tickets on this resource, and none opened in the last 90 days.</p>
          ) : (
            <ul className="divide-y divide-line text-sm">
              {d.tickets.map((w) => (
                <li key={w.number} className="flex flex-wrap items-center gap-2 py-2">
                  <Link href={`/work/${encodeURIComponent(w.number)}`} className="link font-medium">
                    {w.number}
                  </Link>
                  <HealthBadge band={priorityBand(w.priority)} label={w.priority ?? "No priority"} />
                  <span className="text-ink">{w.short_description}</span>
                  <span className="text-muted">
                    {w.closed_at ? `closed ${date(w.closed_at)}` : `open since ${date(w.opened_at)}`}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}
