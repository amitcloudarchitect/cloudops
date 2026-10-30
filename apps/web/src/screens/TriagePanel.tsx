"use client";

// The agent triage panel (step 14): what the agents concluded, presented for a
// person to accept or override. Their output is shown as data, never rendered
// as markup, and nothing here acts on a customer environment: planning (stage 4)
// is not built, so no plan is proposed.

import { useState } from "react";
import type { FormEvent } from "react";

import { TRIAGE_CHOICES, type ReviewRequest, type TriageOverrides, type TriageView } from "@cloudops/contracts";

import { HealthBadge } from "@/components/HealthBadge";
import { Provenance } from "@/components/Provenance";
import { ApiError, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime, money } from "@/lib/format";
import type { Band } from "@/lib/health";
import { can, useMe } from "@/lib/session";

const ROUTE: Record<string, { label: string; band: Band }> = {
  human: { label: "Needs a person", band: "at-risk" },
  ready_for_planning: { label: "Ready for planning", band: "healthy" },
};

const FIELD_LABEL: Record<string, string> = {
  intent: "Intent",
  affected_ci: "Affected CI",
  customer: "Customer",
  action_class: "Action class",
  risk: "Risk",
  confidence: "Confidence",
  rationale: "Rationale",
};

const RISK_BAND: Record<string, Band> = { low: "healthy", medium: "watch", high: "critical" };

