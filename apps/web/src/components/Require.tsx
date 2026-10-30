"use client";

import type { ReactNode } from "react";

import { can, useMe } from "@/lib/session";

import { EmptyState } from "./EmptyState";

// A screen checks its permission itself: a typed-in URL reaches the screen even
// without a link. The API refuses regardless; this keeps the page honest.
export function Require({ permission, children }: { permission: string; children: ReactNode }) {
  const me = useMe();
  if (!can(me, permission)) {
    return (
      <EmptyState
        title="Not available to your role"
        next="Choose a module from the navigation, or ask a platform administrator if your role should include this."
      />
    );
  }
  return <>{children}</>;
}
