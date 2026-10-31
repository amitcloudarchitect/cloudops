import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { Pricing } from "@/screens/Pricing";

export const metadata: Metadata = { title: "Pricing" };

export default function Page() {
  return (
    <Require permission="commercial.read" staff>
      <Pricing />
    </Require>
  );
}