function text(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function Fields({ data, caption }: { data: Record<string, unknown>; caption: string }) {
  const entries = Object.entries(data);
  if (entries.length === 0) return <p className="text-sm text-muted">None.</p>;
  return (
    <table className="w-full text-sm">
      <caption className="sr-only">{caption}</caption>
      <tbody>
        {entries.map(([key, value]) => (
          <tr key={key} className="border-b border-line last:border-b-0">
            <th scope="row" className="w-40 py-1.5 pr-3 text-left font-medium text-muted">
              {FIELD_LABEL[key] ?? key}
            </th>
            <td className="py-1.5 break-words text-ink">
              {key === "risk" && typeof value === "string" ? (
                <HealthBadge band={RISK_BAND[value] ?? "unknown"} label={value} />
              ) : (
                text(value)
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

type Draft = { [K in keyof TriageOverrides]-?: string };
const EMPTY: Draft = { intent: "", action_class: "", risk: "", affected_ci: "", customer: "" };

export function TriagePanel({ number, triage, onReviewed }: { number: string; triage: TriageView; onReviewed: () => void }) {
  const me = useMe();
  const { token } = useAuth();
  const [mode, setMode] = useState<"idle" | "override">("idle");
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const route = ROUTE[triage.route] ?? { label: triage.route, band: "unknown" as Band };
  const classification = triage.classification ?? {};
  const mayReview = can(me, "triage.review");

  async function submit(body: ReviewRequest) {
    setBusy(true);
    setMessage(null);
    try {
      await apiPost(await token(), "/v1/work-items/{number}/triage/{triage_id}/reviews", { number, triage_id: triage.id }, body);
      setMessage({ kind: "ok", text: body.decision === "accept" ? "Triage accepted." : "Override recorded." });
      setMode("idle");
      setDraft(EMPTY);
      setReason("");
      onReviewed();
    } catch (error) {
      const why = error instanceof ApiError && error.status === 422 ? "Check the values and the reason." : "Try again in a moment.";
      setMessage({ kind: "error", text: `The review was not saved. ${why}` });
    } finally {
      setBusy(false);
    }
  }

  function override(event: FormEvent) {
    event.preventDefault();
    const overrides: TriageOverrides = {};
    if (draft.intent) overrides.intent = draft.intent as TriageOverrides["intent"];
    if (draft.action_class) overrides.action_class = draft.action_class as TriageOverrides["action_class"];
    if (draft.risk) overrides.risk = draft.risk as TriageOverrides["risk"];
    if (draft.affected_ci.trim()) overrides.affected_ci = draft.affected_ci.trim();
    if (draft.customer.trim()) overrides.customer = draft.customer.trim();
    if (Object.keys(overrides).length === 0 || !reason.trim()) {
      setMessage({ kind: "error", text: "Change at least one field, and say why." });
      return;
    }
    void submit({ decision: "override", overrides, reason: reason.trim() });
  }

  const control = "w-full rounded-control border border-line bg-surface px-2 py-1.5 text-sm text-ink";
  const select = (field: "intent" | "action_class" | "risk") => (
    <label className="flex flex-col gap-1 text-xs font-medium text-muted">
      {FIELD_LABEL[field]}
      <select className={control} value={draft[field]} onChange={(e) => setDraft({ ...draft, [field]: e.target.value })}>
        <option value="">Keep “{text(classification[field])}”</option>
        {TRIAGE_CHOICES[field].map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <section aria-labelledby="triage-heading" className="rounded-card border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="triage-heading" className="text-lg font-semibold text-ink">
          Agent triage
        </h2>
        <HealthBadge band={route.band} label={route.label} />
      </div>
      <p className="mt-1 text-xs text-muted">
        Stage {triage.stage} of 3 · {dateTime(triage.created_at)} · cost {money(triage.cost.value, triage.cost.unit, 4)}{" "}
        <Provenance metric={triage.cost} />
      </p>

      {triage.injection_signals.length > 0 ? (
        <div role="alert" className="mt-3 rounded-control border border-band-critical bg-band-critical-bg p-3 text-sm text-band-critical">
          The ticket text contained instructions aimed at the agents ({triage.injection_signals.join(", ")}). They were treated as
          data and not followed. Review the classification with care.
        </div>
      ) : null}

      {triage.reasons.length > 0 ? (
        <div className="mt-3">
          <h3 className="text-sm font-semibold text-ink">Why it stopped here</h3>
          <ul className="mt-1 list-disc pl-5 text-sm text-ink">
            {triage.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div>
          <h3 className="mb-1 text-sm font-semibold text-ink">Classification</h3>
          {triage.classification ? <Fields data={classification} caption="Classification" /> : <p className="text-sm text-muted">The classifier declined; a person classifies this ticket.</p>}
        </div>
        <div>
          <h3 className="mb-1 text-sm font-semibold text-ink">Enrichment</h3>
          {triage.enrichment ? <Fields data={triage.enrichment} caption="Enrichment" /> : <p className="text-sm text-muted">Not enriched: triage stopped before stage 3.</p>}
        </div>
      </div>

      <div className="mt-4">
        <h3 className="text-sm font-semibold text-ink">Proposed plan</h3>
        <p className="text-sm text-muted">
          None. Planning (stage 4) is not built yet, so the agents propose no actions. Nothing is changed in the customer’s
          environment from here.
        </p>
      </div>

      <div className="mt-4 border-t border-line pt-4">
        <h3 className="text-sm font-semibold text-ink">Reviews</h3>
        {triage.reviews.length === 0 ? (
          <p className="text-sm text-muted">Not reviewed yet.</p>
        ) : (
          <ol className="mt-1 space-y-2 text-sm">
            {triage.reviews.map((r) => (
              <li key={r.id} className="rounded-control bg-surface-raised p-2 text-ink" data-testid="review">
                <span className="font-medium">{r.decision === "accept" ? "Accepted" : "Overridden"}</span>{" "}
                <span className="text-muted">{dateTime(r.created_at)}</span>
                {r.overrides ? (
                  <span className="block">
                    {Object.entries(r.overrides)
                      .map(([k, v]) => `${FIELD_LABEL[k] ?? k} → ${v}`)
                      .join("; ")}
                  </span>
                ) : null}
                {r.reason ? <span className="block text-muted">“{r.reason}”</span> : null}
              </li>
            ))}
          </ol>
        )}

        {message ? (
          <p role={message.kind === "error" ? "alert" : "status"} className={`mt-3 text-sm ${message.kind === "error" ? "text-band-critical" : "text-band-healthy"}`}>
            {message.text}
          </p>
        ) : null}

        {mayReview ? (
          mode === "idle" ? (
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => void submit({ decision: "accept" })}
                className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink disabled:opacity-60"
              >
                Accept triage
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setMode("override")}
                className="rounded-control border border-line px-3 py-1.5 text-sm font-medium text-ink"
              >
                Override…
              </button>
            </div>
          ) : (
            <form onSubmit={override} className="mt-3 space-y-3" aria-label="Override the triage">
              <div className="grid gap-3 sm:grid-cols-3">
                {select("intent")}
                {select("action_class")}
                {select("risk")}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="flex flex-col gap-1 text-xs font-medium text-muted">
                  Affected CI
                  <input className={control} maxLength={200} value={draft.affected_ci} onChange={(e) => setDraft({ ...draft, affected_ci: e.target.value })} placeholder={`Keep “${text(classification.affected_ci)}”`} />
                </label>
                <label className="flex flex-col gap-1 text-xs font-medium text-muted">
                  Customer
                  <input className={control} maxLength={200} value={draft.customer} onChange={(e) => setDraft({ ...draft, customer: e.target.value })} placeholder={`Keep “${text(classification.customer)}”`} />
                </label>
              </div>
              <label className="flex flex-col gap-1 text-xs font-medium text-muted">
                Reason (required)
                <textarea className={control} rows={3} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} required />
              </label>
              <div className="flex gap-2">
                <button type="submit" disabled={busy} className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink disabled:opacity-60">
                  Record override
                </button>
                <button type="button" onClick={() => setMode("idle")} className="rounded-control border border-line px-3 py-1.5 text-sm text-ink">
                  Cancel
                </button>
              </div>
            </form>
          )
        ) : (
          <p className="mt-3 text-sm text-muted">Your role can read triage but not review it.</p>
        )}
      </div>
    </section>
  );
}
