// The console's modules and the permission each needs (HLD, role-driven
// navigation). A role never sees a link to a module it cannot use: the shell
// filters this list by the permissions /v1/me returns, and each screen checks
// again, because hiding a link is not access control (the API is).

/** Not a matrix permission: held by a customer administrator of its own tenant (/v1/me). */
export const TENANT_ADMIN = "tenant_admin";

/** The navigation's areas, in order. An area with no module the role can use is not shown. */
export const AREAS = ["Operations", "Security", "Commercial", "Assurance & Reports", "Knowledge", "Settings"] as const;
export type Area = (typeof AREAS)[number];

export interface Module {
  href: string;
  label: string;
  area: Area;
  permission: string;
  /** Staff only: a customer never sees it, even holding the permission. */
  staff?: boolean;
  /** Not built yet: listed so its place is decided, never shown. */
  reserved?: boolean;
}

export const MODULES: readonly Module[] = [
  // The role's dashboard (ADR 0030): where every role lands.
  { href: "/", label: "Home", area: "Operations", permission: "authenticated" },
  { href: "/work", label: "Work queue", area: "Operations", permission: "tickets.read" },
  { href: "/resources", label: "Resources", area: "Operations", permission: "inventory.read" },
  { href: "/approvals", label: "Approvals", area: "Operations", permission: "approvals.read" },
  // Step 18: the customer roles' landing views (ADR 0018); staff with the permission see them too.
  { href: "/performance", label: "Performance", area: "Operations", permission: "tickets.read" },
  { href: "/customers", label: "Customers", area: "Operations", permission: "authenticated" },
  // The platform's own usage and health (ADR 0034).
  { href: "/observability", label: "Observability", area: "Operations", permission: "platform.observe", staff: true },
  { href: "/posture", label: "Security posture", area: "Security", permission: "vulnerabilities.read" },
  // The SecOps workspace: built separately; shown once its page exists.
  { href: "/secops", label: "SecOps", area: "Security", permission: "vulnerabilities.read", staff: true, reserved: true },
  // Step 16: cost reconciliation and allocation, margin, and the pricing engine.
  // Customer viewers hold cost.read for their own cost; the FinOps module is staff work.
  { href: "/finops", label: "FinOps", area: "Commercial", permission: "cost.read", staff: true },
  { href: "/margin", label: "Margin", area: "Commercial", permission: "commercial.read", staff: true },
  { href: "/pricing", label: "Pricing", area: "Commercial", permission: "commercial.read", staff: true },
  // Step 17: assessments and monthly report packs.
  { href: "/assurance", label: "Assurance", area: "Assurance & Reports", permission: "assurance.read", staff: true },
  { href: "/reports", label: "Reports", area: "Assurance & Reports", permission: "reports.read" },
  // Step 17: document staleness and gaps.
  { href: "/knowledge", label: "Knowledge", area: "Knowledge", permission: "knowledge.read", staff: true },
  // Settings: who may use the platform and what it is connected to. Each entry keeps
  // its own permission, so the area shows only to those who hold one of them.
  // Step 18: a customer administrator's own users (ADR 0018, D2).
  { href: "/users", label: "Users", area: "Settings", permission: TENANT_ADMIN },
  // ADR 0027: a customer's cloud estate, read-only, from a stored credential (ADR 0003).
  { href: "/cloud-onboarding", label: "Cloud onboarding", area: "Settings", permission: "access.manage", staff: true },
  // Step 18: onboarding a customer to the portal (HLD M2), and its AMARA switch (ADR 0029).
  { href: "/onboarding", label: "Portal onboarding", area: "Settings", permission: "access.manage" },
  // Step 14b: onboarding tenants, mapping source company names to them (HLD M1), and
  // which tenant hosts the platform (ADR 0031).
  { href: "/mapping", label: "Customer mapping", area: "Settings", permission: "mapping.manage" },
  // Step 15: ingest health per source and entity, and schema drift (staff only).
  { href: "/connectors", label: "Connectors", area: "Settings", permission: "connectors.read" },
  // SOWs and the rate card (ADR 0033, ADR 0034): commercial roles read it; recording
  // needs commercial.write and publishing ratecard.publish, checked on the page and by the API.
  { href: "/settings/commercial", label: "Commercial setup", area: "Settings", permission: "commercial.read", staff: true },
];

export function visibleModules(has: (permission: string) => boolean, staff: boolean): Module[] {
  return MODULES.filter((m) => !m.reserved && (staff || !m.staff) && has(m.permission));
}

/** The visible modules grouped by area, in AREAS order, empty areas left out. */
export function visibleAreas(has: (permission: string) => boolean, staff: boolean): { area: Area; modules: Module[] }[] {
  const modules = visibleModules(has, staff);
  return AREAS.map((area) => ({ area, modules: modules.filter((m) => m.area === area) })).filter((g) => g.modules.length > 0);
}
