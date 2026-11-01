import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { CloudFindingsView } from "@/screens/CloudServices";

export const metadata: Metadata = { title: "Cloud findings" };

export default function Page() {
  return (
    <Require permission="cloud_services.read">
      <Suspense fallback={<Loading what="findings" />}>
        <CloudFindingsView />
      </Suspense>
    </Require>
  );
}
