"use client";

// Settings → Commercial setup (ADR 0033, ADR 0035): what margin, pricing and the
// report packs compute from, recorded through the platform.
//
// - The SOW register, per customer: contract currency, term, scope, the recurring
//   fee and its billing frequency, the target margin, the consumption uplift, the
//   contract values, and where the agreement is recorded. Versioned: an amendment
//   is a new version from a later date, and the version before it only gains the
//   day it ends. Nothing is edited.
// - "Recompute margin now": a request the FinOps worker answers within a minute or
//   so, instead of at the next daily run.
// - The rate card: every published version, and a new one published from the
//   version in force, effective from a future date.
//
// Staff with commercial.read see it; recording needs commercial.write and
// publishing ratecard.publish, and the database checks both again. Money is typed
// and sent as decimal strings, never numbers, and rounded only for display.

import Link from "next/link";
import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import type {
  PublishRateCard,
  RateCardDocument,
  RateCardVersionView,
  SowInput,
  SowRegister,
  SowVersionView,
} from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useCustomer } from "@/lib/customer";
import { date, dateTime, decimal, money, percent } from "@/lib/format";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const control = "w-full field";
const labelStyle = "flex flex-col gap-1 text-xs font-semibold text-muted";
const MONEY = /^\d{1,12}(\.\d{1,6})?$/;
const RATE = /^\d{1,12}(\.\d{1,16})?$/;
const PERCENT = /^-?\d{1,2}(\.\d{1,4})?$/;
const FREQUENCY_LABEL: Record<SowInput["billing_frequency"], string> = {
  monthly: "Monthly",
  quarterly: "Quarterly",
  annual: "Annually",
};

type Message = { kind: "ok" | "error"; text: string } | null;

/** "25" (per cent) as the fraction "0.25", on its digits: no float involved. */
export function fromPercent(value: string): string {
  const match = /^(-?)(\d{1,2})(?:\.(\d{1,4}))?$/.exec(value.trim());
  if (!match) throw new Error(`not a percentage: ${value}`);
  const [, sign = "", whole = "0", fraction = ""] = match;
  const digits = `${whole.padStart(2, "0")}${fraction}`.replace(/0+$/, "");
  return digits === "" ? "0" : `${sign}0.${digits}`;
}

/** The fraction "0.25" as "25" (per cent), for prefilling a form. */
export function toPercent(fraction: string): string {
  return percent(fraction, 4).replace(/%$/, "").replace(/\.?0+$/, "");
}

function failure(error: unknown, what: string): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return `${what} is not available to your role.`;
    if (error.status === 404) return "That customer is not in your scope.";
    if (error.code === "effective_date_not_after_current") return "An amendment takes effect after the version in force: choose a later date.";
    if (error.code === "effective_date_not_future") return "A new rate card version takes effect after today and after the latest version.";
    if (error.code === "version_exists") return "That version name is already published: choose another.";
    if (error.code === "invalid_rate_card") return "The rate card cannot price every case: check the role shares add up to 100% and every amount is filled in.";
    if (error.status === 422) return "Not saved: check the dates, the amounts and the required fields.";
  }
  return "Not saved. Try again in a moment.";
}

function Feedback({ message }: { message: Message }) {
  if (!message) return null;
  return (
    <p role={message.kind === "error" ? "alert" : "status"} className={`text-sm ${message.kind === "error" ? "text-band-critical" : "text-ink"}`}>
      {message.text}
    </p>
  );
}

function Field({ label, children, wide = false }: { label: string; children: ReactNode; wide?: boolean }) {
  return <label className={`${labelStyle} ${wide ? "sm:col-span-2" : ""}`}>{label}{children}</label>;
}

// --- the SOW register ---------------------------------------------------------------

