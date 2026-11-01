import type { Metadata } from "next";

import { Require } from "@/components/Require";
import { ManageCustomers } from "@/screens/ManageCustomers";

export const metadata: Metadata = { title: "Manage customers" };

export default function Page() {
  return (
    <Require permission="mapping.manage" staff>
      <ManageCustomers />
    </Require>
  );
}
