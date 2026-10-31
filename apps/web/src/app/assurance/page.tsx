import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { AssuranceList } from "@/screens/AssuranceList";

export const metadata: Metadata = { title: "Assurance" };

export default function Page() {
  return (
    <Require permission="assurance.read">
      <AssuranceList />
    </Require>
  );
}
