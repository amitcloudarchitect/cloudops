// The console's Content Security Policy (ADR 0017). Built per request by the proxy
// (src/proxy.ts) with a fresh nonce, which Next.js puts on its own scripts and
// styles; nothing else inline runs.
//
// connect-src is the API and Entra's token endpoint only, plus this origin for the
// App Router's own navigation requests. No frames at all: MSAL renews with the
// refresh token over fetch, and falls back to a full-page redirect, never an iframe
// (src/lib/auth.tsx).

export const ENTRA_ORIGIN = "https://login.microsoftonline.com";

export function contentSecurityPolicy(nonce: string, apiBase: string, dev = false): string {
  const api = new URL(apiBase);
  const connect = ["'self'", api.origin, ENTRA_ORIGIN];
  const directives = [
    "default-src 'none'",
    // React's development build reconstructs error stacks with eval; production never does.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src ${[...new Set(connect)].join(" ")}${dev ? " ws:" : ""}`,
    "manifest-src 'self'",
    "frame-src 'none'",
    "worker-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ];
  if (api.protocol === "https:") directives.push("upgrade-insecure-requests");
  return directives.join("; ");
}
