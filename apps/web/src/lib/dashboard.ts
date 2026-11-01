// The home dashboards (ADR 0030): which widgets exist, the permission each needs,
// and each role class's default layout. A widget is shown only when the role holds
// its permission (and, for staff work, is staff); the API checks again on every
// call, because hiding a widget is not access control.
//
// A user may hide and reorder widgets. The layout is kept in this browser, per
// role, as the tables' saved views are (ADR 0014, decision 6).

export type WidgetId =
  | "estate"
  | "incidents"
  | "closest"
  | "triage"
  | "changes"
  | "noisy"
  | "sla_trend"
  | "backlog_customer"
  | "backlog_workgroup"
  | "connectors"
  | "csat"
  | "cost_trend"
  | "anomalies"
  | "reconciliation"
  | "margin"
  | "platform_cost"
  | "ai_usage"
  | "mapping"
  | "onboarding"
  | "price"
  | "posture"
  | "availability"
  | "report"
  | "approvals";

export interface WidgetSpec {
  title: string;
  permission: string;
  /** Staff work: never shown to a customer, whatever it holds. */
  staff?: boolean;
  /** Customer only: a staff role reads the staff view of the same figures. */
  customer?: boolean;
  /** Takes two columns on wide screens. */
  wide?: boolean;
}

export const WIDGETS: Record<WidgetId, WidgetSpec> = {
  estate: { title: "Your service", permission: "authenticated", customer: true, wide: true },
  incidents: { title: "Open incidents and SLA risk", permission: "tickets.read", wide: true },
  closest: { title: "Closest to breach", permission: "tickets.read" },
  triage: { title: "Triage awaiting review", permission: "triage.review", staff: true },
  changes: { title: "Changes this week", permission: "tickets.read" },
  noisy: { title: "Noisy configuration items", permission: "tickets.read" },
  sla_trend: { title: "SLA attainment", permission: "tickets.read" },
  backlog_customer: { title: "Backlog and MTTR by customer", permission: "tickets.read", staff: true, wide: true },
  backlog_workgroup: { title: "Backlog and MTTR by workgroup", permission: "tickets.read", staff: true },
  connectors: { title: "Connector health", permission: "connectors.read", staff: true, wide: true },
  csat: { title: "Customer satisfaction", permission: "tickets.read", staff: true },
  cost_trend: { title: "Cloud cost by customer", permission: "cost.read", staff: true, wide: true },
  anomalies: { title: "Cost anomalies", permission: "cost.read", staff: true },
  reconciliation: { title: "Cost reconciliation", permission: "cost.read", staff: true, wide: true },
  margin: { title: "Margin by customer", permission: "commercial.read", staff: true, wide: true },
  platform_cost: { title: "Platform cost", permission: "cost.read", staff: true },
  ai_usage: { title: "AI gateway usage", permission: "cost.read", staff: true },
  mapping: { title: "Alias and mapping queue", permission: "mapping.manage", staff: true },
  onboarding: { title: "Portal onboarding", permission: "access.manage", staff: true },
  price: { title: "Cloud consumption", permission: "cost.read", customer: true },
  posture: { title: "Vulnerabilities", permission: "vulnerabilities.read" },
  availability: { title: "Availability", permission: "inventory.read" },
  report: { title: "Latest report pack", permission: "reports.read" },
  approvals: { title: "Changes awaiting approval", permission: "approvals.read" },
};

