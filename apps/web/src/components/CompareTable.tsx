// A figure per component, for one date range or two side by side (ADR 0038). A real
// table: each row names its component, each column its range, and the change is
// said in words (with its sign), never by colour alone. A modelled figure carries
// the modelled mark; a figure that is not available says why instead of a zero.
// Cells wrap rather than scroll, so the table fits a tablet's width.

import type { ReactNode } from "react";

import { count, decimal, money, percent } from "@/lib/format";
import { subtractDecimal } from "@/lib/range";

import { ModelledMark, Provenance, sourceLabel, type AnyMetric } from "./Provenance";

const CURRENCY = /^[A-Z]{3}$/;

/** A metric's value as text, by its unit: money in its currency, a ratio as a
 * percentage, a count grouped; rounded once, here. */
export function metricText(m: AnyMetric): string {
  const value = String(m.value);
  if (CURRENCY.test(m.unit)) return money(value, m.unit);
  if (m.unit === "ratio") return percent(value);
  if (m.unit === "percent") return `${decimal(value, 1)}%`;
  if (m.unit === "points") return decimal(value, 0);
  if (m.unit === "hours") return `${decimal(value, 1)} h`;
  return count(Number(m.value));
}

/** The change from b to a in the metric's own unit, signed: "+3", "−EUR 120.00",
 * "+4.0 pts". Null when the two cannot be compared. */
export function changeText(a: AnyMetric, b: AnyMetric): string | null {
  if (a.unit !== b.unit) return null;
  const diff = subtractDecimal(String(a.value), String(b.value));
  const negative = diff.startsWith("-");
  const size = negative ? diff.slice(1) : diff;
  const sign = /^[0.]*$/.test(size) ? "±" : negative ? "−" : "+";
  if (CURRENCY.test(a.unit)) return `${sign}${money(size, a.unit)}`;
  if (a.unit === "ratio") return `${sign}${percent(size).replace("%", "")} pts`;
  if (a.unit === "percent") return `${sign}${decimal(size, 1)} pts`;
  if (a.unit === "points") return `${sign}${decimal(size, 0)}`;
  return `${sign}${count(Number(size))}`;
}

export interface CompareRow {
  key: string;
  label: ReactNode;
  a: AnyMetric | null | undefined;
  b?: AnyMetric | null | undefined;
  /** Said in place of a figure the range has none of: never drawn as zero. */
  missing?: string;
  /** Emphasise a total row. */
  total?: boolean;
}

function Cell({ metric, missing }: { metric: AnyMetric | null | undefined; missing: string }) {
  if (!metric) return <span className="text-muted">{missing}</span>;
  return (
    <span title={`${sourceLabel(metric.source_system)} · ${metric.query_ref}`}>
      <span className="tabular-nums text-ink">{metricText(metric)}</span>
      {metric.is_modelled ? (
        <span className="ml-1 inline-block align-middle text-xs">
          <ModelledMark />
        </span>
      ) : null}
    </span>
  );
}

export function CompareTable({
  caption,
  component = "Component",
  a,
  b,
  rows,
  provenance,
  change = true,
}: {
  caption: string;
  component?: string;
  /** The column heading for each range: its label. */
  a: string;
  b?: string;
  rows: CompareRow[];
  /** The metric whose source and as-of time the table states under it. */
  provenance?: AnyMetric | null;
  /** Off where the two columns are two figures, not two ranges (cost and price). */
  change?: boolean;
}) {
  const comparing = b !== undefined;
  return (
    <div className="min-w-0">
      <table className="w-full border-collapse text-sm" data-compare={comparing ? "two" : "one"}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="text-left text-xs text-muted">
            <th scope="col" className="pb-1 pr-2 font-semibold">
              {component}
            </th>
            <th scope="col" className="pb-1 pl-2 text-right font-semibold">
              {a}
            </th>
            {comparing ? (
              <>
                <th scope="col" className="pb-1 pl-2 text-right font-semibold">
                  {b}
                </th>
                {change ? (
                  <th scope="col" className="pb-1 pl-2 text-right font-semibold">
                    Change
                  </th>
                ) : null}
              </>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const delta = comparing && change && r.a && r.b ? changeText(r.a, r.b) : null;
            return (
              <tr key={r.key} className={`border-t border-line-subtle align-top ${r.total ? "font-semibold" : ""}`}>
                <th scope="row" className={`py-1.5 pr-2 text-left ${r.total ? "font-semibold" : "font-normal"} text-ink`}>
                  {r.label}
                </th>
                <td className="py-1.5 pl-2 text-right">
                  <Cell metric={r.a} missing={r.missing ?? "—"} />
                </td>
                {comparing ? (
                  <>
                    <td className="py-1.5 pl-2 text-right">
                      <Cell metric={r.b} missing={r.missing ?? "—"} />
                    </td>
                    {change ? <td className="py-1.5 pl-2 text-right tabular-nums text-muted">{delta ?? "—"}</td> : null}
                  </>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
      {provenance ? (
        <div className="mt-2 border-t border-line-subtle pt-2">
          <Provenance metric={provenance} />
        </div>
      ) : null}
    </div>
  );
}
