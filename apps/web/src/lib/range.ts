// The date range a home section reads (ADR 0038), and the range it is compared
// with. Days are ISO dates ("2026-10-31"), both inclusive, in UTC as the API counts
// them. The API refuses a range backwards or longer than a year; so does this, so a
// bad range is said here and never sent.

import { plain } from "./format";
import { sumDecimal } from "./commercial";

export interface DateRange {
  start: string;
  end: string;
}

export type Preset = "mtd" | "last_month" | "last_30" | "last_90" | "ytd";

export const PRESETS: readonly { id: Preset; label: string }[] = [
  { id: "mtd", label: "This month to date" },
  { id: "last_month", label: "Last month" },
  { id: "last_30", label: "Last 30 days" },
  { id: "last_90", label: "Last 90 days" },
  { id: "ytd", label: "Year to date" },
];

export const MAX_RANGE_DAYS = 366;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const DAY_MS = 86_400_000;

function iso(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function utc(day: string): Date {
  return new Date(`${day}T00:00:00Z`);
}

function addDays(day: string, n: number): string {
  return iso(new Date(utc(day).getTime() + n * DAY_MS));
}

function lastDayOfMonth(year: number, month0: number): number {
  return new Date(Date.UTC(year, month0 + 1, 0)).getUTCDate();
}

export function today(now: Date = new Date()): string {
  return iso(now);
}

export function presetRange(preset: Preset, now: string): DateRange {
  const d = utc(now);
  const y = d.getUTCFullYear();
  const m = d.getUTCMonth();
  switch (preset) {
    case "mtd":
      return { start: iso(new Date(Date.UTC(y, m, 1))), end: now };
    case "last_month":
      return { start: iso(new Date(Date.UTC(y, m - 1, 1))), end: iso(new Date(Date.UTC(y, m, 0))) };
    case "last_30":
      return { start: addDays(now, -29), end: now };
    case "last_90":
      return { start: addDays(now, -89), end: now };
    case "ytd":
      return { start: iso(new Date(Date.UTC(y, 0, 1))), end: now };
  }
}

/** Days in the range, both ends counted. */
export function rangeDays(r: DateRange): number {
  return Math.round((utc(r.end).getTime() - utc(r.start).getTime()) / DAY_MS) + 1;
}

/** Why the API would refuse this range, or null when it is fine. */
export function rangeProblem(r: DateRange): string | null {
  if (!ISO_DAY.test(r.start) || !ISO_DAY.test(r.end) || Number.isNaN(utc(r.start).getTime()) || Number.isNaN(utc(r.end).getTime())) {
    return "Choose both a start and an end date.";
  }
  if (r.end < r.start) return "The range ends before it starts.";
  if (rangeDays(r) > MAX_RANGE_DAYS) return "A range is at most a year.";
  return null;
}

/** The default comparison: a range starting on the 1st is compared with the same
 * days of the month before (the whole month, when the range is a whole month);
 * any other with the same number of days just before it. */
export function previousRange(r: DateRange): DateRange {
  const s = utc(r.start);
  const e = utc(r.end);
  const monthAligned = s.getUTCDate() === 1 && rangeDays(r) <= 31 && s.getUTCMonth() === e.getUTCMonth();
  if (monthAligned) {
    const y = s.getUTCFullYear();
    const m = s.getUTCMonth() - 1;
    const wholeMonth = e.getUTCDate() === lastDayOfMonth(e.getUTCFullYear(), e.getUTCMonth());
    const last = lastDayOfMonth(y + Math.floor(m / 12), ((m % 12) + 12) % 12);
    const endDay = wholeMonth ? last : Math.min(e.getUTCDate(), last);
    return { start: iso(new Date(Date.UTC(y, m, 1))), end: iso(new Date(Date.UTC(y, m, endDay))) };
  }
  const days = rangeDays(r);
  const end = addDays(r.start, -1);
  return { start: addDays(end, -(days - 1)), end };
}

const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const MONTH = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });

/** "October 2026" for a whole month, else "1 Oct 2026 – 17 Oct 2026". */
export function rangeLabel(r: DateRange): string {
  const s = utc(r.start);
  const e = utc(r.end);
  if (s.getUTCDate() === 1 && s.getUTCMonth() === e.getUTCMonth() && s.getUTCFullYear() === e.getUTCFullYear()
    && e.getUTCDate() === lastDayOfMonth(e.getUTCFullYear(), e.getUTCMonth())) {
    return MONTH.format(s);
  }
  return r.start === r.end ? DAY.format(s) : `${DAY.format(s)} – ${DAY.format(e)}`;
}

export function rangeQuery(r: DateRange): { start: string; end: string } {
  return { start: r.start, end: r.end };
}

/** a − b, exactly, on the digits (never through a float). */
export function subtractDecimal(a: string, b: string): string {
  const negated = plain(b).startsWith("-") ? plain(b).slice(1) : `-${plain(b)}`;
  return sumDecimal([a, negated]);
}
