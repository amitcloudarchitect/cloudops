// The seeded principals the journeys sign in as (packages/devdata seed.py).
export const ROLES = {
  engineer: "engineer-nwl-crl",
  customer: "customer-nwl",
  finops: "finops-all",
  admin: "platform-admin",
} as const;

export type RoleName = keyof typeof ROLES;

export const TOKEN_DIR = new URL("./.tokens/", import.meta.url);
