// Build-time configuration. NEXT_PUBLIC_* values are inlined into the bundle, so
// they are identifiers only, never secrets.

export type AuthMode = "entra" | "local";

export interface ConsoleConfig {
  authMode: AuthMode;
  apiBase: string;
  entra: { clientId: string; tenantId: string; apiScope: string };
}

function mode(value: string | undefined): AuthMode {
  if (value === "entra" || value === "local") return value;
  // No silent default: a build that forgot to choose fails at first use.
  throw new Error("NEXT_PUBLIC_AUTH_MODE must be 'entra' or 'local'");
}

export function config(): ConsoleConfig {
  const authMode = mode(process.env.NEXT_PUBLIC_AUTH_MODE);
  const apiBase = (process.env.NEXT_PUBLIC_API_BASE ?? "").replace(/\/+$/, "");
  if (!apiBase) throw new Error("NEXT_PUBLIC_API_BASE is not set");
  const entra = {
    clientId: process.env.NEXT_PUBLIC_ENTRA_CLIENT_ID ?? "",
    tenantId: process.env.NEXT_PUBLIC_ENTRA_TENANT_ID ?? "",
    apiScope: process.env.NEXT_PUBLIC_API_SCOPE ?? "",
  };
  if (authMode === "entra" && !(entra.clientId && entra.tenantId && entra.apiScope)) {
    throw new Error("entra mode needs the client id, tenant id and API scope");
  }
  return { authMode, apiBase, entra };
}