function sowColumns(): Column<SowVersionView>[] {
  return [
    { key: "version", header: "Version", value: (v) => Number(v.version.value), sortable: true },
    { key: "reference", header: "Reference", value: (v) => v.reference, sortable: true },
    {
      key: "effective",
      header: "In force",
      value: (v) => v.effective_from,
      render: (v) => (
        <span className="flex flex-col gap-1">
          <span>
            {date(v.effective_from)} – {v.effective_to ? date(v.effective_to) : "open"}
          </span>
          {v.in_force ? <HealthBadge band="healthy" label="In force today" /> : null}
        </span>
      ),
      sortable: true,
    },
    {
      key: "fee",
      header: "Recurring fee",
      value: (v) => v.recurring_fee?.value ?? null,
      render: (v) =>
        v.recurring_fee && v.billing_frequency ? (
          <span className="tabular-nums">
            {money(v.recurring_fee.value, v.currency)} {FREQUENCY_LABEL[v.billing_frequency].toLowerCase()}
          </span>
        ) : (
          <span className="text-muted">Not recorded</span>
        ),
      className: "tabular-nums",
    },
    {
      key: "monthly",
      header: "Monthly revenue",
      value: (v) => v.monthly_manage_revenue.value,
      render: (v) => <span className="tabular-nums">{money(v.monthly_manage_revenue.value, v.currency)}</span>,
      className: "tabular-nums",
    },
    {
      key: "target",
      header: "Target margin",
      value: (v) => v.target_margin?.value ?? null,
      render: (v) => (v.target_margin ? percent(v.target_margin.value) : <span className="text-muted">None</span>),
    },
    {
      key: "uplift",
      header: "Consumption uplift",
      value: (v) => v.consumption_uplift?.value ?? null,
      render: (v) => (v.consumption_uplift ? percent(v.consumption_uplift.value, 2) : <span className="text-muted">Not priced</span>),
    },
    { key: "scope", header: "Service scope", value: (v) => v.service_scope, render: (v) => <span className="break-words">{v.service_scope ?? "—"}</span> },
    { key: "agreement", header: "Agreement recorded at", value: (v) => v.document_uri, render: (v) => <span className="break-words">{v.document_uri ?? "—"}</span> },
    { key: "why", header: "Amendment", value: (v) => v.amendment_reason, render: (v) => <span className="break-words">{v.amendment_reason ?? "First version"}</span> },
  ];
}

interface SowForm {
  reference: string;
  effective_from: string;
  term_start: string;
  term_end: string;
  currency: string;
  service_scope: string;
  coverage: "" | NonNullable<SowInput["coverage"]>;
  support: "" | NonNullable<SowInput["support"]>;
  recurring_fee: string;
  billing_frequency: SowInput["billing_frequency"];
  target_margin: string; // per cent
  consumption_uplift: string; // per cent
  hosting_acv: string;
  manage_acv: string;
  professional_services: string;
  document_uri: string;
  amendment_reason: string;
}

const EMPTY_SOW: SowForm = {
  reference: "",
  effective_from: "",
  term_start: "",
  term_end: "",
  currency: "EUR",
  service_scope: "",
  coverage: "24x7",
  support: "Gold",
  recurring_fee: "",
  billing_frequency: "monthly",
  target_margin: "",
  consumption_uplift: "",
  hosting_acv: "",
  manage_acv: "",
  professional_services: "",
  document_uri: "",
  amendment_reason: "",
};

/** An amendment starts from the version in force: only what changes is retyped. */
function fromVersion(v: SowVersionView): SowForm {
  return {
    ...EMPTY_SOW,
    reference: v.reference,
    term_start: v.term_start ?? "",
    term_end: v.term_end ?? "",
    currency: v.currency,
    service_scope: v.service_scope ?? "",
    coverage: v.coverage === "8x5" || v.coverage === "16x5" || v.coverage === "24x7" ? v.coverage : "",
    support: v.support === "Bronze" || v.support === "Silver" || v.support === "Gold" ? v.support : "",
    recurring_fee: v.recurring_fee ? String(v.recurring_fee.value) : "",
    billing_frequency: v.billing_frequency ?? "monthly",
    target_margin: v.target_margin ? toPercent(String(v.target_margin.value)) : "",
    consumption_uplift: v.consumption_uplift ? toPercent(String(v.consumption_uplift.value)) : "",
    hosting_acv: v.hosting_acv ? String(v.hosting_acv.value) : "",
    manage_acv: v.manage_acv ? String(v.manage_acv.value) : "",
    professional_services: String(v.professional_services.value),
    document_uri: v.document_uri ?? "",
  };
}

