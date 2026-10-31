// Shared wording and controls for the commercial screens (step 16: FinOps, margin,
// pricing). Words only: every figure, verdict and band comes from the API.

import type { AccountCheck, CustomerCost, MarginView } from "@cloudops/contracts";

import { percent, plain } from "./format";

/** Reporting currencies a viewer may convert into; "" keeps transaction or contract currency. */
export const REPORTING_CURRENCIES = ["EUR", "GBP", "USD"] as const;

export const PERIOD_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;
export const CURRENCY_PATTERN = /^[A-Z]{3}$/;

/** A period or currency from the address bar, only if it is well formed. */
export function periodParam(value: string | null): string | undefined {
  return value && PERIOD_PATTERN.test(value) ? value : undefined;
}

export function currencyParam(value: string | null): string | undefined {
  return value && CURRENCY_PATTERN.test(value) ? value : undefined;
}

/** "2026-09" as "September 2026". */
export function periodLabel(period: string): string {
  const [year, month] = period.split("-");
  const names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const name = names[Number(month) - 1];
  return name ? `${name} ${year}` : period;
}

export function chargeClassLabel(charge: string): string {
  const text = charge.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Why an account's figure is unverified, in words. */
export function checkReason(check: AccountCheck): string {
  const against = check.kind === "invoice" ? "the invoice" : "the provider’s total";
  switch (check.reason) {
    case "drift":
      return check.drift ? `drifts ${percent(check.drift.value)} from ${against}` : `drifts from ${against}`;
    case "currency_mismatch":
      return `the ledger and ${against} are in different currencies`;
    case "reference_zero":
      return `${against} is zero, but the ledger is not`;
    case "no_reference":
      return `no ${check.kind === "invoice" ? "invoice" : "provider total"} to check against yet`;
    case "mixed_currency":
      return "the account is billed in more than one currency";
    case null:
      return check.status === "verified" ? `matches ${against}` : "not checked";
    default:
      return check.reason;
  }
}

/** The customer's verdict in words: the first unverified account's reason, and how many more. */
export function verificationReason(cost: CustomerCost): string | null {
  if (cost.verification === "verified") return null;
  const failing = cost.checks.filter((c) => c.status === "unverified");
  const first = failing[0];
  if (!first) {
    return cost.checks.length === 0 ? "no reconciliation check has run for this period" : "an account with cost has not been checked";
  }
  const more = failing.length - 1;
  return `${first.account_name} ${checkReason(first)}${more > 0 ? ` (and ${more} more account${more === 1 ? "" : "s"})` : ""}`;
}

const NOT_CAPTURED: Record<NonNullable<MarginView["not_captured"]>, string> = {
  no_sow: "no SOW",
  no_people_cost: "no people cost for the month",
  no_fx_rate: "no FX rate for the contract currency",
  not_computed: "not computed for this month yet",
  no_cost_data: "no cost data for the month",
};

export function notCapturedReason(reason: NonNullable<MarginView["not_captured"]>): string {
  return NOT_CAPTURED[reason];
}

export function statusLabel(m: Pick<MarginView, "status" | "restated">): string {
  if (m.status === null) return "";
  const base = m.status === "provisional" ? "Provisional" : "Closed";
  return m.restated ? `${base}, restated` : base;
}

/** Exact sum of decimal strings, on their digits (never through a float): the
 * result keeps the longest fraction among the inputs and is rounded only at display. */
export function sumDecimal(values: readonly string[]): string {
  const parsed = values.map((v) => {
    const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(plain(v));
    if (!match) throw new Error(`not a decimal: ${v}`);
    return { sign: match[1] === "-" ? -1n : 1n, whole: match[2] ?? "0", fraction: match[3] ?? "" };
  });
  const scale = Math.max(0, ...parsed.map((p) => p.fraction.length));
  let total = 0n;
  for (const p of parsed) total += p.sign * BigInt(p.whole + p.fraction.padEnd(scale, "0"));
  const negative = total < 0n;
  const digits = (negative ? -total : total).toString().padStart(scale + 1, "0");
  const whole = digits.slice(0, digits.length - scale);
  const fraction = scale > 0 ? `.${digits.slice(digits.length - scale)}` : "";
  return `${negative ? "-" : ""}${whole}${fraction}`;
}

/** For sorting a column only; display always goes through decimal()/money(). */
export function sortKey(value: string | null | undefined): number | null {
  return value === null || value === undefined ? null : Number(value);
}
