import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { CloudServicesOverview } from "@/screens/CloudServices";

export const metadata: Metadata = { title: "Cloud Services" };

// Staff, and the customer roles holding cloud_services.read (ADR 0038).
export default function Page() {
  return (
    <Require permission="cloud_services.read">
      <Suspense fallback={<Loading what="cloud services" />}>
        <CloudServicesOverview />
      </Suspense>
    </Require>
  );
}
