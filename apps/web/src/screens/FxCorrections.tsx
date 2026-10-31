"use client";

// FX corrections (step 16; ADR 0019): Finance corrects a rate with a reason, and
// each correction is an explicit recorded entry, never an overwrite. Closed months
// it touches are restated by the next month-end run as new versions. EUR anchors
// every rate: the inverse and every pair with the moving currency follow the
// correction, each listed as its own entry; for a pair without EUR, Finance says
// which EUR leg moves. The list is staff-only (cost.read); the form needs
// fx.manage, and the database checks again.

import { useState } from "react";
import type { FormEvent } from "react";

import type { FxCorrectionRequest, FxCorrectionView } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { LoadError, Loading } from "@/components/Loading";
import { Provenance, sourceLabel } from "@/components/Provenance";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { CURRENCY_PATTERN } from "@/lib/commercial";
import { date, dateTime, decimal } from "@/lib/format";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

type Message = { kind: "ok" | "error"; text: string } | null;

const control = "w-full rounded-control border border-line bg-surface px-2 py-1.5 text-sm text-ink";
const labelStyle = "flex flex-col gap-1 text-xs font-medium text-muted";
const RATE = /^\d{1,10}(\.\d{1,8})?$/;

function rateText(value: string): string {
  return decimal(value, 6);
}

function failure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "Correcting FX rates is not available to your role.";
    if (error.status === 409) return "The correction was not saved: that rate, and every rate derived from it, is already in force for that day.";
    if (error.code === "eur_leg_required") return "Neither currency is EUR: choose which EUR leg moves.";
    if (error.code === "no_held_rate") return "The correction was not saved: the held currency has no EUR rate on or before that day.";
    if (error.status === 422) return "The correction was not saved: check the currencies, date, rate and reason.";
  }
  return "The correction was not saved. Try again in a moment.";
}

function CorrectionForm({ onSaved }: { onSaved: (message: Message) => void }) {
  const { token } = useAuth();
  const [form, setForm] = useState({ from: "", to: "EUR", kind: "average" as FxCorrectionRequest["rate_type"], on: "", rate: "", reason: "", moves: "" });
  const pairFrom = form.from.trim().toUpperCase();
  const pairTo = form.to.trim().toUpperCase();
  // Without EUR in the pair, Finance names the EUR leg that moves; the other is held.
  const needsLeg = CURRENCY_PATTERN.test(pairFrom) && CURRENCY_PATTERN.test(pairTo) && pairFrom !== pairTo && pairFrom !== "EUR" && pairTo !== "EUR";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const from = form.from.trim().toUpperCase();
    const to = form.to.trim().toUpperCase();
    if (!CURRENCY_PATTERN.test(from) || !CURRENCY_PATTERN.test(to) || from === to) {
      setError("Enter two different three-letter currency codes, such as GBP and EUR.");
      return;
    }
    if (!RATE.test(form.rate.trim()) || /^0+(\.0+)?$/.test(form.rate.trim())) {
      setError("Enter the rate as a positive decimal with up to 8 places, such as 1.17245.");
      return;
    }
    if (needsLeg && form.moves !== from && form.moves !== to) {
      setError("Neither currency is EUR: choose which EUR leg moves.");
      return;
    }
    if (form.reason.trim().length < 3) {
      setError("Say why the rate is corrected: it is recorded with the entry.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const saved = await apiPost(await token(), "/v1/finops/fx-corrections", {}, {
        from_currency: from,
        to_currency: to,
        rate_type: form.kind,
        effective_date: form.on,
        rate: form.rate.trim(),
        reason: form.reason.trim(),
        moves: needsLeg ? form.moves : null,
      });
      setForm((f) => ({ ...f, rate: "", reason: "" }));
      onSaved({
        kind: "ok",
        text: `Correction recorded: ${saved.from_currency} to ${saved.to_currency}, ${saved.rate_type}, ${date(saved.effective_date)}, now ${rateText(saved.new_rate.value)}.`,
      });
    } catch (e) {
      setError(failure(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-3 rounded-card border border-line bg-surface p-4" aria-labelledby="fx-form-heading">
      <h3 id="fx-form-heading" className="font-semibold text-ink">
        Correct a rate
      </h3>
      <p className="text-xs text-muted">
        The correction is recorded with your name and reason beside the rate it replaces. Its inverse and the rates derived from it through EUR
        follow, each recorded too. Closed months that used the old rate are restated as new versions at the next month-end run.
      </p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label className={labelStyle}>
          From currency
          <input className={control} value={form.from} required maxLength={3} autoComplete="off" onChange={(e) => setForm({ ...form, from: e.target.value })} />
        </label>
        <label className={labelStyle}>
          To currency
          <input className={control} value={form.to} required maxLength={3} autoComplete="off" onChange={(e) => setForm({ ...form, to: e.target.value })} />
        </label>
        <label className={labelStyle}>
          Rate type
          <select className={control} value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value === "closing" ? "closing" : "average" })}>
            <option value="average">Average</option>
            <option value="closing">Closing</option>
          </select>
        </label>
        <label className={labelStyle}>
          Effective date
          <input type="date" className={control} value={form.on} required onChange={(e) => setForm({ ...form, on: e.target.value })} />
        </label>
        <label className={labelStyle}>
          Rate
          <input className={control} value={form.rate} required inputMode="decimal" autoComplete="off" onChange={(e) => setForm({ ...form, rate: e.target.value })} />
        </label>
        {needsLeg ? (
          <label className={labelStyle}>
            EUR leg that moves
            <select className={control} value={form.moves} onChange={(e) => setForm({ ...form, moves: e.target.value })}>
              <option value="">Choose…</option>
              <option value={pairFrom}>EUR → {pairFrom} moves, EUR → {pairTo} held</option>
              <option value={pairTo}>EUR → {pairTo} moves, EUR → {pairFrom} held</option>
            </select>
          </label>
        ) : null}
        <label className={`${labelStyle} sm:col-span-2 lg:col-span-1`}>
          Reason
          <input className={control} value={form.reason} required minLength={3} maxLength={500} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
        </label>
      </div>
      {error ? (
        <p role="alert" className="text-sm text-band-critical">
          {error}
        </p>
      ) : null}
      <button type="submit" disabled={busy} className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink disabled:opacity-60">
        Record correction
      </button>
    </form>
  );
}

