import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { FinOpsCustomer } from "@/screens/FinOpsCustomer";

export const metadata: Metadata = { title: "Customer cost" };

export default async function Page({ params }: { params: Promise<{ tenantId: string }> }) {
  const { tenantId } = await params;
  return (
    <Require permission="cost.read" staff>
      <Suspense fallback={<Loading what="the customer’s cost" />}>
        <FinOpsCustomer tenantId={tenantId} />
      </Suspense>
    </Require>
  );
}