function SowEditor({ tenantId, current, onSaved }: { tenantId: string; current: SowVersionView | null; onSaved: (r: SowRegister, text: string) => void }) {
  const { token } = useAuth();
  const [form, setForm] = useState<SowForm>(current ? fromVersion(current) : EMPTY_SOW);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const amending = current !== null;
  const set = (patch: Partial<SowForm>) => setForm((f) => ({ ...f, ...patch }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    const amounts: [string, string, boolean][] = [
      ["recurring fee", form.recurring_fee, true],
      ["hosting contract value", form.hosting_acv, false],
      ["manage contract value", form.manage_acv, false],
      ["professional services", form.professional_services, false],
    ];
    for (const [name, value, required] of amounts) {
      if ((required || value.trim()) && !MONEY.test(value.trim())) {
        setError(`Enter the ${name} as a number with up to 6 decimal places, such as 12500 or 12500.50.`);
        return;
      }
    }
    if (!/^[A-Z]{3}$/.test(form.currency.trim().toUpperCase())) {
      setError("Enter the contract currency as a three-letter code, such as EUR or GBP.");
      return;
    }
    for (const [name, value] of [["target margin", form.target_margin], ["consumption uplift", form.consumption_uplift]] as const) {
      if (value.trim() && !PERCENT.test(value.trim())) {
        setError(`Enter the ${name} as a percentage, such as 25 or 7.5.`);
        return;
      }
    }
    if (form.consumption_uplift.trim() && !/[1-9]/.test(form.consumption_uplift)) {
      setError("The consumption uplift is above 0%, or left empty while it is not agreed.");
      return;
    }
    if (amending && form.amendment_reason.trim().length < 3) {
      setError("Say why the SOW is amended: it is recorded with the new version.");
      return;
    }
    setError(null);
    setBusy(true);
    const optional = (v: string) => (v.trim() ? v.trim() : null);
    const body: SowInput = {
      reference: form.reference.trim(),
      effective_from: form.effective_from,
      term_start: form.term_start,
      term_end: optional(form.term_end),
      currency: form.currency.trim().toUpperCase(),
      service_scope: form.service_scope.trim(),
      coverage: form.coverage || null,
      support: form.support || null,
      recurring_fee: form.recurring_fee.trim(),
      billing_frequency: form.billing_frequency,
      target_margin: form.target_margin.trim() ? fromPercent(form.target_margin) : null,
      consumption_uplift: form.consumption_uplift.trim() ? fromPercent(form.consumption_uplift) : null,
      hosting_acv: optional(form.hosting_acv),
      manage_acv: optional(form.manage_acv),
      professional_services: optional(form.professional_services),
      document_uri: form.document_uri.trim(),
      amendment_reason: amending ? form.amendment_reason.trim() : null,
    };
    try {
      const saved = await apiPost(await token(), "/v1/commercial/customers/{tenant_id}/sows", { tenant_id: tenantId }, body);
      const top = saved.versions[0];
      onSaved(saved, top ? `Version ${String(top.version.value)} recorded, in force from ${date(top.effective_from)}.` : "Recorded.");
    } catch (e) {
      setError(failure(e, "Recording a SOW"));
    } finally {
      setBusy(false);
    }
  }

  const heading = amending ? "Record an amendment" : "Record the first SOW";
  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-4 card p-4" aria-labelledby="sow-form-heading">
      <h3 id="sow-form-heading" className="font-semibold text-ink">
        {heading}
      </h3>
      <p className="text-xs text-muted">
        {amending
          ? "An amendment is a new version from its effective date. The version in force ends the day before, and nothing else about it changes."
          : "The SOW the customer's margin, price and report packs are computed from. Later changes are recorded as amendments."}
      </p>
      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold text-ink">Agreement</legend>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Field label="Reference">
            <input className={control} value={form.reference} required maxLength={100} onChange={(e) => set({ reference: e.target.value })} />
          </Field>
          <Field label="Effective from">
            <input type="date" className={control} value={form.effective_from} required onChange={(e) => set({ effective_from: e.target.value })} />
          </Field>
          <Field label="Term starts">
            <input type="date" className={control} value={form.term_start} required onChange={(e) => set({ term_start: e.target.value })} />
          </Field>
          <Field label="Term ends (empty: open-ended)">
            <input type="date" className={control} value={form.term_end} onChange={(e) => set({ term_end: e.target.value })} />
          </Field>
          <Field label="Where the agreement is recorded" wide>
            <input className={control} value={form.document_uri} required minLength={3} maxLength={500} placeholder="Contract system or library and the document's name" onChange={(e) => set({ document_uri: e.target.value })} />
          </Field>
          <Field label="Service scope" wide>
            <input className={control} value={form.service_scope} required maxLength={500} onChange={(e) => set({ service_scope: e.target.value })} />
          </Field>
          <Field label="Coverage">
            <select className={control} value={form.coverage} onChange={(e) => set({ coverage: e.target.value === "8x5" || e.target.value === "16x5" || e.target.value === "24x7" ? e.target.value : "" })}>
              <option value="">The customer&apos;s default</option>
              <option value="8x5">8x5</option>
              <option value="16x5">16x5</option>
              <option value="24x7">24x7</option>
            </select>
          </Field>
          <Field label="Support tier">
            <select className={control} value={form.support} onChange={(e) => set({ support: e.target.value === "Bronze" || e.target.value === "Silver" || e.target.value === "Gold" ? e.target.value : "" })}>
              <option value="">The customer&apos;s default</option>
              <option value="Bronze">Bronze</option>
              <option value="Silver">Silver</option>
              <option value="Gold">Gold</option>
            </select>
          </Field>
        </div>
      </fieldset>
      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold text-ink">Commercials</legend>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Field label="Contract currency">
            <input className={control} value={form.currency} required maxLength={3} autoComplete="off" onChange={(e) => set({ currency: e.target.value })} />
          </Field>
          <Field label={`Recurring fee (${form.currency.toUpperCase() || "contract currency"})`}>
            <input className={control} value={form.recurring_fee} required inputMode="decimal" autoComplete="off" onChange={(e) => set({ recurring_fee: e.target.value })} />
          </Field>
          <Field label="Billed">
            <select className={control} value={form.billing_frequency} onChange={(e) => set({ billing_frequency: e.target.value === "quarterly" ? "quarterly" : e.target.value === "annual" ? "annual" : "monthly" })}>
              <option value="monthly">Monthly</option>
              <option value="quarterly">Quarterly</option>
              <option value="annual">Annually</option>
            </select>
          </Field>
          <Field label="Target margin (%)">
            <input className={control} value={form.target_margin} inputMode="decimal" autoComplete="off" onChange={(e) => set({ target_margin: e.target.value })} />
          </Field>
          <Field label="Consumption uplift (%)">
            <input className={control} value={form.consumption_uplift} inputMode="decimal" autoComplete="off" onChange={(e) => set({ consumption_uplift: e.target.value })} />
          </Field>
          <Field label="Hosting contract value, annual (optional)">
            <input className={control} value={form.hosting_acv} inputMode="decimal" autoComplete="off" onChange={(e) => set({ hosting_acv: e.target.value })} />
          </Field>
          <Field label="Manage contract value, annual (optional)">
            <input className={control} value={form.manage_acv} inputMode="decimal" autoComplete="off" onChange={(e) => set({ manage_acv: e.target.value })} />
          </Field>
          <Field label="Professional services value (optional)">
            <input className={control} value={form.professional_services} inputMode="decimal" autoComplete="off" onChange={(e) => set({ professional_services: e.target.value })} />
          </Field>
        </div>
        <p className="text-xs text-muted">
          Margin uses the fee as a monthly figure: a quarterly fee divided by 3, an annual one by 12. The target margin bands the customer on the Margin
          page; the uplift prices the customer&apos;s cloud consumption in the portal, and while it is empty the portal shows &ldquo;Not yet priced&rdquo;.
        </p>
      </fieldset>
      {amending ? (
        <Field label="Why the SOW is amended" wide>
          <input className={control} value={form.amendment_reason} required minLength={3} maxLength={500} onChange={(e) => set({ amendment_reason: e.target.value })} />
        </Field>
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-band-critical">
          {error}
        </p>
      ) : null}
      <button type="submit" disabled={busy} className="btn btn-primary">
        {busy ? "Recording…" : amending ? "Record amendment" : "Record SOW"}
      </button>
    </form>
  );
}

