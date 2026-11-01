import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { SecOpsPostureScore } from "@/screens/SecOps";

export const metadata: Metadata = { title: "Posture score" };

// Staff only (ADR 0036): a customer reads its own posture at /posture.
export default function Page() {
  return (
    <Require permission="vulnerabilities.read" staff>
      <Suspense fallback={<Loading what="posture scores" />}>
        <SecOpsPostureScore />
      </Suspense>
    </Require>
  );
}
