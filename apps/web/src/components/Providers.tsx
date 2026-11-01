"use client";

import type { ReactNode } from "react";

import { AssistantProvider } from "@/lib/assistant";
import { AuthProvider } from "@/lib/auth";
import { CustomerProvider } from "@/lib/customer";
import { SessionProvider } from "@/lib/session";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <SessionProvider>
        <CustomerProvider>
          <AssistantProvider>{children}</AssistantProvider>
        </CustomerProvider>
      </SessionProvider>
    </AuthProvider>
  );
}