// Each role class's default layout, most important first (HLD, role-driven navigation).
export const PRESETS: Record<string, readonly WidgetId[]> = {
  engineer: ["incidents", "closest", "triage", "changes", "noisy", "posture", "connectors"],
  secops: ["posture", "incidents", "closest", "changes", "noisy"],
  delivery_lead: [
    "sla_trend", "incidents", "backlog_customer", "backlog_workgroup", "changes", "connectors", "csat",
    "margin", "mapping",
  ],
  finops: ["cost_trend", "anomalies", "reconciliation", "platform_cost", "margin", "ai_usage"],
  commercial: ["margin", "cost_trend", "reconciliation", "anomalies", "report"],
  cfo: ["margin", "cost_trend", "reconciliation", "anomalies", "platform_cost", "report"],
  ceo: ["sla_trend", "incidents", "margin", "cost_trend", "backlog_customer", "posture", "report"],
  auditor: ["sla_trend", "posture", "reconciliation", "margin", "report"],
  platform_admin: [
    "connectors", "mapping", "onboarding", "ai_usage", "platform_cost", "triage", "incidents", "changes",
  ],
  customer: ["estate", "incidents", "closest", "sla_trend", "price", "posture", "availability", "report", "approvals"],
};

// A customer role's landing view (ADR 0018, D4) becomes its dashboard's lead widget.
const LEAD: Record<string, WidgetId> = {
  "/customers": "estate",
  "/performance": "sla_trend",
  "/resources": "estate",
  "/work": "incidents",
  "/posture": "posture",
  "/approvals": "approvals",
};

export interface Role {
  kind: string;
  roleClass: string;
  landing: string | null | undefined;
  can: (permission: string) => boolean;
}

export function allowed(id: WidgetId, role: Role): boolean {
  const w = WIDGETS[id];
  const staff = role.kind === "staff";
  if (w.staff && !staff) return false;
  if (w.customer && staff) return false;
  return w.permission === "authenticated" || role.can(w.permission);
}

/** The role's default layout: its preset, lead widget first, filtered to what it may see. */
export function defaultLayout(role: Role): WidgetId[] {
  const preset = [...(PRESETS[role.roleClass] ?? PRESETS[role.kind === "customer" ? "customer" : "engineer"] ?? [])];
  const lead = role.landing ? LEAD[role.landing] : undefined;
  const ordered = lead ? [lead, ...preset.filter((id) => id !== lead)] : preset;
  return ordered.filter((id) => allowed(id, role));
}

export interface Layout {
  order: WidgetId[];
  hidden: WidgetId[];
}

function isWidget(value: unknown): value is WidgetId {
  return typeof value === "string" && Object.hasOwn(WIDGETS, value);
}

function storageKey(role: string): string {
  return `cloudops.home.${role}`;
}

/** The saved layout, reconciled with what the role may see now: a widget the role
 * lost is dropped, one it gained is appended. Storage may be unavailable. */
export function loadLayout(roleName: string, role: Role): Layout {
  const fallback = { order: defaultLayout(role), hidden: [] };
  try {
    const raw = window.localStorage.getItem(storageKey(roleName));
    if (!raw) return fallback;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return fallback;
    const { order, hidden } = parsed as { order?: unknown; hidden?: unknown };
    const keep = (list: unknown): WidgetId[] =>
      Array.isArray(list) ? list.filter(isWidget).filter((id) => allowed(id, role)) : [];
    const saved = keep(order);
    const missing = fallback.order.filter((id) => !saved.includes(id));
    return { order: [...saved, ...missing], hidden: keep(hidden) };
  } catch {
    return fallback;
  }
}

export function saveLayout(roleName: string, layout: Layout | null): void {
  try {
    if (layout) window.localStorage.setItem(storageKey(roleName), JSON.stringify(layout));
    else window.localStorage.removeItem(storageKey(roleName));
  } catch {
    // A private window or blocked storage: the layout lasts this visit only.
  }
}

/** Move `id` to just before (by -1) or after (by 1) `beside`: one press moves a widget
 * past its visible neighbour, whatever hidden widgets lie between them. */
export function place(order: readonly WidgetId[], id: WidgetId, beside: WidgetId, by: -1 | 1): WidgetId[] {
  const rest = order.filter((x) => x !== id);
  const at = rest.indexOf(beside);
  if (at < 0 || !order.includes(id)) return [...order];
  rest.splice(by < 0 ? at : at + 1, 0, id);
  return rest;
}
