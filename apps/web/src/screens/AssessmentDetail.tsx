"use client";

// One assessment (step 17, HLD M14, M15): each control with its evidence, the
// rule's proposal, the model's draft and its citations, the person's narrative
// and the approved result. The score counts approved results only; the draft
// is never a result, and nothing here can approve on a person's behalf. The
// evidence pack downloads exactly as signed. Staff only.

import { useState } from "react";

import type { AssessmentDetail as Detail, ResponseView } from "@cloudops/contracts";

import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { count, dateTime, decimal, safeHttps } from "@/lib/format";
import type { Band } from "@/lib/health";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

type Result = NonNullable<ResponseView["result"]>;
type Message = { kind: "ok" | "error"; text: string } | null;

const RESULT: Record<Result, { band: Band; label: string }> = {
  compliant: { band: "healthy", label: "Compliant" },
  partially_compliant: { band: "watch", label: "Partially compliant" },
  non_compliant: { band: "critical", label: "Non-compliant" },
  not_applicable: { band: "unknown", label: "Not applicable" },
  manual: { band: "unknown", label: "Manual" },
};
const ALLOWED: Record<ResponseView["control"]["assessment"], Result[]> = {
  automated: ["compliant", "partially_compliant", "non_compliant", "not_applicable"],
  narrative: ["compliant", "partially_compliant", "non_compliant", "not_applicable"],
  manual: ["manual", "not_applicable"],
};
const DECLINED: Record<string, string> = {
  instruction_like_content: "the material contains instruction-like text, so a person drafts this one",
  ungrounded_citations: "the draft cited something it was not given",
  claims_result: "the draft asserted a result, which only a person decides",
  low_confidence: "the model was not confident enough to draft",
  ungrounded_numbers: "the draft used a number not in its data",
};
const ERRORS: Record<string, string> = {
  response_changed: "The narrative changed since this page loaded. Reload and read it before approving.",
  compliant_needs_narrative_and_evidence: "A compliant result needs a narrative, and collected evidence or a citation behind it.",
  result_not_allowed_for_control: "That result is not allowed for this kind of control.",
  assessment_closed: "This assessment is closed.",
};

function Feedback({ message }: { message: Message }) {
  if (!message) return null;
  return (
    <p role={message.kind === "error" ? "alert" : "status"} className={`text-sm ${message.kind === "error" ? "text-band-critical" : "text-band-healthy"}`}>
      {message.text}
    </p>
  );
}

function errorText(e: unknown, fallback: string): string {
  if (e instanceof ApiError && ERRORS[e.code]) return ERRORS[e.code] ?? fallback;
  if (e instanceof ApiError && e.status === 403) return "Not available to your role.";
  return fallback;
}

