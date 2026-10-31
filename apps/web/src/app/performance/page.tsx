import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { Performance } from "@/screens/Performance";

export const metadata: Metadata = { title: "Performance" };

export default function Page() {
  return (
    <Require permission="tickets.read">
      <Suspense fallback={<Loading what="your service performance" />}>
        <Performance />
      </Suspense>
    </Require>
  );
}
