import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { MarginCustomer } from "@/screens/MarginCustomer";

export const metadata: Metadata = { title: "Customer margin" };

export default async function Page({ params }: { params: Promise<{ tenantId: string }> }) {
  const { tenantId } = await params;
  return (
    <Require permission="commercial.read" staff>
      <Suspense fallback={<Loading what="the customer’s margin" />}>
        <MarginCustomer tenantId={tenantId} />
      </Suspense>
    </Require>
  );
}
