// The seeded principals the journeys sign in as (packages/devdata seed.py).
export const ROLES = {
  engineer: "engineer-nwl-crl",
  customer: "customer-nwl",
  finops: "finops-all",
  admin: "platform-admin",
  customerMaster: "customer-master",
  // Step 18: one Northwind user per default customer role (seed.py, PORTAL_TENANTS).
  // The CXO is also Northwind's first administrator.
  cxo: "customer-nwl-admin",
  vp: "customer-nwl-vp",
  architect: "customer-nwl-architect",
  customerEngineer: "customer-nwl-engineer",
  security: "customer-nwl-security",
  approver: "customer-nwl-approver",
} as const;

export type RoleName = keyof typeof ROLES;

export const TOKEN_DIR = new URL("./.tokens/", import.meta.url);
