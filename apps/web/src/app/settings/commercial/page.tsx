import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { CommercialSetup } from "@/screens/CommercialSetup";

export const metadata: Metadata = { title: "Commercial setup" };

export default function Page() {
  return (
    <Require permission="commercial.read" staff>
      <CommercialSetup />
    </Require>
  );
}
