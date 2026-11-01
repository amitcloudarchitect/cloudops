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
  cloudops_ingest: "Ingest run log",
  cloudops_policy: "Platform policy",
  cloudops_quarantine: "Quarantine",
  billing_exports: "Billing exports",
  provider_billing: "Provider billing total",
  reconciliation: "Reconciliation",
  margin_engine: "Margin engine",
  sow_register: "SOW register",
  ecb: "ECB reference rates",
  triage_classifier: "Triage classifier",
  customer_price: "Price charged",
  audit_log: "Action log",
  triage_review: "Triage reviews",
  cloud_audit: "Cloud audit",
  cost_fact: "Cost facts",
};

export function sourceLabel(source: string): string {
  if (source.startsWith("rate_card:")) return `Rate card ${source.slice("rate_card:".length)}`;
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
      {metric.is_modelled ? <ModelledMark /> : null}
    </span>
  );
}

/** The one marker for a modelled value, in words as well as colour. */
export function ModelledMark() {
  return (
    <span className="rounded-full border border-dashed border-modelled bg-modelled-bg px-2 font-semibold text-modelled">
      Modelled
    </span>
  );
}
