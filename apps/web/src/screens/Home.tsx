"use client";

// Home (ADR 0038, amending ADR 0030): five sections, each over a date range the
// viewer chooses and, where it helps, compared with a second range side by side.
// Every section follows the customer chosen in the top bar: staff read every
// customer or the one chosen; a customer reads its own. A role sees only the
// sections it may, its landing view's section first (ADR 0018, D4).

import Link from "next/link";
import { useMemo } from "react";

import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { WidgetCache } from "@/components/Widget";
import { useAssistantContext } from "@/lib/assistant";
import { useCustomer } from "@/lib/customer";
import { SECTIONS, sectionsFor, type SectionId } from "@/lib/dashboard";
import { MODULES } from "@/lib/nav";
import { can, useMe } from "@/lib/session";

import { OperationsSection } from "./home/Operations";
import { PlatformSection } from "./home/Platform";
import { RevenueSection } from "./home/Revenue";
import { SecuritySection } from "./home/Security";
import { TopCustomersSection } from "./home/TopCustomers";

export function Home() {
  useAssistantContext({ screen: "home" });
  const me = useMe();
  const { tenantId, tenants } = useCustomer();
  const customer = me.kind !== "staff";
  const sections = useMemo(() => sectionsFor({ kind: me.kind, landing: me.landing, can: (p: string) => can(me, p) }), [me]);
  const landingPath = me.landing ? me.landing.split("?")[0] : null;
  const landing = landingPath ? MODULES.find((m) => m.href === landingPath) : undefined;
  const chosen = tenantId ? tenants.find((t) => t.id === tenantId)?.name : null;

  const subtitle = customer
    ? "Your service at a glance: choose a date range for each section, or compare two."
    : `${chosen ? `${chosen} only.` : "Every customer in your scope."} Choose a date range for each section, or compare two; the customer selector above narrows every section.`;

  const render: Record<SectionId, () => React.ReactNode> = {
    operations: () => <OperationsSection />,
    revenue: () => <RevenueSection customer={customer} />,
    security: () => <SecuritySection customer={customer} />,
    platform: () => <PlatformSection />,
    top: () => <TopCustomersSection />,
  };

  return (
    <>
      <PageHeader title="Home" subtitle={subtitle}>
        {landing && landing.href !== "/" ? (
          <Link href={me.landing ?? landing.href} className="btn btn-secondary">
            Go to {landing.label}
          </Link>
        ) : null}
      </PageHeader>
      {sections.length === 0 ? (
        <EmptyState title="No dashboard section for your role" next={landing ? `Your view is ${landing.label}, linked above.` : "Ask a platform administrator which role you need."} />
      ) : (
        <>
          {sections.length > 1 ? (
            <nav aria-label="Home sections" className="mb-4">
              <ul className="flex flex-wrap gap-2">
                {sections.map((id) => (
                  <li key={id}>
                    <a href={`#section-${id}`} className="btn btn-secondary px-3 py-1 text-xs">
                      {customer && id === "revenue" ? "Your cloud consumption" : SECTIONS[id].title}
                    </a>
                  </li>
                ))}
              </ul>
            </nav>
          ) : null}
          <WidgetCache>
            <div className="flex flex-col gap-6">
              {sections.map((id) => (
                <div key={id}>{render[id]()}</div>
              ))}
            </div>
          </WidgetCache>
        </>
      )}
    </>
  );
}
