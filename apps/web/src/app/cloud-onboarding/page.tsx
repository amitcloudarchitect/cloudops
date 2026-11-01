import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { CloudOnboarding } from "@/screens/CloudOnboarding";

export const metadata: Metadata = { title: "Cloud onboarding" };

export default function Page() {
  return (
    <Require permission="access.manage">
      <Suspense fallback={<Loading what="cloud onboarding" />}>
        <CloudOnboarding />
      </Suspense>
    </Require>
  );
}
