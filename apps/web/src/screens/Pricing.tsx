"use client";

// Pricing (step 16; HLD M12, ADR 0019): the pricing engine on the rate card in
// force. An inventory of catalogue resource types, the month's cloud consumption,
// coverage, support model and delivery location give a quote: service cost, base
// pack, platform cost, total, price per CI, the implied team, the slab and how
// far it is to the next one. The engine computes and stores nothing; the figures
// are modelled, and the rate card version is shown with them. EUR only.

import { useState } from "react";
import type { FormEvent } from "react";

import type { QuoteRequest, QuoteView, RateCardView } from "@cloudops/contracts";

import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { count, date, decimal, money } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const control = "w-full rounded-control border border-line bg-surface px-2 py-1.5 text-sm text-ink";
const labelStyle = "flex flex-col gap-1 text-xs font-medium text-muted";
const AMOUNT = /^\d{1,12}(\.\d{1,6})?$/;
const WHOLE = /^\d{1,7}$/;

const COVERAGES = ["8x5", "16x5", "24x7"] as const satisfies readonly QuoteRequest["coverage"][];
const SUPPORT = ["Bronze", "Silver", "Gold"] as const satisfies readonly QuoteRequest["support_model"][];
const LOCATIONS = ["India", "Budapest"] as const satisfies readonly QuoteRequest["location"][];

/** Only the values both the rate card offers and the API's schema accepts. */
function offered<T extends string>(allowed: readonly T[], card: readonly string[]): T[] {
  return allowed.filter((a) => card.includes(a));
}

function failure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "no_rate_card") return "No rate card is in force for that date.";
    if (error.code === "currency_not_supported") return "The rate card prices in EUR only.";
    if (error.status === 403) return "Pricing is not available to your role.";
    if (error.status === 422) return "The quote was not produced: check the counts and the consumption.";
  }
  return "The quote was not produced. Try again in a moment.";
}

