import type { Metadata } from "next";
import { Suspense } from "react";

import { EmptyState } from "@/components/EmptyState";
import { Loading } from "@/components/Loading";
import { Require } from "@/components/Require";
import { isUuid } from "@/lib/ids";
import { CloudAccountView } from "@/screens/CloudServices";

export const metadata: Metadata = { title: "Cloud account" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Require permission="cloud_services.read">
      {/* An id is a UUID: anything else is not looked up, or put in a URL. */}
      {isUuid(id) ? (
        <Suspense fallback={<Loading what="the account" />}>
          <CloudAccountView id={id} />
        </Suspense>
      ) : (
        <EmptyState title="Not found" next="Open it from Cloud Services." action={{ label: "Back to Cloud Services", href: "/cloud-services" }} />
      )}
    </Require>
  );
}
