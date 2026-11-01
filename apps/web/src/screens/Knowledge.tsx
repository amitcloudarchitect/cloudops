"use client";

// Knowledge and document intelligence (step 17, HLD M20): which customers lack a
// current design document or handover pack, and which documents the live estate
// has superseded, and why. The verdicts are the API's (cloudops_domain.knowledge);
// this screen names them and never decides staleness itself. Staff only.

import type { CoverageView, DocumentList, DocumentView } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { count, date, safeHttps } from "@/lib/format";
import type { Band } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

type Coverage = CoverageView["design"];
type Reason = DocumentView["reasons"][number];

const COVERAGE: Record<Coverage, { band: Band; label: string }> = {
  current: { band: "healthy", label: "Current" },
  stale: { band: "at-risk", label: "Stale" },
  missing: { band: "critical", label: "Missing" },
};

const KIND: Record<DocumentView["doc_kind"], string> = {
  design: "Design",
  handover: "Handover",
  runbook: "Runbook",
  policy: "Policy",
};

interface Data {
  documents: DocumentList;
  gaps: CoverageView[];
}

async function load(token: string): Promise<Data> {
  const [documents, gaps] = await Promise.all([apiGet(token, "/v1/knowledge/documents"), apiGet(token, "/v1/knowledge/gaps")]);
  return { documents, gaps };
}

function CoverageBadge({ value, kind }: { value: Coverage; kind: string }) {
  const c = COVERAGE[value];
  return <HealthBadge band={c.band} label={`${kind}: ${c.label}`} />;
}

export function Knowledge() {
  useAssistantContext({ screen: "knowledge" });
  const me = useMe();
  const loaded = useApi(load, "knowledge");
  if (loaded.status === "loading") return <Loading what="knowledge" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="knowledge" />;
  const { documents, gaps } = loaded.data;
  const rules = documents.rules as Record<Reason, string>;
  const withGaps = gaps.filter((g) => g.has_gap);

  const gapColumns: Column<CoverageView>[] = [
    { key: "customer", header: "Customer", value: (g) => g.tenant.name, sortable: true },
    {
      key: "design",
      header: "Design document",
      value: (g) => g.design,
      render: (g) => <CoverageBadge value={g.design} kind="Design" />,
      sortable: true,
    },
    {
      key: "handover",
      header: "Handover pack",
      value: (g) => g.handover,
      render: (g) => <CoverageBadge value={g.handover} kind="Handover" />,
      sortable: true,
    },
  ];

  const docColumns: Column<DocumentView>[] = [
    {
      key: "name",
      header: "Document",
      value: (d) => d.name,
      render: (d) =>
        safeHttps(d.web_url) ? (
          <a href={safeHttps(d.web_url) ?? undefined} target="_blank" rel="noreferrer noopener" className="link font-medium">
            {d.name}
          </a>
        ) : (
          <span className="font-medium">{d.name}</span>
        ),
      sortable: true,
    },
    { key: "customer", header: "Customer", value: (d) => d.tenant.name, sortable: true },
    { key: "kind", header: "Kind", value: (d) => KIND[d.doc_kind], sortable: true },
    { key: "modified", header: "Last modified", value: (d) => d.modified_at ?? "", render: (d) => date(d.modified_at), sortable: true },
    {
      key: "status",
      header: "Status",
      value: (d) => (d.stale ? "Stale" : "Current"),
      render: (d) => (
        <span className="flex flex-col items-start gap-1">
          <HealthBadge band={d.stale ? "at-risk" : "healthy"} label={d.stale ? "Stale" : "Current"} />
          {d.reasons.length > 0 ? <span className="text-xs text-muted">{d.reasons.map((r) => rules[r] ?? r).join("; ")}</span> : null}
          {d.removed_resources.length > 0 ? (
            <span className="text-xs text-muted">Names removed: {d.removed_resources.join(", ")}</span>
          ) : null}
        </span>
      ),
      sortable: true,
    },
    {
      key: "named",
      header: "Resources named",
      value: (d) => d.resources_named.value,
      render: (d) => count(d.resources_named.value),
      sortable: true,
      className: "tabular-nums",
    },
  ];

  return (
    <>
      <PageHeader
        title="Knowledge"
        subtitle="Customers' design documents and handover packs, checked against the live estate. A document's kind comes from its name and folder, never from what it says."
      >
        <button type="button" onClick={loaded.reload} className="btn btn-secondary">
          Refresh
        </button>
      </PageHeader>

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <MetricCard
          label="Stale documents"
          metric={documents.stale}
          band={documents.stale.value > 0 ? "at-risk" : "healthy"}
          display={(m) => count(Number(m.value))}
        />
        <div className="card p-4">
          <h3 className="text-sm font-medium text-muted">Customers with a gap</h3>
          <p className="mt-1 text-2xl font-semibold tabular-nums text-ink">
            {count(withGaps.length)} <span className="text-base font-normal text-muted">of {count(gaps.length)}</span>
          </p>
        </div>
      </div>

      <details className="mb-6 card p-3 text-sm text-ink">
        <summary className="cursor-pointer font-medium">How staleness is decided</summary>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-muted">
          {Object.entries(rules).map(([reason, rule]) => (
            <li key={reason}>{rule}</li>
          ))}
        </ul>
      </details>

      <div className="space-y-6">
        <section aria-labelledby="gaps-heading">
          <h2 id="gaps-heading" className="mb-2 section-title">
            Gaps
          </h2>
          <DataTable
            id="knowledge-gaps"
            caption="Design and handover coverage"
            rows={gaps}
            rowKey={(g) => g.tenant.id}
            columns={gapColumns}
            facets={[{ key: "gap", label: "Coverage", value: (g) => (g.has_gap ? "Has a gap" : "Covered") }]}
            role={me.role}
            empty={{ title: "No customers in scope", next: "Customers appear here once they are assigned to you." }}
          />
        </section>
        <section aria-labelledby="documents-heading">
          <h2 id="documents-heading" className="mb-2 section-title">
            Documents
          </h2>
          <DataTable
            id="knowledge-documents"
            caption="Documents checked against the estate"
            rows={documents.items}
            rowKey={(d) => d.id}
            columns={docColumns}
            facets={[
              { key: "kind", label: "Kind", value: (d) => KIND[d.doc_kind] },
              { key: "status", label: "Status", value: (d) => (d.stale ? "Stale" : "Current") },
            ]}
            role={me.role}
            initial={{ sort: { key: "status", dir: "desc" } }}
            empty={{
              title: "No design, handover, runbook or policy documents yet",
              next: "Documents arrive from SharePoint sites whose owners have granted access. Ask the customer's delivery lead to register the site.",
            }}
          />
        </section>
      </div>
    </>
  );
}
