import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { WorkQueue } from "@/screens/WorkQueue";

export const metadata: Metadata = { title: "Work queue" };

export default function Page() {
  return (
    <Require permission="tickets.read">
      <Suspense fallback={<Loading what="the work queue" />}>
        <WorkQueue />
      </Suspense>
    </Require>
  );
}
