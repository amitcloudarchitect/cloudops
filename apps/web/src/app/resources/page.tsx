import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { ResourceList } from "@/screens/ResourceList";

export const metadata: Metadata = { title: "Resources" };

export default function Page() {
  return (
    <Require permission="inventory.read">
      <ResourceList />
    </Require>
  );
}
