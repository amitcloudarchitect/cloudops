import type { Metadata, Viewport } from "next";
import { connection } from "next/server";
import type { ReactNode } from "react";

import { AppShell } from "@/components/AppShell";
import { Providers } from "@/components/Providers";
import { CONFIG_ELEMENT_ID, runtimeFromEnv, serialise } from "@/lib/config";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "CloudOps Operations", template: "%s · CloudOps Operations" },
  description: "Getronics CloudOps operations console",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

// A server component: layout only. Everything that needs the user's token runs
// in the browser (ADR 0014). Rendered per request (ADR 0017): the CSP nonce is new
// each time, and the environment's identifiers come from the pod, not the build.
export default async function RootLayout({ children }: { children: ReactNode }) {
  await connection();
  const runtime = serialise(runtimeFromEnv());
  return (
    <html lang="en-GB" suppressHydrationWarning>
      <body className="bg-canvas text-ink antialiased">
        {/* Data, never run: a JSON script is not executable, so the CSP has no say. */}
        <script id={CONFIG_ELEMENT_ID} type="application/json" dangerouslySetInnerHTML={{ __html: runtime }} />
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
