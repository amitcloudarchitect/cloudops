// Metric card (HLD, UI patterns): value, label, trend, as-of, and a link to the
// evidence behind the number. A metric the caller may not see is shown as not
// available, never as zero.

import Link from "next/link";

import { BAND_LABEL, type Band } from "@/lib/health";

import { BAND_EDGE, HealthBadge } from "./HealthBadge";
import { Provenance, type AnyMetric } from "./Provenance";

export interface Trend {
  direction: "up" | "down" | "flat";
  text: string;
  /** Whether this direction is good news; drives wording, not only colour. */
  better: boolean;
}

interface Props {
  label: string;
  metric: AnyMetric | null | undefined;
  display?: (metric: AnyMetric) => string;
  band?: Band;
  trend?: Trend;
  href?: string;
  unavailable?: string;
}

const ARROW = { up: "↑", down: "↓", flat: "→" } as const;

export function MetricCard({ label, metric, display, band, trend, href, unavailable }: Props) {
  const edge = band ? BAND_EDGE[band] : "border-l-line";
  const modelled = metric?.is_modelled ?? false;
  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-medium text-muted">{label}</h3>
        {band && metric ? <HealthBadge band={band} /> : null}
      </div>
      {metric ? (
        <>
          <p className="mt-1 text-2xl font-semibold tabular-nums text-ink">
            {display ? display(metric) : String(metric.value)}
            <span className="sr-only">
              {" "}
              {metric.unit}
              {band ? `, ${BAND_LABEL[band]}` : ""}
            </span>
          </p>
          {trend ? (
            <p className="text-xs text-muted">
              <span aria-hidden="true">{ARROW[trend.direction]} </span>
              {trend.text}
              <span className="sr-only">{trend.better ? " (improving)" : " (worsening)"}</span>
            </p>
          ) : null}
          <div className="mt-2">
            <Provenance metric={metric} />
          </div>
        </>
      ) : (
        <p className="mt-1 text-sm text-muted">{unavailable ?? "Not available to your role"}</p>
      )}
    </>
  );
  const frame = `block rounded-card border border-line border-l-4 ${edge} bg-surface p-4 ${
    modelled ? "border-dashed" : ""
  }`;
  return href && metric ? (
    // The visible text stays the link's name (WCAG 2.5.3); the hint is added to it.
    <Link href={href} className={`${frame} hover:bg-surface-raised`}>
      {body}
      <span className="mt-2 block text-xs font-medium text-accent underline">View the evidence</span>
    </Link>
  ) : (
    <div className={frame}>{body}</div>
  );
}
