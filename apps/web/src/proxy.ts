import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { runtimeFromEnv } from "@/lib/config";
import { contentSecurityPolicy } from "@/lib/csp";

// Every page gets a fresh nonce and the CSP built around it (ADR 0017). Next.js
// reads the nonce from the request's CSP header and applies it to its own scripts,
// which is why pages render per request (src/app/layout.tsx).
export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.getRandomValues(new Uint8Array(16))).toString("base64");
  const csp = contentSecurityPolicy(
    nonce,
    runtimeFromEnv().apiBase,
    process.env.NODE_ENV === "development",
  );
  const headers = new Headers(request.headers);
  headers.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    // Not the hashed static assets (served with no script context) or the probe.
    {
      source: "/((?!_next/static|_next/image|favicon.ico|healthz).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
