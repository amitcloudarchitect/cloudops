import type { Metadata } from "next";

import { EmptyState } from "@/components/EmptyState";
import { Require } from "@/components/Require";
import { isUuid } from "@/lib/ids";
import { SecOpsFindingView } from "@/screens/SecOpsFinding";

export const metadata: Metadata = { title: "Finding" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Require permission="vulnerabilities.read" staff>
      {/* A finding id is a UUID: anything else is not looked up, or put in a URL. */}
      {isUuid(id) ? (
        <SecOpsFindingView id={id} />
      ) : (
        <EmptyState title="Finding not found" next="Open a finding from the SecOps workspace." action={{ label: "Back to SecOps", href: "/secops" }} />
      )}
    </Require>
  );
}
