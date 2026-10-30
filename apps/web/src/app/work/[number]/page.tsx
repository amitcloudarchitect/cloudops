import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { TicketDetail } from "@/screens/TicketDetail";

export const metadata: Metadata = { title: "Ticket" };

export default async function Page({ params }: { params: Promise<{ number: string }> }) {
  const { number } = await params;
  return (
    <Require permission="tickets.read">
      <TicketDetail number={decodeURIComponent(number)} />
    </Require>
  );
}
