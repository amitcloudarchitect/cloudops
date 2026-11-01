"use client";

import type { ReactNode } from "react";

import { AssistantProvider } from "@/lib/assistant";
import { AuthProvider } from "@/lib/auth";
import { SessionProvider } from "@/lib/session";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <SessionProvider>
        <AssistantProvider>{children}</AssistantProvider>
      </SessionProvider>
    </AuthProvider>
  );
}
