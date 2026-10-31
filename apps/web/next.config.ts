import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

// The console is a browser application: it signs in with MSAL and calls the API
// with the user's token (ADR 0014). Server components render layout only; no
// server code here holds a token or reaches the API.
//
// Deployed as a container (ADR 0017): `standalone` traces what the server needs,
// from the repository root because the workspace hoists node_modules there.
const config: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: fileURLToPath(new URL("../..", import.meta.url)),
  reactStrictMode: true,
  poweredByHeader: false,
  transpilePackages: ["@cloudops/contracts"],
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
          { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
          { key: "Strict-Transport-Security", value: "max-age=31536000" },
          // The Content-Security-Policy is per request, with a nonce (src/proxy.ts).
        ],
      },
    ];
  },
};

export default config;
