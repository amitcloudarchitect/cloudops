import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { Revenue } from "@/screens/Revenue";

export const metadata: Metadata = { title: "Revenue & margin" };

export default function Page() {
  return (
    <Require permission="commercial.read" staff>
      <Suspense fallback={<Loading what="revenue and margin" />}>
        <Revenue />
      </Suspense>
    </Require>
  );
}
