import type { Metadata } from "next";
import { Suspense } from "react";

import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { TENANT_ADMIN } from "@/lib/nav";
import { TenantUsers } from "@/screens/TenantUsers";

export const metadata: Metadata = { title: "Users" };

export default function Page() {
  return (
    <Require permission={TENANT_ADMIN}>
      <Suspense fallback={<Loading what="your organisation’s users" />}>
        <TenantUsers />
      </Suspense>
    </Require>
  );
}
