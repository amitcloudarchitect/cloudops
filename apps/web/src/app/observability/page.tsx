import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { Observability } from "@/screens/Observability";

export const metadata: Metadata = { title: "Observability" };

export default function Page() {
  return (
    <Require permission="platform.observe" staff>
      <Observability />
    </Require>
  );
}