function Recompute({ tenantId, register, write, onDone }: { tenantId: string; register: SowRegister; write: boolean; onDone: (m: Message) => void }) {
  const { token } = useAuth();
  const [busy, setBusy] = useState(false);
  const r = register.recompute;
  async function request() {
    setBusy(true);
    try {
      const made = await apiPost(await token(), "/v1/commercial/customers/{tenant_id}/margin-recompute", { tenant_id: tenantId }, undefined);
      onDone({ kind: "ok", text: `Margin recompute ${made.status === "requested" ? "requested" : made.status}: the FinOps worker answers within a few minutes.` });
    } catch (e) {
      onDone({ kind: "error", text: failure(e, "Recomputing margin") });
    } finally {
      setBusy(false);
    }
  }
  const STATUS: Record<NonNullable<SowRegister["recompute"]>["status"], string> = {
    requested: "Waiting for the FinOps worker",
    running: "Being recomputed",
    succeeded: "Recomputed",
    failed: "Failed: the next daily run tries again",
  };
  return (
    <div className="card flex flex-wrap items-center justify-between gap-3 p-4">
      <div className="text-sm">
        <p className="font-semibold text-ink">Margin</p>
        <p className="text-muted">
          {r ? `${STATUS[r.status]} (requested ${dateTime(r.requested_at)}${r.finished_at ? `, finished ${dateTime(r.finished_at)}` : ""}).` : "Recomputed daily by the FinOps worker."}{" "}
          <Link href={`/margin/${tenantId}`} className="link">
            Open this customer’s margin
          </Link>
        </p>
      </div>
      {write ? (
        <button type="button" className="btn" disabled={busy} onClick={() => void request()}>
          {busy ? "Requesting…" : "Recompute margin now"}
        </button>
      ) : null}
    </div>
  );
}

