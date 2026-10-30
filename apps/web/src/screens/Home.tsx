"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { EmptyState } from "@/components/EmptyState";
import { visibleModules } from "@/lib/nav";
import { can, useMe } from "@/lib/session";

// Home is the first module the role can use.
export function Home() {
  const me = useMe();
  const router = useRouter();
  const first = visibleModules((p) => can(me, p))[0];
  useEffect(() => {
    if (first) router.replace(first.href);
  }, [first, router]);
  return first ? null : <EmptyState title="No modules for your role" next="Ask a platform administrator which role you need." />;
}
