import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { AssessmentDetail } from "@/screens/AssessmentDetail";

export const metadata: Metadata = { title: "Assessment" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <Require permission="assurance.read">
      <AssessmentDetail id={decodeURIComponent(id)} />
    </Require>
  );
}
