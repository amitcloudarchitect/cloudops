import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { Approvals } from "@/screens/Approvals";

export const metadata: Metadata = { title: "Approvals" };

export default function Page() {
  return (
    <Require permission="approvals.read">
      <Suspense fallback={<Loading what="changes awaiting approval" />}>
        <Approvals />
      </Suspense>
    </Require>
  );
}
