// Configuration (ADR 0014, ADR 0017). Identifiers only, never secrets.
//
// The auth mode is fixed when the bundle is built (NEXT_PUBLIC_AUTH_MODE), so an
// entra build carries no local sign-in path. Everything that differs between
// environments is read by the server at request time from the pod's environment and
// handed to the browser in the page (CONFIG_ELEMENT_ID): one image, promoted as built.

export type AuthMode = "entra" | "local";

export interface RuntimeConfig {
  apiBase: string;
  entra: { clientId: string; tenantId: string; apiScope: string };
  /** The platform's own monitoring (Terraform stack 03-observability). Empty until
   * the stack is applied and its outputs are set in the overlay: "not yet applied". */
  observability?: { grafanaUrl: string; appInsightsUrl: string };
}

export interface ConsoleConfig extends RuntimeConfig {
  authMode: AuthMode;
}

export const CONFIG_ELEMENT_ID = "cloudops-config";

export function authMode(): AuthMode {
  const value = process.env.NEXT_PUBLIC_AUTH_MODE;
  if (value === "entra" || value === "local") return value;
  // No silent default: a build that forgot to choose fails at first use.
  throw new Error("NEXT_PUBLIC_AUTH_MODE must be 'entra' or 'local'");
}

function validated(raw: RuntimeConfig, mode: AuthMode): RuntimeConfig {
  const apiBase = (raw.apiBase ?? "").replace(/\/+$/, "");
  if (!apiBase) throw new Error("the API base is not set");
  let url: URL;
  try {
    url = new URL(apiBase);
  } catch {
    throw new Error("the API base is not a URL");
  }
  if (url.origin !== apiBase) throw new Error("the API base must be an origin, without a path");
  // Only local mode talks to a plain-http API (make api on this machine).
  if (url.protocol !== "https:" && mode !== "local") throw new Error("the API base must be https");
  const { clientId = "", tenantId = "", apiScope = "" } = raw.entra ?? {};
  if (mode === "entra" && !(clientId && tenantId && apiScope)) {
    throw new Error("entra mode needs the client id, tenant id and API scope");
  }
  const { grafanaUrl = "", appInsightsUrl = "" } = raw.observability ?? {};
  for (const link of [grafanaUrl, appInsightsUrl]) {
    if (link && !/^https:\/\/[^\s"'<>]+$/.test(link)) throw new Error("a monitoring link must be an https URL");
  }
  return { apiBase, entra: { clientId, tenantId, apiScope }, observability: { grafanaUrl, appInsightsUrl } };
}

/** Server only: the runtime values, from the process environment. */
export function runtimeFromEnv(env: Record<string, string | undefined> = process.env): RuntimeConfig {
  return validated(
    {
      apiBase: env.CLOUDOPS_WEB_API_BASE ?? "",
      entra: {
        clientId: env.CLOUDOPS_WEB_ENTRA_CLIENT_ID ?? "",
        tenantId: env.CLOUDOPS_WEB_ENTRA_TENANT_ID ?? "",
        apiScope: env.CLOUDOPS_WEB_API_SCOPE ?? "",
      },
      observability: {
        grafanaUrl: env.CLOUDOPS_WEB_GRAFANA_URL ?? "",
        appInsightsUrl: env.CLOUDOPS_WEB_APP_INSIGHTS_URL ?? "",
      },
    },
    authMode(),
  );
}

/** For the page: JSON that cannot close the script element it sits in. */
export function serialise(runtime: RuntimeConfig): string {
  return JSON.stringify(runtime).replace(/</g, "\\u003c");
}

let cached: ConsoleConfig | null = null;

/** Browser only: the values the server put in the page. */
export function config(): ConsoleConfig {
  if (cached) return cached;
  const text = document.getElementById(CONFIG_ELEMENT_ID)?.textContent;
  if (!text) throw new Error("the page carries no console configuration");
  const mode = authMode();
  cached = { authMode: mode, ...validated(JSON.parse(text) as RuntimeConfig, mode) };
  return cached;
}
