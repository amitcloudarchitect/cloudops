import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { ConnectorHealth } from "@/screens/ConnectorHealth";

export const metadata: Metadata = { title: "Connectors" };

export default function Page() {
  return (
    <Require permission="connectors.read">
      <ConnectorHealth />
    </Require>
  );
}