function SowSection({ tenantId, write }: { tenantId: string; write: boolean }) {
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/commercial/customers/{tenant_id}/sows", { tenant_id: tenantId }), `sows-${tenantId}`);
  const [message, setMessage] = useState<Message>(null);
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    setMessage(null);
    setEditing(false);
  }, [tenantId]);

  if (loaded.status === "loading") return <Loading what="the SOW register" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="the SOW register" />;
  const register = loaded.data;
  const current = register.versions.find((v) => v.in_force) ?? register.versions[0] ?? null;
  return (
    <section aria-labelledby="sow-heading" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="sow-heading" className="section-title">
          SOW register: {register.tenant.name}
        </h2>
        {write && !editing ? (
          <button type="button" className="btn btn-primary" onClick={() => setEditing(true)}>
            {current ? "Record an amendment" : "Record the first SOW"}
          </button>
        ) : null}
      </div>
      <Feedback message={message} />
      {editing ? (
        <SowEditor
          key={current?.id ?? "first"}
          tenantId={tenantId}
          current={current}
          onSaved={(_, text) => {
            setEditing(false);
            setMessage({ kind: "ok", text: `${text} Request a margin recompute to see it on the Margin page now.` });
            loaded.reload();
          }}
        />
      ) : null}
      <DataTable
        id="sow-register"
        caption="SOW versions, newest first"
        rows={register.versions}
        rowKey={(v) => v.id}
        columns={sowColumns()}
        role={me.role ?? "staff"}
        empty={{
          title: "No SOW recorded",
          next: write ? "Record the first SOW: until then margin and the customer's price are not captured." : "Margin and the customer's price are not captured until commercial records a SOW.",
        }}
      />
      <Recompute tenantId={tenantId} register={register} write={write} onDone={(m) => { setMessage(m); loaded.reload(); }} />
    </section>
  );
}

