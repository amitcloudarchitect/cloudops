"use client";

// What every home section shares (ADR 0038): its frame, its date range and the
// optional second range it is compared with, and loading both under the customer
// chosen in the top bar. Each section reads its own endpoint once per range.

import Link from "next/link";
import { useId, useState, type ReactNode } from "react";

import { Body } from "@/components/Widget";
import { customerQuery, useCustomer } from "@/lib/customer";
import { PRESETS, presetRange, previousRange, rangeLabel, rangeProblem, today, type DateRange, type Preset } from "@/lib/range";
import type { Loaded } from "@/lib/useApi";

export interface RangeChoice {
  preset: Preset | "custom";
  a: DateRange;
  /** The range compared with, or null when not comparing. */
  b: DateRange | null;
}

export function useRangeChoice(initial: Preset = "mtd"): [RangeChoice, (next: RangeChoice) => void] {
  const [choice, setChoice] = useState<RangeChoice>(() => ({ preset: initial, a: presetRange(initial, today()), b: null }));
  return [choice, setChoice];
}

/** The query for one range under the customer chosen in the top bar. */
export function useRangeQuery(range: DateRange | null, extra: Record<string, string | undefined> = {}): Record<string, string | undefined> | null {
  const { tenantId } = useCustomer();
  if (!range || rangeProblem(range)) return null;
  return { start: range.start, end: range.end, ...customerQuery(tenantId), ...extra };
}

export function queryKey(path: string, query: Record<string, string | undefined> | null): string {
  return query ? `${path}?${new URLSearchParams(Object.entries(query).filter((e): e is [string, string] => e[1] !== undefined)).toString()}` : `${path}#none`;
}

function DateField({ label, value, onChange, describedBy }: { label: string; value: string; onChange: (v: string) => void; describedBy?: string }) {
  return (
    <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
      {label}
      <input type="date" className="field" value={value} aria-describedby={describedBy} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

export function RangeControls({ section, choice, onChange, children }: { section: string; choice: RangeChoice; onChange: (next: RangeChoice) => void; children?: ReactNode }) {
  const id = useId();
  const problemA = rangeProblem(choice.a);
  const problemB = choice.b ? rangeProblem(choice.b) : null;
  return (
    <fieldset className="mb-4 flex flex-wrap items-end gap-3 rounded-control border border-line-subtle p-3">
      <legend className="sr-only">Date range for {section}</legend>
      <div className="flex flex-col gap-1 text-xs font-semibold text-muted">
        <label htmlFor={`${id}-range`}>Range</label>
        <select
          id={`${id}-range`}
          className="field"
          value={choice.preset}
          onChange={(e) => {
            const preset = e.target.value as Preset | "custom";
            if (preset === "custom") return onChange({ ...choice, preset });
            const a = presetRange(preset, today());
            onChange({ preset, a, b: choice.b ? previousRange(a) : null });
          }}
        >
          {PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
          <option value="custom">Custom</option>
        </select>
      </div>
      <DateField label="From" value={choice.a.start} describedBy={problemA ? `${id}-a` : undefined} onChange={(start) => onChange({ ...choice, preset: "custom", a: { ...choice.a, start } })} />
      <DateField label="To" value={choice.a.end} describedBy={problemA ? `${id}-a` : undefined} onChange={(end) => onChange({ ...choice, preset: "custom", a: { ...choice.a, end } })} />
      <label className="flex items-center gap-2 self-center text-sm text-ink">
        <input type="checkbox" checked={choice.b !== null} onChange={(e) => onChange({ ...choice, b: e.target.checked ? previousRange(choice.a) : null })} />
        Compare with another range
      </label>
      {choice.b ? (
        <div className="flex flex-wrap items-end gap-3" role="group" aria-label={`Compared range for ${section}`}>
          <DateField label="Compare from" value={choice.b.start} describedBy={problemB ? `${id}-b` : undefined} onChange={(start) => choice.b && onChange({ ...choice, b: { ...choice.b, start } })} />
          <DateField label="Compare to" value={choice.b.end} describedBy={problemB ? `${id}-b` : undefined} onChange={(end) => choice.b && onChange({ ...choice, b: { ...choice.b, end } })} />
        </div>
      ) : null}
      {children}
      {problemA ? (
        <p id={`${id}-a`} role="alert" className="w-full text-sm text-band-critical">
          {problemA}
        </p>
      ) : null}
      {problemB ? (
        <p id={`${id}-b`} role="alert" className="w-full text-sm text-band-critical">
          Compared range: {problemB}
        </p>
      ) : null}
    </fieldset>
  );
}

/** The column headings for the two ranges. */
export function rangeHeadings(choice: RangeChoice): { a: string; b?: string } {
  return choice.b ? { a: rangeLabel(choice.a), b: rangeLabel(choice.b) } : { a: rangeLabel(choice.a) };
}

export function HomeSection({ id, title, subtitle, href, hrefLabel, children }: { id: string; title: string; subtitle?: ReactNode; href?: string; hrefLabel?: string; children: ReactNode }) {
  const heading = `section-${id}`;
  return (
    <section aria-labelledby={heading} data-section={id} className="card min-w-0 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="max-w-3xl">
          <h2 id={heading} className="font-display text-xl font-bold text-ink">
            {title}
          </h2>
          {subtitle ? <p className="mt-1 text-sm text-muted">{subtitle}</p> : null}
        </div>
        {href ? (
          <Link href={href} className="link text-xs font-semibold">
            {hrefLabel ?? "View the evidence"}
            <span className="sr-only"> for {title}</span>
          </Link>
        ) : null}
      </div>
      {children}
    </section>
  );
}

/** Both ranges loaded, or the first one's loading or refusal state. The compared
 * range's own failure is shown in its place, never blanking the first. */
export function Both<T>({ a, b, what, children }: { a: Loaded<T>; b: Loaded<T | null>; what: string; children: (a: T, b: T | null) => ReactNode }) {
  return (
    <Body loaded={a} what={what}>
      {(first) => {
        if (b.status === "loading") return children(first, null);
        if (b.status === "error") {
          return (
            <>
              <p role="status" className="mb-2 text-sm text-muted">
                The compared range could not be loaded. Showing the first range only.
              </p>
              {children(first, null)}
            </>
          );
        }
        return children(first, b.data);
      }}
    </Body>
  );
}

/** A subheading inside a section. */
export function Part({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <h3 className="section-title mb-2">{title}</h3>
      {children}
    </div>
  );
}
