import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { Posture } from "@/screens/Posture";

export const metadata: Metadata = { title: "Security posture" };

export default function Page() {
  return (
    <Require permission="vulnerabilities.read">
      <Suspense fallback={<Loading what="your security posture" />}>
        <Posture />
      </Suspense>
    </Require>
  );
}
