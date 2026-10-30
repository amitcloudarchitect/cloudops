import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { ResourceContextView } from "@/screens/ResourceContextView";

export const metadata: Metadata = { title: "Resource" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Require permission="inventory.read">
      <ResourceContextView id={id} />
    </Require>
  );
}