function Response({ assessment, r, onChange }: { assessment: string; r: ResponseView; onChange: (m: Message) => void }) {
  const me = useMe();
  const { token } = useAuth();
  const [text, setText] = useState(r.narrative ?? "");
  const [keepCitations, setKeepCitations] = useState(false);
  const [result, setResult] = useState<Result>(ALLOWED[r.control.assessment][0] ?? "not_applicable");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const manage = can(me, "assurance.manage");
  const approve = can(me, "assurance.approve");
  const base = { assessment_id: assessment, code: r.control.code };
  const id = `control-${r.control.code}`;

  async function act(run: (t: string) => Promise<unknown>, ok: string, fallback: string) {
    setBusy(true);
    try {
      await run(await token());
      onChange({ kind: "ok", text: ok });
    } catch (e) {
      onChange({ kind: "error", text: errorText(e, fallback) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <article aria-labelledby={`${id}-title`} className="card p-4">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 id={`${id}-title`} className="font-semibold text-ink">
            {r.control.code} · {r.control.title}
          </h3>
          <p className="text-sm text-muted">
            {r.control.domain} · {r.control.assessment} · answered by {r.control.evidence_source}
          </p>
        </div>
        {r.result ? (
          <HealthBadge band={RESULT[r.result].band} label={RESULT[r.result].label} />
        ) : (
          <HealthBadge band="unknown" label="Awaiting approval" />
        )}
      </header>
      <p className="mt-2 text-sm text-ink">{r.control.requirement}</p>

      {r.proposed_result ? (
        <p className="mt-2 text-sm text-ink">
          <strong>Proposed by the evidence rule:</strong> {RESULT[r.proposed_result].label} ({r.proposed_basis})
        </p>
      ) : null}

      {r.evidence.length > 0 ? (
        <details className="mt-2 text-sm">
          <summary className="cursor-pointer font-medium text-ink">Evidence ({count(r.evidence.length)})</summary>
          <ul className="mt-1 space-y-2">
            {r.evidence.map((e) => (
              <li key={e.id} className="rounded-control border border-line-subtle p-2">
                <p className="text-ink">
                  {e.query_name} from {e.source_system}, retrieved {dateTime(e.retrieved_at)}
                </p>
                <pre className="mt-1 overflow-x-auto text-xs text-muted">{JSON.stringify(e.raw_value, null, 1)}</pre>
                <p className="text-xs text-muted">
                  <code className="break-all">{e.query_text}</code>
                </p>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {r.control.assessment === "narrative" ? (
        <section aria-label={`Draft for ${r.control.code}`} className="mt-3 rounded-control border border-dashed border-line p-3 text-sm">
          <p className="font-medium text-ink">
            AI draft: {r.draft_state}
            {r.draft_state === "declined" && r.draft_note ? ` (${DECLINED[r.draft_note] ?? r.draft_note})` : ""}
          </p>
          {r.draft_narrative ? (
            <>
              <p className="mt-1 text-ink">{r.draft_narrative}</p>
              <p className="mt-1 text-xs text-muted">
                Material: {r.draft_sufficiency}. A draft is never a result.
              </p>
              {r.draft_citations.length > 0 ? (
                <ul className="mt-1 list-disc pl-5 text-xs text-muted">
                  {r.draft_citations.map((c, i) => (
                    <li key={i}>
                      {safeHttps(c.web_url) ? (
                        <a href={safeHttps(c.web_url) ?? undefined} target="_blank" rel="noreferrer noopener" className="link font-medium">
                          {String(c.label)}
                        </a>
                      ) : (
                        String(c.label)
                      )}
                    </li>
                  ))}
                </ul>
              ) : null}
              {manage ? (
                <button type="button" className="mt-2 btn btn-secondary" onClick={() => { setText(r.draft_narrative ?? ""); setKeepCitations(true); }}>
                  Start from the draft
                </button>
              ) : null}
            </>
          ) : null}
          {manage && ["none", "drafted", "declined", "failed"].includes(r.draft_state) ? (
            <button
              type="button"
              disabled={busy}
              className="mt-2 ml-2 btn btn-secondary"
              onClick={() =>
                void act(
                  (t) => apiPost(t, "/v1/assurance/assessments/{assessment_id}/drafts", { assessment_id: assessment }, undefined, { control: r.control.code }),
                  `A draft for ${r.control.code} was requested.`,
                  "The draft was not requested.",
                )
              }
            >
              Request a draft
            </button>
          ) : null}
        </section>
      ) : null}

      <div className="mt-3 space-y-2 text-sm">
        {manage ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void act(
                (t) => apiPost(t, "/v1/assurance/assessments/{assessment_id}/responses/{code}/narrative", base, { narrative: text, keep_draft_citations: keepCitations }),
                `Narrative for ${r.control.code} saved.`,
                "The narrative was not saved.",
              );
            }}
          >
            <label className="block text-ink">
              Narrative for {r.control.code}
              <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} maxLength={8000} className="mt-1 block w-full field" />
            </label>
            <label className="mt-1 flex items-center gap-2 text-xs text-muted">
              <input type="checkbox" checked={keepCitations} onChange={(e) => setKeepCitations(e.target.checked)} /> Keep the draft's citations (I checked them)
            </label>
            <button type="submit" disabled={busy || !text.trim()} className="mt-1 btn btn-secondary">
              Save narrative
            </button>
          </form>
        ) : r.narrative ? (
          <p className="text-ink">{r.narrative}</p>
        ) : null}

        {r.approved_at ? (
          <p className="text-xs text-muted">
            Approved {dateTime(r.approved_at)}
            {r.approval_note ? `: ${r.approval_note}` : ""}
          </p>
        ) : null}
        {approve ? (
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void act(
                (t) => apiPost(t, "/v1/assurance/assessments/{assessment_id}/responses/{code}/approve", base, { result, narrative_seen: r.narrative ?? null, note: note.trim() || null }),
                `${r.control.code} approved as ${RESULT[result].label.toLowerCase()}.`,
                "The approval was not saved.",
              );
            }}
          >
            <label className="text-ink">
              Result for {r.control.code}
              <select value={result} onChange={(e) => setResult(e.target.value as Result)} className="mt-1 block field">
                {ALLOWED[r.control.assessment].map((v) => (
                  <option key={v} value={v}>
                    {RESULT[v].label}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-ink">
              Note
              <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} className="mt-1 block field" />
            </label>
            <button type="submit" disabled={busy} className="btn btn-primary">
              Approve
            </button>
          </form>
        ) : null}
      </div>
    </article>
  );
}

function download(name: string, data: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export function AssessmentDetail({ id }: { id: string }) {
  const me = useMe();
  const { token } = useAuth();
  const loaded = useApi((t) => apiGet(t, "/v1/assurance/assessments/{assessment_id}", { assessment_id: id }), `assessment:${id}`);
  const [message, setMessage] = useState<Message>(null);
  const [busy, setBusy] = useState(false);
  if (loaded.status === "loading") return <Loading what="assessment" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="assessment" />;
  const a: Detail = loaded.data;
  const manage = can(me, "assurance.manage");

  function changed(m: Message) {
    setMessage(m);
    loaded.reload();
  }

  async function run(action: (t: string) => Promise<string>, fallback: string) {
    setBusy(true);
    try {
      changed({ kind: "ok", text: await action(await token()) });
    } catch (e) {
      changed({ kind: "error", text: e instanceof ApiError && e.code === "signing_unavailable" ? "Evidence packs cannot be signed here yet: no signing key is configured." : errorText(e, fallback) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title={a.title} subtitle={`${a.tenant.name} · ${a.framework} ${a.library_version} · opened ${dateTime(a.created_at)}`}>
        {manage ? (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              className="btn btn-secondary"
              onClick={() =>
                void run(async (t) => {
                  const got = await apiPost(t, "/v1/assurance/assessments/{assessment_id}/collect", { assessment_id: id }, undefined);
                  return `Evidence collected for ${count(got.length)} automated controls.`;
                }, "Evidence was not collected.")
              }
            >
              Collect evidence
            </button>
            <button
              type="button"
              disabled={busy}
              className="btn btn-secondary"
              onClick={() =>
                void run(async (t) => {
                  const got = await apiPost(t, "/v1/assurance/assessments/{assessment_id}/drafts", { assessment_id: id }, undefined);
                  return `${count(got.value)} drafts requested. The drafting worker writes them shortly.`;
                }, "Drafts were not requested.")
              }
            >
              Request drafts
            </button>
            <button
              type="button"
              disabled={busy}
              className="btn btn-primary"
              onClick={() =>
                void run(async (t) => {
                  const pack = await apiPost(t, "/v1/assurance/assessments/{assessment_id}/evidence-packs", { assessment_id: id }, undefined);
                  download(`evidence-pack-${a.framework}-${id}.json`, pack);
                  return "Signed evidence pack exported.";
                }, "The evidence pack was not exported.")
              }
            >
              Export signed evidence pack
            </button>
          </div>
        ) : null}
      </PageHeader>
      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard label="Score" metric={a.score.percent} display={(m) => `${decimal(String(m.value), 2)}%`} unavailable="Not scored: nothing approved yet" />
        <MetricCard label="Scored controls" metric={a.score.scorable} display={(m) => count(Number(m.value))} />
        <MetricCard label="Awaiting approval" metric={a.score.pending} band={a.score.pending.value > 0 ? "watch" : "healthy"} display={(m) => count(Number(m.value))} />
        <MetricCard label="Manual or not applicable" metric={a.score.excluded} display={(m) => count(Number(m.value))} />
      </div>
      <p className="mb-4 text-sm text-muted">Compliant scores 100, partially compliant 50, non-compliant 0. Manual and not applicable are excluded, and only approved results count.</p>
      <div className="mb-3">
        <Feedback message={message} />
      </div>
      <div className="space-y-3">
        {a.responses.map((r) => (
          <Response key={r.control.code} assessment={id} r={r} onChange={changed} />
        ))}
      </div>
    </>
  );
}
