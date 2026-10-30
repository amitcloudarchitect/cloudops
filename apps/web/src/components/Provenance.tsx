// Where a number came from and when (HLD, UI patterns): every figure the
// console shows carries its source and as-of time; a modelled value is marked
// apart from an observed one, in words as well as colour.

import type { MetricDecimal, MetricInt } from "@cloudops/contracts";

import { dateTime } from "@/lib/format";

export type AnyMetric = MetricInt | MetricDecimal;

const SOURCE_LABEL: Record<string, string> = {
  servicenow: "ServiceNow",
  qualys: "Qualys",
  cloud_cost: "Cloud billing",
  platform_inventory: "Platform inventory",
  cloudops_ai_gateway: "AI gateway",
};

export function sourceLabel(source: string): string {
  return SOURCE_LABEL[source] ?? source;
}

export function Provenance({ metric }: { metric: AnyMetric }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted" title={metric.query_ref}>
      <span>
        <span className="sr-only">Source: </span>
        {sourceLabel(metric.source_system)}
      </span>
      <span aria-hidden="true">·</span>
      <span>
        <span className="sr-only">As of </span>
        <time dateTime={metric.as_of}>{dateTime(metric.as_of)}</time>
      </span>
      {metric.is_modelled ? (
        <span className="rounded-control border border-dashed border-modelled bg-modelled-bg px-1.5 font-medium text-modelled">
          Modelled
        </span>
      ) : null}
    </span>
  );
}
