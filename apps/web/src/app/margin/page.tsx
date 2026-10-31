import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { Margin } from "@/screens/Margin";

export const metadata: Metadata = { title: "Margin" };

export default function Page() {
  return (
    <Require permission="commercial.read" staff>
      <Suspense fallback={<Loading what="margin" />}>
        <Margin />
      </Suspense>
    </Require>
  );
}
