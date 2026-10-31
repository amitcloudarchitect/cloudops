// The console's modules and the permission each needs (HLD, role-driven
// navigation). A role never sees a link to a module it cannot use: the shell
// filters this list by the permissions /v1/me returns, and each screen checks
// again, because hiding a link is not access control (the API is).

/** Not a matrix permission: held by a customer administrator of its own tenant (/v1/me). */
export const TENANT_ADMIN = "tenant_admin";

export interface Module {
  href: string;
  label: string;
  permission: string;
  /** Staff only: a customer never sees it, even holding the permission. */
  staff?: boolean;
  /** Not built yet: listed so its place is decided, never shown. */
  reserved?: boolean;
}

export const MODULES: readonly Module[] = [
  { href: "/work", label: "Work queue", permission: "tickets.read" },
  // Step 18: the customer roles' landing views (ADR 0018); staff with the permission see them too.
  { href: "/performance", label: "Performance", permission: "tickets.read" },
  { href: "/posture", label: "Security posture", permission: "vulnerabilities.read" },
  { href: "/approvals", label: "Approvals", permission: "approvals.read" },
  { href: "/resources", label: "Resources", permission: "inventory.read" },
  { href: "/customers", label: "Customers", permission: "authenticated" },
  // Step 18: a customer administrator's own users (ADR 0018, D2).
  { href: "/users", label: "Users", permission: TENANT_ADMIN },
  // Step 14b: onboarding customers and mapping source company names to them (HLD M1).
  { href: "/mapping", label: "Customer mapping", permission: "mapping.manage" },
  // Step 18: onboarding a customer to the portal (HLD M2), platform administrators only.
  { href: "/onboarding", label: "Portal onboarding", permission: "access.manage" },
  // Step 15: ingest health per source and entity, and schema drift (staff only).
  { href: "/connectors", label: "Connectors", permission: "connectors.read" },
  // Step 16: cost reconciliation and allocation, margin, and the pricing engine.
  // Customer viewers hold cost.read for their own cost; the FinOps module is staff work.
  { href: "/finops", label: "FinOps", permission: "cost.read", staff: true },
  { href: "/margin", label: "Margin", permission: "commercial.read", staff: true },
  { href: "/pricing", label: "Pricing", permission: "commercial.read", staff: true },
];

export function visibleModules(has: (permission: string) => boolean, staff: boolean): Module[] {
  return MODULES.filter((m) => !m.reserved && (staff || !m.staff) && has(m.permission));
}

/** Where the role opens: its landing view when it can use it, else its first module. */
export function landing(modules: Module[], preferred: string | null | undefined): Module | undefined {
  return modules.find((m) => m.href === preferred) ?? modules[0];
}