// --- the rate card --------------------------------------------------------------------

function cardColumns(): Column<RateCardVersionView>[] {
  return [
    {
      key: "version",
      header: "Version",
      value: (v) => v.version,
      render: (v) => (
        <span className="flex flex-col gap-1">
          <span className="font-semibold text-ink">{v.version}</span>
          {v.in_force ? <HealthBadge band="healthy" label="In force today" /> : null}
        </span>
      ),
      sortable: true,
    },
    // effective_to is exclusive (where the next version begins): shown as its last day.
    { key: "from", header: "Effective", value: (v) => v.effective_from, render: (v) => `${date(v.effective_from)} – ${v.effective_to ? date(lastDayBefore(v.effective_to)) : "open"}`, sortable: true },
    { key: "currency", header: "Currency", value: (v) => v.document.currency },
    { key: "day", header: "Blended day rate", value: (v) => v.document.blended_day_rate, render: (v) => money(v.document.blended_day_rate, v.document.currency), className: "tabular-nums" },
    {
      key: "people",
      header: "People cost per month",
      value: (v) => v.document.people_cost.map((p) => p.location).join(", "),
      render: (v) => (
        <span className="flex flex-col tabular-nums">
          {v.document.people_cost.map((p) => (
            <span key={p.location}>
              {p.location}: {money(p.monthly_cost, v.document.currency)}
            </span>
          ))}
        </span>
      ),
    },
    { key: "roles", header: "Roles", value: (v) => v.document.roles.length, render: (v) => v.document.roles.map((r) => `${r.role} ${percent(r.share, 0)}`).join(", ") },
    { key: "published", header: "Published", value: (v) => v.published_at, render: (v) => dateTime(v.published_at), sortable: true },
    { key: "reason", header: "Why", value: (v) => v.reason, render: (v) => <span className="break-words">{v.reason}</span> },
  ];
}