function Quote({ quote }: { quote: QuoteView }) {
  const eur = (x: { value: string | number }) => money(String(x.value), quote.currency);
  const fte = (x: { value: string | number }) => `${decimal(String(x.value), 2)} FTE`;
  return (
    <section aria-labelledby="quote-heading" className="space-y-4" aria-live="polite">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="quote-heading" className="text-lg font-semibold text-ink">
          Quote
        </h2>
        <span className="text-sm text-muted">
          Rate card {quote.rate_card_version}, slab <strong className="text-ink">{quote.slab}</strong>
        </span>
        {quote.repricing_due ? <HealthBadge band="watch" label="Repricing due: close to the next slab" /> : null}
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Total per month" metric={quote.total_monthly} display={eur} />
        <MetricCard label="Price per CI" metric={quote.price_per_ci} display={eur} />
        <MetricCard label="Service cost" metric={quote.service_cost} display={eur} />
        <MetricCard label="Base pack" metric={quote.base_pack} display={eur} />
        <MetricCard label="Cloud services cost" metric={quote.cloud_services_cost} display={eur} />
        <MetricCard label="Platform cost" metric={quote.platform_cost} display={eur} />
        <MetricCard label="Critical CIs" metric={quote.critical_cis} display={(x) => count(Number(x.value))} />
        <MetricCard
          label="Until the next slab"
          metric={quote.cis_until_next_slab}
          display={(x) => `${count(Number(x.value))} CIs${quote.next_slab ? ` (to ${quote.next_slab})` : ""}`}
          unavailable="Already in the top slab"
        />
        <MetricCard label="Implied team" metric={quote.fte} display={fte} />
        <MetricCard label="Transition" metric={quote.transition_weeks} display={(x) => `${count(Number(x.value))} weeks`} />
      </div>
      {quote.team.length > 0 ? (
        <div className="rounded-card border border-line bg-surface p-4">
          <h3 className="mb-2 font-semibold text-ink">Team</h3>
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Implied team by role</caption>
            <thead>
              <tr>
                <th scope="col" className="py-1 font-semibold text-ink">
                  Role
                </th>
                <th scope="col" className="py-1 text-right font-semibold text-ink">
                  FTE
                </th>
              </tr>
            </thead>
            <tbody>
              {quote.team.map((t) => (
                <tr key={t.role} className="border-t border-line">
                  <td className="py-1 text-ink">{t.role}</td>
                  <td className="py-1 text-right tabular-nums text-ink">{decimal(t.fte.value, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2">
            <Provenance metric={quote.fte} />
          </div>
        </div>
      ) : null}
    </section>
  );
}

function QuoteForm({ card }: { card: RateCardView }) {
  const { token } = useAuth();
  const coverages = offered(COVERAGES, card.coverages);
  const support = offered(SUPPORT, card.support_models);
  const locations = offered(LOCATIONS, card.locations);
  const [counts, setCounts] = useState<Record<string, string>>({});
  const [consumption, setConsumption] = useState("");
  const [coverage, setCoverage] = useState<QuoteRequest["coverage"]>(coverages.includes("24x7") ? "24x7" : (coverages[0] ?? "24x7"));
  const [supportModel, setSupportModel] = useState<QuoteRequest["support_model"]>(support.includes("Gold") ? "Gold" : (support[0] ?? "Gold"));
  const [location, setLocation] = useState<QuoteRequest["location"]>(locations[0] ?? "India");
  const [corestack, setCorestack] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [quote, setQuote] = useState<QuoteView | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const inventory: Record<string, number> = {};
    for (const [type, raw] of Object.entries(counts)) {
      const text = raw.trim();
      if (text === "") continue;
      if (!WHOLE.test(text)) {
        setError(`Enter a whole number of resources for ${type}.`);
        return;
      }
      const n = Number(text);
      if (n > 0) inventory[type] = n;
    }
    if (Object.keys(inventory).length === 0) {
      setError("Enter a count for at least one resource type.");
      return;
    }
    const amount = consumption.trim() || "0";
    if (!AMOUNT.test(amount)) {
      setError("Enter the monthly consumption in EUR as a number, such as 25000 or 25000.50.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const body: QuoteRequest = {
        inventory,
        monthly_consumption: amount,
        currency: card.currency,
        coverage,
        support_model: supportModel,
        location,
        corestack,
      };
      setQuote(await apiPost(await token(), "/v1/commercial/quotes", {}, body));
    } catch (e) {
      setQuote(null);
      setError(failure(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <form onSubmit={(e) => void submit(e)} className="space-y-4 rounded-card border border-line bg-surface p-4" aria-label="Quote">
        <fieldset className="space-y-2">
          <legend className="font-semibold text-ink">Inventory: critical CIs by resource type</legend>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {card.resource_types.map((type) => (
              <label key={type} className={labelStyle}>
                {type}
                <input
                  className={control}
                  inputMode="numeric"
                  autoComplete="off"
                  value={counts[type] ?? ""}
                  onChange={(e) => setCounts({ ...counts, [type]: e.target.value })}
                />
              </label>
            ))}
          </div>
        </fieldset>
        <fieldset className="space-y-2">
          <legend className="font-semibold text-ink">Service</legend>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <label className={labelStyle}>
              Monthly cloud consumption ({card.currency})
              <input className={control} inputMode="decimal" autoComplete="off" value={consumption} onChange={(e) => setConsumption(e.target.value)} />
            </label>
            <label className={labelStyle}>
              Coverage
              <select className={control} value={coverage} onChange={(e) => setCoverage(coverages.find((c) => c === e.target.value) ?? coverage)}>
                {coverages.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className={labelStyle}>
              Support model
              <select className={control} value={supportModel} onChange={(e) => setSupportModel(support.find((s) => s === e.target.value) ?? supportModel)}>
                {support.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <label className={labelStyle}>
              Delivery location
              <select className={control} value={location} onChange={(e) => setLocation(locations.find((l) => l === e.target.value) ?? location)}>
                {locations.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="flex items-center gap-2 text-sm text-ink">
            <input type="checkbox" checked={corestack} onChange={(e) => setCorestack(e.target.checked)} />
            Include CoreStack
          </label>
        </fieldset>
        {error ? (
          <p role="alert" className="text-sm text-band-critical">
            {error}
          </p>
        ) : null}
        <button type="submit" disabled={busy} className="rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink disabled:opacity-60">
          {busy ? "Pricing…" : "Price it"}
        </button>
      </form>
      {quote ? <Quote quote={quote} /> : null}
    </div>
  );
}

export function Pricing() {
  const loaded = useApi((token) => apiGet(token, "/v1/commercial/rate-card"), "rate-card");
  const header = (card?: RateCardView) => (
    <PageHeader
      title="Pricing"
      subtitle={
        card ? (
          <>
            Rate card <strong className="text-ink">{card.version}</strong>, in force from {date(card.effective_from)}
            {card.effective_to ? ` to ${date(card.effective_to)}` : ""}, in {card.currency}. Quotes are modelled and are not stored.
          </>
        ) : (
          "Quote a managed service from an inventory, on the rate card in force."
        )
      }
    />
  );
  if (loaded.status === "loading") return <>{header()}<Loading what="the rate card" /></>;
  if (loaded.status === "error") return <>{header()}<LoadError error={loaded.error} what="rate card" /></>;
  return (
    <>
      {header(loaded.data)}
      <QuoteForm card={loaded.data} />
    </>
  );
}
