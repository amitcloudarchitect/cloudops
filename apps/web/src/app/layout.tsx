import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { AppShell } from "@/components/AppShell";
import { Providers } from "@/components/Providers";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "CloudOps Operations", template: "%s · CloudOps Operations" },
  description: "Getronics CloudOps operations console",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

// A server component: layout only. Everything that needs the user's token runs
// in the browser (ADR 0014).
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en-GB" suppressHydrationWarning>
      <body className="bg-canvas text-ink antialiased">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
