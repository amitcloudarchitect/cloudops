import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { Knowledge } from "@/screens/Knowledge";

export const metadata: Metadata = { title: "Knowledge" };

export default function Page() {
  return (
    <Require permission="knowledge.read">
      <Knowledge />
    </Require>
  );
}
