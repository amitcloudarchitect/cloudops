import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { FinOps } from "@/screens/FinOps";

export const metadata: Metadata = { title: "FinOps" };

export default function Page() {
  return (
    <Require permission="cost.read" staff>
      <Suspense fallback={<Loading what="cost" />}>
        <FinOps />
      </Suspense>
    </Require>
  );
}
