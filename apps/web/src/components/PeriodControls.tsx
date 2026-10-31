"use client";

// The month and reporting currency a commercial screen shows, kept in the address
// so a link to a customer, a reload or a shared URL keeps the same view. Values are
// checked against the API's own patterns before they are sent.

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useId } from "react";

import { currencyParam, PERIOD_PATTERN, periodParam, REPORTING_CURRENCIES } from "@/lib/commercial";

export interface PeriodChoice {
  /** Undefined: the API's default month. */
  period: string | undefined;
  /** Undefined: no conversion. */
  currency: string | undefined;
}

export function usePeriodChoice(): PeriodChoice & { set: (next: Partial<PeriodChoice>) => void; query: string } {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const period = periodParam(params.get("period"));
  const currency = currencyParam(params.get("currency"));
  const search = new URLSearchParams();
  if (period) search.set("period", period);
  if (currency) search.set("currency", currency);
  const query = search.toString();

  function set(next: Partial<PeriodChoice>) {
    const merged = { period, currency, ...next };
    const out = new URLSearchParams();
    if (merged.period) out.set("period", merged.period);
    if (merged.currency) out.set("currency", merged.currency);
    const qs = out.toString();
    router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
  }

  return { period, currency, set, query };
}

const control = "field";

export function PeriodControls({
  choice,
  shownPeriod,
  currencyHelp,
}: {
  choice: ReturnType<typeof usePeriodChoice>;
  /** The month the API answered for, shown when none was chosen. */
  shownPeriod: string | undefined;
  currencyHelp: string;
}) {
  const help = useId();
  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
        Month
        <input
          type="month"
          className={control}
          value={choice.period ?? shownPeriod ?? ""}
          onChange={(e) => {
            const value = e.target.value;
            if (value === "" || PERIOD_PATTERN.test(value)) choice.set({ period: value || undefined });
          }}
        />
      </label>
      <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
        Reporting currency
        <select
          className={control}
          aria-describedby={help}
          value={choice.currency ?? ""}
          onChange={(e) => choice.set({ currency: e.target.value || undefined })}
        >
          <option value="">None (as billed)</option>
          {REPORTING_CURRENCIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>
      <p id={help} className="max-w-sm text-xs text-muted">
        {currencyHelp}
      </p>
    </div>
  );
}
