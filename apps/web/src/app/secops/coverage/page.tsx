import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { SecOpsCoverage } from "@/screens/SecOps";

export const metadata: Metadata = { title: "Scanner coverage" };

// Staff only (ADR 0036): a customer reads its own posture at /posture.
export default function Page() {
  return (
    <Require permission="vulnerabilities.read" staff>
      <Suspense fallback={<Loading what="scanner coverage" />}>
        <SecOpsCoverage />
      </Suspense>
    </Require>
  );
}
