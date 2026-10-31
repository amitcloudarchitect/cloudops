import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { ReportPack } from "@/screens/ReportPack";

export const metadata: Metadata = { title: "Report pack" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Require permission="reports.read">
      <ReportPack id={decodeURIComponent(id)} />
    </Require>
  );
}
