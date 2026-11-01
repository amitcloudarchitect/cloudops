// A bar chart that is its own table (ADR 0030): each row names its label and
// value in text, and the bar beside them is decoration (aria-hidden). Screen
// readers, keyboard users and print get the table; nothing is said by colour or
// length alone. One series per chart, so there is no legend: the caption names it.
// Bars use the token file's neutral line colour, or a health band's colour when
// the band is also named in the row.

import type { ReactNode } from "react";

import type { Band } from "@/lib/health";

export interface Bar {
  key: string;
  label: ReactNode;
  /** A non-negative magnitude, for the bar's length only. Text shows `text`. */
  size: number;
  text: ReactNode;
  band?: Band;
  /** Extra cells after the value, each with its header in `extra`. */
  cells?: ReactNode[];
  /** Hover text for the bar: the row in words. */
  title?: string;
}

const FILL: Record<Band, string> = {
  healthy: "fill-band-healthy",
  watch: "fill-band-watch",
  "at-risk": "fill-band-at-risk",
  critical: "fill-band-critical",
  unknown: "fill-band-unknown",
};

export function BarTable({
  caption,
  labelHeader,
  valueHeader,
  extra = [],
  rows,
  hideCaption = true,
}: {
  caption: string;
  labelHeader: string;
  valueHeader: string;
  extra?: string[];
  rows: Bar[];
  hideCaption?: boolean;
}) {
  const max = Math.max(0, ...rows.map((r) => r.size));
  return (
    <table className="w-full table-fixed border-collapse text-sm">
      <caption className={hideCaption ? "sr-only" : "mb-2 text-left text-xs font-semibold text-muted"}>{caption}</caption>
      <thead>
        <tr className="text-left text-xs text-muted">
          <th scope="col" className="w-2/5 pb-1 font-semibold">
            {labelHeader}
          </th>
          <th scope="col" className="pb-1 font-semibold">
            {valueHeader}
          </th>
          {extra.map((h) => (
            <th key={h} scope="col" className="w-24 pb-1 text-right font-semibold">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key} className="border-t border-line-subtle align-middle">
            <th scope="row" className="truncate py-1.5 pr-2 text-left font-normal text-ink">
              {r.label}
            </th>
            <td className="py-1.5">
              <div className="flex items-center gap-2" title={r.title}>
                <span className="w-16 shrink-0 text-right tabular-nums text-ink">{r.text}</span>
                {/* SVG attributes, not an inline style: the CSP allows no style attribute. */}
                <svg aria-hidden="true" focusable="false" className="h-2 min-w-0 flex-1" preserveAspectRatio="none">
                  <rect width="100%" height="100%" rx="2" className="fill-surface-raised" />
                  <rect
                    width={`${max > 0 ? Math.max(r.size > 0 ? 2 : 0, (r.size / max) * 100) : 0}%`}
                    height="100%"
                    rx="2"
                    className={r.band ? FILL[r.band] : "fill-line"}
                  />
                </svg>
              </div>
            </td>
            {(r.cells ?? []).map((cell, i) => (
              <td key={extra[i] ?? i} className="py-1.5 pl-2 text-right whitespace-nowrap tabular-nums text-ink">
                {cell}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