export function FxCorrections({ manage }: { manage: boolean }) {
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/finops/fx-corrections"), "fx-corrections");
  const [message, setMessage] = useState<Message>(null);

  const columns: Column<FxCorrectionView>[] = [
    { key: "pair", header: "Pair", value: (c) => `${c.from_currency} → ${c.to_currency}`, sortable: true },
    { key: "type", header: "Rate type", value: (c) => c.rate_type, sortable: true },
    { key: "effective", header: "Effective", value: (c) => c.effective_date, render: (c) => date(c.effective_date), sortable: true },
    {
      key: "rate",
      header: "Rate",
      value: (c) => c.new_rate.value,
      render: (c) => (
        <span className="tabular-nums">
          {c.previous_rate ? (
            <>
              <span className="text-muted">{rateText(c.previous_rate.value)} → </span>
              {rateText(c.new_rate.value)}
            </>
          ) : (
            rateText(c.new_rate.value)
          )}
        </span>
      ),
      sortable: false,
      className: "tabular-nums",
    },
    { key: "source", header: "Source", value: (c) => sourceLabel(c.source), sortable: true },
    { key: "reason", header: "Reason", value: (c) => c.reason, render: (c) => <span className="break-words">{c.reason}</span>, sortable: false },
    { key: "entered", header: "Entered", value: (c) => c.entered_at, render: (c) => dateTime(c.entered_at), sortable: true },
  ];

  return (
    <section aria-labelledby="fx-heading" className="space-y-3">
      <h2 id="fx-heading" className="text-lg font-semibold text-ink">
        FX corrections
      </h2>
      <p className="text-sm text-muted">
        Every correction or source revision of a rate, newest first.{" "}
        {manage ? "" : "Finance (fx.manage) records corrections; ask them if a rate looks wrong."}
      </p>
      {manage ? (
        <CorrectionForm
          onSaved={(m) => {
            setMessage(m);
            loaded.reload();
          }}
        />
      ) : null}
      {message ? (
        <p role="status" className="text-sm text-band-healthy">
          {message.text}
        </p>
      ) : null}
      {loaded.status === "loading" ? (
        <Loading what="FX corrections" />
      ) : loaded.status === "error" ? (
        <LoadError error={loaded.error} what="FX corrections" />
      ) : (
        <>
          {loaded.data[0] ? <Provenance metric={loaded.data[0].new_rate} /> : null}
          <DataTable
            id="fx-corrections"
            caption="FX corrections"
            rows={loaded.data}
            rowKey={(c) => c.id}
            columns={columns}
            role={me.role}
            empty={{ title: "No corrections recorded", next: "Rates come from the ECB feed. A correction made here is listed with its reason." }}
          />
        </>
      )}
    </section>
  );
}
