// The home dashboard's sections (ADR 0038, amending ADR 0030): which exist, the
// permission each needs, and whether it is staff work. A section is shown only when
// the role holds its permission (and, for staff work, is staff); the API checks
// again on every call, because hiding a section is not access control.

export type SectionId = "operations" | "revenue" | "security" | "platform" | "top";

export interface SectionSpec {
  title: string;
  permission: string;
  /** Staff work: never shown to a customer, whatever it holds. */
  staff?: boolean;
}

export const SECTIONS: Record<SectionId, SectionSpec> = {
  operations: { title: "Operations", permission: "tickets.read" },
  // A customer sees the price of its own consumption here (ADR 0021), never cost or margin.
  revenue: { title: "Revenue & margin", permission: "cost.read" },
  security: { title: "Security & compliance", permission: "vulnerabilities.read" },
  platform: { title: "Platform", permission: "inventory.read", staff: true },
  top: { title: "Top 10 customers", permission: "tickets.read", staff: true },
};

export const ORDER: readonly SectionId[] = ["operations", "revenue", "security", "platform", "top"];

// A customer role's landing view (ADR 0018, D4) puts its section first.
const LEAD: Record<string, SectionId> = {
  "/performance": "operations",
  "/work": "operations",
  "/posture": "security",
};

export interface Role {
  kind: string;
  landing: string | null | undefined;
  can: (permission: string) => boolean;
}

export function allowed(id: SectionId, role: Role): boolean {
  const s = SECTIONS[id];
  if (s.staff && role.kind !== "staff") return false;
  return role.can(s.permission);
}

/** The sections the role may see, its landing view's section first. */
export function sectionsFor(role: Role): SectionId[] {
  const lead = role.landing ? LEAD[role.landing] : undefined;
  const ordered = lead ? [lead, ...ORDER.filter((id) => id !== lead)] : [...ORDER];
  return ordered.filter((id) => allowed(id, role));
}