function lastDayBefore(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

function tomorrow(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  return d.toISOString().slice(0, 10);
}

/** A new version starts as a copy of the version in force; the editable parts are
 * the ones Finance and commercial change. The catalogue, complexity minutes,
 * service multipliers, base pack bands and CI slabs carry over unchanged. */
function Publisher({ base, onSaved }: { base: RateCardVersionView; onSaved: (m: Message) => void }) {
  const { token } = useAuth();
  const [doc, setDoc] = useState<RateCardDocument>({ ...base.document, version: "", effective_from: tomorrow() });
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<RateCardDocument>) => setDoc((d) => ({ ...d, ...patch }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    const rates = [doc.blended_day_rate, doc.base_pack_floor, doc.corestack_consumption_rate, ...doc.people_cost.map((p) => p.monthly_cost), ...doc.tools.map((t) => t.cost_per_ci), ...doc.roles.map((r) => r.share)];
    if (rates.some((r) => !RATE.test(r.trim()))) {
      setError("Enter every amount and share as a number, such as 266.99 or 0.45.");
      return;
    }
    // Shares as tenths of a thousandth, in integers: no float sums.
    const units = (s: string) => {
      const [w = "0", f = ""] = s.trim().split(".");
      return BigInt(w) * 10n ** 16n + BigInt((f + "0".repeat(16)).slice(0, 16));
    };
    if (doc.roles.reduce((sum, r) => sum + units(r.share), 0n) !== 10n ** 16n) {
      setError("The role shares add up to exactly 1 (100%).");
      return;
    }
    if (reason.trim().length < 3) {
      setError("Say why this version is published: it is recorded with it.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const body: PublishRateCard = { document: { ...doc, version: doc.version.trim(), source: doc.source.trim() }, reason: reason.trim() };
      const saved = await apiPost(await token(), "/v1/commercial/rate-cards", {}, body);
      onSaved({ kind: "ok", text: `Rate card ${saved.version} published, in force from ${date(saved.effective_from)}.` });
    } catch (e) {
      setError(failure(e, "Publishing a rate card"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-4 card p-4" aria-labelledby="card-form-heading">
      <h3 id="card-form-heading" className="font-semibold text-ink">
        Publish a new version
      </h3>
      <p className="text-xs text-muted">
        Starts from {base.version}. A published version never changes: it takes effect from its date, after today, and the version before it ends
        that day. The catalogue, complexity minutes, service multipliers, base pack bands and CI slabs carry over from {base.version}.
      </p>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Field label="Version name">
          <input className={control} value={doc.version} required maxLength={40} pattern="[A-Za-z0-9][A-Za-z0-9._\-]*" onChange={(e) => set({ version: e.target.value })} />
        </Field>
        <Field label="Effective from">
          <input type="date" className={control} value={doc.effective_from} min={tomorrow()} required onChange={(e) => set({ effective_from: e.target.value })} />
        </Field>
        <Field label="Source" wide>
          <input className={control} value={doc.source} required maxLength={200} onChange={(e) => set({ source: e.target.value })} />
        </Field>
        <Field label={`Blended day rate (${doc.currency})`}>
          <input className={control} value={doc.blended_day_rate} required inputMode="decimal" onChange={(e) => set({ blended_day_rate: e.target.value })} />
        </Field>
        <Field label={`Base pack floor (${doc.currency})`}>
          <input className={control} value={doc.base_pack_floor} required inputMode="decimal" onChange={(e) => set({ base_pack_floor: e.target.value })} />
        </Field>
        <Field label="CoreStack rate on consumption (fraction)">
          <input className={control} value={doc.corestack_consumption_rate} required inputMode="decimal" onChange={(e) => set({ corestack_consumption_rate: e.target.value })} />
        </Field>
      </div>
      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold text-ink">People cost per person per month ({doc.currency})</legend>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {doc.people_cost.map((p, i) => (
            <Field key={p.location} label={p.location}>
              <input className={control} value={p.monthly_cost} required inputMode="decimal" onChange={(e) => set({ people_cost: doc.people_cost.map((q, j) => (j === i ? { ...q, monthly_cost: e.target.value } : q)) })} />
            </Field>
          ))}
        </div>
      </fieldset>
      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold text-ink">Roles and their share of the team (fractions adding up to 1)</legend>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {doc.roles.map((r, i) => (
            <Field key={r.role} label={r.role}>
              <input className={control} value={r.share} required inputMode="decimal" onChange={(e) => set({ roles: doc.roles.map((q, j) => (j === i ? { ...q, share: e.target.value } : q)) })} />
            </Field>
          ))}
        </div>
      </fieldset>
      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold text-ink">Tool cost per CI per month ({doc.currency})</legend>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {doc.tools.map((t, i) => (
            <Field key={t.tool} label={t.tool}>
              <input className={control} value={t.cost_per_ci} required inputMode="decimal" onChange={(e) => set({ tools: doc.tools.map((q, j) => (j === i ? { ...q, cost_per_ci: e.target.value } : q)) })} />
            </Field>
          ))}
        </div>
      </fieldset>
      <Field label="Why this version" wide>
        <input className={control} value={reason} required minLength={3} maxLength={500} onChange={(e) => setReason(e.target.value)} />
      </Field>
      {error ? (
        <p role="alert" className="text-sm text-band-critical">
          {error}
        </p>
      ) : null}
      <button type="submit" disabled={busy} className="btn btn-primary">
        {busy ? "Publishing…" : "Publish version"}
      </button>
    </form>
  );
}

