import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { ReportsList } from "@/screens/ReportsList";

export const metadata: Metadata = { title: "Reports" };

export default function Page() {
  return (
    <Require permission="reports.read">
      <ReportsList />
    </Require>
  );
}
