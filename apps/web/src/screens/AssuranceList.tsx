"use client";

// Assurance (step 17, HLD M14): the loaded control libraries and the
// assessments in scope, with their scores. Opening an assessment creates one
// response per active control of the framework. Staff only.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import type { AssessmentSummary, FrameworkView } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { useAuth } from "@/lib/auth";
import { count, date, decimal } from "@/lib/format";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

interface Data {
  frameworks: FrameworkView[];
  assessments: AssessmentSummary[];
}

async function load(token: string): Promise<Data> {
  const [frameworks, assessments] = await Promise.all([
    apiGet(token, "/v1/assurance/frameworks"),
    apiGet(token, "/v1/assurance/assessments"),
  ]);
  return { frameworks, assessments };
}

export function scoreText(a: AssessmentSummary): string {
  return a.score.percent ? `${decimal(a.score.percent.value, 2)}%` : "Not scored";
}

function OpenAssessment({ frameworks }: { frameworks: FrameworkView[] }) {
  const me = useMe();
  const { token } = useAuth();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tenants = me.subscription_limited ? [] : me.tenants;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      const created = await apiPost(await token(), "/v1/assurance/assessments", {}, {
        tenant_id: String(form.get("tenant")),
        framework: String(form.get("framework")),
        title: String(form.get("title")).trim(),
      });
      router.push(`/assurance/${created.id}`);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 404 ? "That customer or framework is not available to you." : "The assessment was not opened. Try again in a moment.");
      setBusy(false);
    }
  }

  if (tenants.length === 0 || frameworks.length === 0) return null;
  return (
    <form onSubmit={(e) => void submit(e)} className="mb-6 grid gap-3 card p-4 sm:grid-cols-4" aria-label="Open an assessment">
      <label className="text-sm text-ink">
        Customer
        <select name="tenant" required className="mt-1 block w-full field">
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm text-ink">
        Framework
        <select name="framework" required className="mt-1 block w-full field">
          {frameworks.map((f) => (
            <option key={f.framework} value={f.framework}>
              {f.framework} ({f.library_version})
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm text-ink">
        Title
        <input name="title" required maxLength={200} className="mt-1 block w-full field" />
      </label>
      <div className="flex items-end gap-2">
        <button type="submit" disabled={busy} className="btn btn-primary">
          Open assessment
        </button>
      </div>
      {error ? (
        <p role="alert" className="text-sm text-band-critical sm:col-span-4">
          {error}
        </p>
      ) : null}
    </form>
  );
}

export function AssuranceList() {
  useAssistantContext({ screen: "assurance" });
  const me = useMe();
  const loaded = useApi(load, "assurance");
  if (loaded.status === "loading") return <Loading what="assessments" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="assessments" />;
  const { frameworks, assessments } = loaded.data;

  const columns: Column<AssessmentSummary>[] = [
    {
      key: "title",
      header: "Assessment",
      value: (a) => a.title,
      render: (a) => (
        <Link href={`/assurance/${a.id}`} className="link font-medium">
          {a.title}
        </Link>
      ),
      sortable: true,
    },
    { key: "customer", header: "Customer", value: (a) => a.tenant.name, sortable: true },
    { key: "framework", header: "Framework", value: (a) => `${a.framework} ${a.library_version}`, sortable: true },
    { key: "score", header: "Score", value: (a) => (a.score.percent ? Number(a.score.percent.value) : null), render: scoreText, sortable: true, className: "tabular-nums" },
    { key: "pending", header: "Awaiting approval", value: (a) => a.score.pending.value, render: (a) => count(a.score.pending.value), sortable: true, className: "tabular-nums" },
    { key: "opened", header: "Opened", value: (a) => a.created_at, render: (a) => date(a.created_at), sortable: true },
  ];

  return (
    <>
      <PageHeader
        title="Assurance"
        subtitle="Controls answered from the platform's own data, drafted where they are narrative, and scored only once a person approves each result."
      />
      {frameworks.length === 0 ? (
        <div className="mb-6">
          <EmptyState
            title="No control library is loaded"
            next="Security supplies the control library; a platform administrator loads it. Until then there is nothing to assess."
          />
        </div>
      ) : (
        <ul className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-label="Control libraries">
          {frameworks.map((f) => (
            <li key={f.framework} className="card p-4 text-sm">
              <p className="font-semibold text-ink">
                {f.framework} <span className="font-normal text-muted">{f.library_version}</span>
              </p>
              <p className="mt-1 text-muted">
                {count(f.controls.value)} controls: {count(f.automated.value)} automated, {count(f.narrative.value)} narrative, {count(f.manual.value)} manual
              </p>
            </li>
          ))}
        </ul>
      )}
      {can(me, "assurance.manage") ? <OpenAssessment frameworks={frameworks} /> : null}
      <DataTable
        id="assessments"
        caption="Assessments"
        rows={assessments}
        rowKey={(a) => a.id}
        columns={columns}
        facets={[{ key: "framework", label: "Framework", value: (a) => a.framework }]}
        role={me.role}
        initial={{ sort: { key: "opened", dir: "desc" } }}
        empty={{ title: "No assessments yet", next: "Open one for a customer once a control library is loaded." }}
      />
    </>
  );
}
