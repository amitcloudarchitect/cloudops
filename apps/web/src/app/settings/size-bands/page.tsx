import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { SizeBands } from "@/screens/SizeBands";

export const metadata: Metadata = { title: "Size bands" };

export default function Page() {
  return (
    <Require permission="inventory.read" staff>
      <SizeBands />
    </Require>
  );
}
