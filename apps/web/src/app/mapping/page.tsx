import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { CustomerMapping } from "@/screens/CustomerMapping";

export const metadata: Metadata = { title: "Customer mapping" };

export default function Page() {
  return (
    <Require permission="mapping.manage">
      <CustomerMapping />
    </Require>
  );
}
