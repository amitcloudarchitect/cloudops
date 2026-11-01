"use client";

// Settings → Size bands (ADR 0038): the thresholds that put each customer in a size
// band on Home's Platform section. Staff read them and their history; setting new
// ones needs sizebands.manage, and the database checks again. A change is a new
// version with its reason, never an edit, and the request is in the audit log.

import { useState } from "react";
import type { FormEvent } from "react";

import type { SizeBandSettings } from "@cloudops/contracts";

import { metricText } from "@/components/CompareTable";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const MONEY = /^\d{1,12}(\.\d{1,6})?$/;
const WHOLE = /^\d{1,8}$/;
const labelStyle = "flex flex-col gap-1 text-xs font-semibold text-muted";

function failure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "Setting size bands is not available to your role.";
    if (error.status === 422) return "The thresholds were not saved: check that each Large threshold is at or above its Medium one.";
  }
  return "The thresholds were not saved. Try again in a moment.";
}

function Form({ current, onSaved }: { current: SizeBandSettings["current"]; onSaved: (s: SizeBandSettings) => void }) {
  const { token } = useAuth();
  const [form, setForm] = useState({
    spendMedium: String(current.spend_medium_from.value),
    spendLarge: String(current.spend_large_above.value),
    resourcesMedium: String(current.resources_medium_from.value),
    resourcesLarge: String(current.resources_large_above.value),
    reason: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const { spendMedium, spendLarge, resourcesMedium, resourcesLarge, reason } = form;
    if (!MONEY.test(spendMedium.trim()) || !MONEY.test(spendLarge.trim())) {
      setError("Enter spend thresholds as amounts with up to 6 decimal places, such as 50000.");
      return;
    }
    if (!WHOLE.test(resourcesMedium.trim()) || !WHOLE.test(resourcesLarge.trim()) || Number(resourcesMedium) < 1) {
      setError("Enter resource thresholds as whole numbers of at least 1.");
      return;
    }
    if (reason.trim().length < 3) {
      setError("Say why the thresholds change: it is recorded with the new version.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const saved = await apiPost(await token(), "/v1/settings/size-bands", {}, {
        spend_currency: current.currency,
        spend_medium_from: spendMedium.trim(),
        spend_large_above: spendLarge.trim(),
        resources_medium_from: Number(resourcesMedium),
        resources_large_above: Number(resourcesLarge),
        reason: reason.trim(),
      });
      setForm((f) => ({ ...f, reason: "" }));
      onSaved(saved);
    } catch (e) {
      setError(failure(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="card space-y-3 p-4" aria-labelledby="size-form-heading">
      <h2 id="size-form-heading" className="font-semibold text-ink">
        Set new thresholds
      </h2>
      <p className="text-xs text-muted">
        A customer is Small below the Medium threshold, Large above the Large one, and Medium between them, inclusive. Its band is the higher of its spend band
        and its resource band. Spend is monthly, in {current.currency}.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className={labelStyle}>
          Spend: Medium from ({current.currency} a month)
          <input className="field" inputMode="decimal" required value={form.spendMedium} onChange={(e) => setForm({ ...form, spendMedium: e.target.value })} />
        </label>
        <label className={labelStyle}>
          Spend: Large above ({current.currency} a month)
          <input className="field" inputMode="decimal" required value={form.spendLarge} onChange={(e) => setForm({ ...form, spendLarge: e.target.value })} />
        </label>
        <label className={labelStyle}>
          Resources: Medium from
          <input className="field" inputMode="numeric" required value={form.resourcesMedium} onChange={(e) => setForm({ ...form, resourcesMedium: e.target.value })} />
        </label>
        <label className={labelStyle}>
          Resources: Large above
          <input className="field" inputMode="numeric" required value={form.resourcesLarge} onChange={(e) => setForm({ ...form, resourcesLarge: e.target.value })} />
        </label>
        <label className={`${labelStyle} sm:col-span-2`}>
          Reason
          <input className="field" required minLength={3} maxLength={500} value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
        </label>
      </div>
      {error ? (
        <p role="alert" className="text-sm text-band-critical">
          {error}
        </p>
      ) : null}
      <button type="submit" className="btn btn-primary" disabled={busy}>
        {busy ? "Saving…" : "Save as a new version"}
      </button>
    </form>
  );
}

export function SizeBands() {
  const loaded = useApi((t) => apiGet(t, "/v1/settings/size-bands"), "size-bands");
  const [saved, setSaved] = useState<SizeBandSettings | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  if (loaded.status === "loading") return <Loading what="size bands" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="size bands" />;
  const view = saved ?? loaded.data;
  const c = view.current;
  return (
    <>
      <PageHeader
        title="Size bands"
        subtitle="The thresholds that put each customer in a Large, Medium or Small band on Home. Every change is a new version with its reason."
      />
      {message ? (
        <p role="status" className="mb-4 rounded-control border border-line-subtle bg-surface px-3 py-2 text-sm text-ink">
          {message}
        </p>
      ) : null}
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card p-4" aria-labelledby="size-current">
          <h2 id="size-current" className="mb-2 font-semibold text-ink">
            In force: version {metricText(c.version)}
          </h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted">Spend, Large above</dt>
            <dd className="tabular-nums text-ink">{metricText(c.spend_large_above)} a month</dd>
            <dt className="text-muted">Spend, Medium from</dt>
            <dd className="tabular-nums text-ink">{metricText(c.spend_medium_from)} a month</dd>
            <dt className="text-muted">Resources, Large above</dt>
            <dd className="tabular-nums text-ink">{metricText(c.resources_large_above)}</dd>
            <dt className="text-muted">Resources, Medium from</dt>
            <dd className="tabular-nums text-ink">{metricText(c.resources_medium_from)}</dd>
          </dl>
          <div className="mt-3 border-t border-line-subtle pt-2">
            <Provenance metric={c.version} />
          </div>
        </section>
        {view.can_edit ? (
          <Form
            current={c}
            onSaved={(s) => {
              setSaved(s);
              setMessage(`Version ${metricText(s.current.version)} is in force from now.`);
            }}
          />
        ) : (
          <p className="card p-4 text-sm text-muted">Setting the thresholds needs the Size bands permission. A platform administrator holds it by default.</p>
        )}
      </div>
      <section className="card mt-4 p-4" aria-labelledby="size-history">
        <h2 id="size-history" className="mb-2 font-semibold text-ink">
          History
        </h2>
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">Every version of the size band thresholds, newest first</caption>
          <thead>
            <tr className="text-left text-xs text-muted">
              <th scope="col" className="pb-1 pr-2 font-semibold">Version</th>
              <th scope="col" className="pb-1 pr-2 font-semibold">Set</th>
              <th scope="col" className="pb-1 pr-2 font-semibold">Spend (Medium – Large)</th>
              <th scope="col" className="pb-1 pr-2 font-semibold">Resources (Medium – Large)</th>
              <th scope="col" className="pb-1 font-semibold">Reason</th>
            </tr>
          </thead>
          <tbody>
            {view.history.map((h) => (
              <tr key={String(h.version.value)} className="border-t border-line-subtle align-top">
                <th scope="row" className="py-1.5 pr-2 text-left font-normal tabular-nums text-ink">
                  {metricText(h.version)}
                </th>
                <td className="py-1.5 pr-2 text-ink">
                  {dateTime(h.set_at)}
                  {h.set_by_you ? <span className="block text-xs text-muted">by you</span> : null}
                </td>
                <td className="py-1.5 pr-2 tabular-nums text-ink">
                  {metricText(h.spend_medium_from)} – {metricText(h.spend_large_above)}
                </td>
                <td className="py-1.5 pr-2 tabular-nums text-ink">
                  {metricText(h.resources_medium_from)} – {metricText(h.resources_large_above)}
                </td>
                <td className="py-1.5 text-ink">{h.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
