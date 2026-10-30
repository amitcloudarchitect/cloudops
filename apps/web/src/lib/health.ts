// Health banding: one scale across the whole product (HLD, UI patterns). Every
// screen that colours a value by how worrying it is goes through band(); the
// colours are the band-* tokens.

export type Band = "healthy" | "watch" | "at-risk" | "critical" | "unknown";

export const BAND_LABEL: Record<Band, string> = {
  healthy: "Healthy",
  watch: "Watch",
  "at-risk": "At risk",
  critical: "Critical",
  unknown: "Unknown",
};

/** Thresholds where a higher value is worse: [watch, at-risk, critical], each inclusive. */
export type Thresholds = readonly [number, number, number];

export function band(value: number | null | undefined, [watch, atRisk, critical]: Thresholds): Band {
  if (value === null || value === undefined || Number.isNaN(value)) return "unknown";
  if (value >= critical) return "critical";
  if (value >= atRisk) return "at-risk";
  if (value >= watch) return "watch";
  return "healthy";
}

// The product's thresholds, in one place so a screen cannot invent its own.
export const THRESHOLDS = {
  slaBreachedOpen: [1, 3, 10],
  openIncidents: [5, 20, 50],
  criticalVulnerabilities: [1, 5, 20],
  vulnerabilitySeverity: [3, 4, 5],
} as const satisfies Record<string, Thresholds>;

const PRIORITY_BAND: Record<string, Band> = {
  "1": "critical",
  "2": "at-risk",
  "3": "watch",
  "4": "healthy",
  "5": "healthy",
};

/** ServiceNow priority ("1 - Critical", "2", ...) on the same scale. */
export function priorityBand(priority: string | null | undefined): Band {
  const digit = priority?.trim().charAt(0) ?? "";
  return PRIORITY_BAND[digit] ?? "unknown";
}
