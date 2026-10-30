import type { Metadata } from "next";

import { CustomerOverviewView } from "@/screens/CustomerOverviewView";

export const metadata: Metadata = { title: "Customers" };

export default function Page() {
  return <CustomerOverviewView />;
}
