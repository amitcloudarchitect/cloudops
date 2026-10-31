import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { PortalOnboarding } from "@/screens/PortalOnboarding";

export const metadata: Metadata = { title: "Portal onboarding" };

export default function Page() {
  return (
    <Require permission="access.manage">
      <Suspense fallback={<Loading what="portal onboarding" />}>
        <PortalOnboarding />
      </Suspense>
    </Require>
  );
}
