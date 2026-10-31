// The console's modules and the permission each needs (HLD, role-driven
// navigation). A role never sees a link to a module it cannot use: the shell
// filters this list by the permissions /v1/me returns, and each screen checks
// again, because hiding a link is not access control (the API is).

export interface Module {
  href: string;
  label: string;
  permission: string;
  /** Not built yet: listed so its place is decided, never shown. */
  reserved?: boolean;
}

export const MODULES: readonly Module[] = [
  { href: "/work", label: "Work queue", permission: "tickets.read" },
  { href: "/resources", label: "Resources", permission: "inventory.read" },
  { href: "/customers", label: "Customers", permission: "authenticated" },
  // Step 14b: onboarding customers and mapping source company names to them (HLD M1).
  { href: "/mapping", label: "Customer mapping", permission: "mapping.manage" },
  // Step 15: ingest health per source and entity, and schema drift (staff only).
  { href: "/connectors", label: "Connectors", permission: "connectors.read" },
];

export function visibleModules(has: (permission: string) => boolean): Module[] {
  return MODULES.filter((m) => !m.reserved && has(m.permission));
}