function RateCardSection({ publish }: { publish: boolean }) {
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/commercial/rate-cards"), "rate-cards");
  const [message, setMessage] = useState<Message>(null);
  const [editing, setEditing] = useState(false);
  if (loaded.status === "loading") return <Loading what="the rate card" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="the rate card" />;
  const versions = loaded.data;
  const inForce = versions.find((v) => v.in_force) ?? versions[0] ?? null;
  const latest = versions[0] ?? null;
  return (
    <section aria-labelledby="card-heading" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="card-heading" className="section-title">
          Rate card
        </h2>
        {publish && latest && !editing ? (
          <button type="button" className="btn btn-primary" onClick={() => setEditing(true)}>
            Publish a new version
          </button>
        ) : null}
      </div>
      <p className="text-sm text-muted">
        The pricing engine&apos;s rates: what Pricing quotes and margin&apos;s modelled cost use.{" "}
        {inForce ? (
          <>
            In force today: <strong className="text-ink">{inForce.version}</strong>, in {inForce.document.currency}, blended day rate{" "}
            {money(inForce.document.blended_day_rate, inForce.document.currency)} ({decimal(inForce.document.working_days_per_month, 1)} working days a
            month).
          </>
        ) : (
          "No version is in force: Pricing cannot quote and margin is not computed."
        )}
      </p>
      <Feedback message={message} />
      {editing && latest ? (
        <Publisher
          base={latest}
          onSaved={(m) => {
            setEditing(false);
            setMessage(m);
            loaded.reload();
          }}
        />
      ) : null}
      <DataTable
        id="rate-card-versions"
        caption="Rate card versions, newest first"
        rows={versions}
        rowKey={(v) => v.id}
        columns={cardColumns()}
        role={me.role ?? "staff"}
        empty={{ title: "No rate card published", next: "Publish the first version to price quotes and compute margin." }}
      />
    </section>
  );
}

// --- the page --------------------------------------------------------------------------

export function CommercialSetup() {
  const me = useMe();
  const { tenantId: chosen } = useCustomer();
  const [tenantId, setTenantId] = useState<string | null>(chosen ?? me.tenants[0]?.id ?? null);
  // The top-bar selector preselects the customer; this page's own list can change it.
  useEffect(() => {
    if (chosen) setTenantId(chosen);
  }, [chosen]);
  const write = can(me, "commercial.write");
  const publish = can(me, "ratecard.publish");
  const tenants = [...me.tenants].sort((a, b) => a.name.localeCompare(b.name));
  return (
    <>
      <PageHeader
        title="Commercial setup"
        subtitle="SOWs and the rate card: what margin, pricing and the report packs compute from. Every change is a new version, recorded with who made it and why; nothing is edited."
      />
      <div className="space-y-10">
        <div className="space-y-4">
          <label className={`${labelStyle} max-w-sm`}>
            Customer
            <select className={control} value={tenantId ?? ""} onChange={(e) => setTenantId(e.target.value || null)}>
              {tenants.length === 0 ? <option value="">No customer in your scope</option> : null}
              {tenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          {tenantId ? <SowSection tenantId={tenantId} write={write} /> : null}
        </div>
        <RateCardSection publish={publish} />
      </div>
    </>
  );
}
