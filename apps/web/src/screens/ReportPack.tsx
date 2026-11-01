"use client";

// One monthly pack (step 17, HLD M19): the generated sections, the AI draft for
// each summary section beside the text a person writes or accepts, and release.
// A draft is never printed and never released: the PDF shows a person's text
// only, and approve and publish wait until every section has one. Figures are
// shown as the PDF prints them, money in the contract currency. A customer
// (CXO, VP or director; ADR 0030) reads published packs only, with the
// customer's price and each section's published text: no draft, no editor.

import { useState } from "react";

import type { FigureSnapshot, NarrativeView, ReportPackView, SectionSnapshot } from "@cloudops/contracts";

import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { ApiError, apiDownload, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { count, dateTime, decimal, money } from "@/lib/format";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

import { STATUS } from "./ReportsList";

type Message = { kind: "ok" | "error"; text: string } | null;

type Kind = FigureSnapshot["kind"];

const DECLINED: Record<string, string> = {
  instruction_like_content: "the data contains instruction-like text",
  ungrounded_numbers: "the draft used a number not in the data",
  low_confidence: "the model was not confident enough",
};
const ERRORS: Record<string, string> = {
  narrative_not_reviewed: "Every section's narrative needs a person's text first.",
  pack_not_editable: "Only a draft pack can be edited; generate a new version to change it.",
  pack_not_approved: "Approve the pack before publishing it.",
  draft_in_progress: "A draft is already on its way.",
};

/** As the PDF prints it (cloudops_domain.reporting.render): rounded once, here. */
function shown(value: string, kind: Kind, unit: string): string {
  if (!value) return "not available";
  if (kind === "money") return money(value, unit);
  if (kind === "percent") return `${decimal(value, 2)}%`;
  if (kind === "days") return `${decimal(value, 1)} days`;
  if (kind === "hours") return `${decimal(value, 1)} hours`;
  if (kind === "count") return count(Number(value));
  return value;
}

function SectionView({ section, currency }: { section: SectionSnapshot; currency: string }) {
  if (!section.captured) return <p className="text-sm text-muted">Not captured: {section.gap}.</p>;
  return (
    <>
      <ul className="space-y-1 text-sm text-ink">
        {section.figures.map((f) => (
          <li key={f.key}>
            {f.label}: <span className="font-semibold tabular-nums">{shown(f.value, f.kind, f.unit)}</span>
            {f.note ? <span className="text-muted"> ({f.note})</span> : null}
          </li>
        ))}
      </ul>
      {section.tables.map((t) => (
        <table key={t.key} className="mt-3 w-full text-left text-sm">
          <caption className="text-left font-medium text-ink">{t.title}</caption>
          <thead>
            <tr>
              {t.columns.map((c) => (
                <th key={c} scope="col" className="border-b border-line py-1.5 pr-3 font-semibold text-muted">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {t.rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j} className="border-b border-line-subtle py-1.5 pr-3 tabular-nums">
                    {cell ? shown(cell, t.kinds[j] ?? "text", currency) : "-"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      ))}
    </>
  );
}

function Narrative({ pack, n, editable, onChange }: { pack: string; n: NarrativeView; editable: boolean; onChange: (m: Message) => void }) {
  const { token } = useAuth();
  const [text, setText] = useState(n.text ?? "");
  const [busy, setBusy] = useState(false);
  const title = n.section.replace(/_/g, " ");

  async function act(run: (t: string) => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await run(await token());
      onChange({ kind: "ok", text: ok });
    } catch (e) {
      onChange({ kind: "error", text: (e instanceof ApiError && ERRORS[e.code]) || "That was not saved. Try again in a moment." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 rounded-control border border-dashed border-line p-3 text-sm">
      <p className="font-medium text-ink">
        AI draft: {n.draft_state}
        {n.draft_state === "declined" && n.draft_note ? ` (${DECLINED[n.draft_note] ?? n.draft_note})` : ""}
      </p>
      {n.draft_text ? <p className="mt-1 text-ink">{n.draft_text}</p> : null}
      {n.edited_at ? <p className="mt-1 text-xs text-muted">A person's text saved {dateTime(n.edited_at)}.</p> : <p className="mt-1 text-xs text-muted">No person's text yet: this section blocks release.</p>}
      {editable ? (
        <form
          className="mt-2"
          onSubmit={(e) => {
            e.preventDefault();
            void act((t) => apiPost(t, "/v1/reports/packs/{pack_id}/narratives/{section}", { pack_id: pack, section: n.section }, { text }), `Narrative for ${title} saved.`);
          }}
        >
          <label className="block text-ink">
            Narrative for {title}
            <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} maxLength={6000} className="mt-1 block w-full field" />
          </label>
          <div className="mt-1 flex flex-wrap gap-2">
            <button type="submit" disabled={busy || !text.trim()} className="btn btn-secondary">
              Save narrative
            </button>
            {n.draft_text ? (
              <button type="button" className="btn btn-secondary" onClick={() => setText(n.draft_text ?? "")}>
                Start from the draft
              </button>
            ) : null}
            {["none", "drafted", "declined", "failed"].includes(n.draft_state ?? "") ? (
              <button
                type="button"
                disabled={busy}
                className="btn btn-secondary"
                onClick={() => void act((t) => apiPost(t, "/v1/reports/packs/{pack_id}/narratives/{section}/draft", { pack_id: pack, section: n.section }, undefined), `A new draft for ${title} was requested.`)}
              >
                Request a new draft
              </button>
            ) : null}
          </div>
        </form>
      ) : n.text ? (
        <p className="mt-2 text-ink">{n.text}</p>
      ) : null}
    </div>
  );
}

/** A customer's view of a section's narrative: the published text and nothing else. */
function PublishedNarrative({ n }: { n: NarrativeView }) {
  return n.text ? <p className="mt-3 text-sm text-ink">{n.text}</p> : null;
}

export function ReportPack({ id }: { id: string }) {
  const me = useMe();
  const { token } = useAuth();
  const loaded = useApi((t) => apiGet(t, "/v1/reports/packs/{pack_id}", { pack_id: id }), `pack:${id}`);
  const [message, setMessage] = useState<Message>(null);
  const [busy, setBusy] = useState(false);
  if (loaded.status === "loading") return <Loading what="report pack" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="report pack" />;
  const pack: ReportPackView = loaded.data;
  const narratives = new Map(pack.narratives.map((n) => [n.section, n]));
  const editable = pack.status === "draft" && can(me, "reports.manage");
  const publish = can(me, "reports.publish");

  function changed(m: Message) {
    setMessage(m);
    loaded.reload();
  }

  async function release(step: "approve" | "publish") {
    setBusy(true);
    try {
      const path = step === "approve" ? "/v1/reports/packs/{pack_id}/approve" : "/v1/reports/packs/{pack_id}/publish";
      await apiPost(await token(), path, { pack_id: id }, undefined);
      changed({ kind: "ok", text: step === "approve" ? "Pack approved. Publish it when it should reach the customer." : "Pack published." });
    } catch (e) {
      changed({ kind: "error", text: (e instanceof ApiError && ERRORS[e.code]) || "That did not happen. Try again in a moment." });
    } finally {
      setBusy(false);
    }
  }

  async function pdf() {
    const blob = await apiDownload(await token(), "/v1/reports/packs/{pack_id}/pdf", { pack_id: id });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `report-${pack.period}-v${pack.version}.pdf`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <PageHeader
        title={`${pack.tenant.name} · ${pack.period}`}
        subtitle={`Version ${pack.version} · contract currency ${pack.contract_currency} · data as of ${dateTime(pack.data_as_of)}`}
      >
        <div className="flex flex-wrap items-center gap-2">
          <HealthBadge band={STATUS[pack.status].band} label={STATUS[pack.status].label} />
          <button type="button" onClick={() => void pdf()} className="btn btn-secondary">
            Download PDF
          </button>
          {publish && pack.status === "draft" ? (
            <button type="button" disabled={busy || !pack.releasable} onClick={() => void release("approve")} className="btn btn-primary">
              Approve
            </button>
          ) : null}
          {publish && pack.status === "approved" ? (
            <button type="button" disabled={busy} onClick={() => void release("publish")} className="btn btn-primary">
              Publish
            </button>
          ) : null}
        </div>
      </PageHeader>
      {!pack.releasable && pack.status === "draft" ? (
        <p className="mb-3 text-sm text-muted">Approval waits until every summary section has a person's text. Drafts are never printed or released.</p>
      ) : null}
      <div className="mb-3">
        {message ? (
          <p role={message.kind === "error" ? "alert" : "status"} className={`text-sm ${message.kind === "error" ? "text-band-critical" : "text-band-healthy"}`}>
            {message.text}
          </p>
        ) : null}
      </div>
      <div className="space-y-4">
        {pack.sections.map((s) => {
          const n = narratives.get(s.key);
          return (
            <section key={s.key} aria-labelledby={`section-${s.key}`} className="card p-4">
              <h2 id={`section-${s.key}`} className="mb-2 section-title">
                {s.title}
              </h2>
              <SectionView section={s} currency={pack.contract_currency} />
              {n && me.kind === "customer" ? <PublishedNarrative n={n} /> : null}
              {n && me.kind !== "customer" ? <Narrative pack={id} n={n} editable={editable} onChange={changed} /> : null}
            </section>
          );
        })}
      </div>
    </>
  );
}
