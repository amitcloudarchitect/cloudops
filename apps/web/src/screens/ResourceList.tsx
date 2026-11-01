"use client";

import Link from "next/link";

import type { components } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { customerQuery, useCustomer } from "@/lib/customer";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

type Resource = components["schemas"]["ResourceStaffView"] | components["schemas"]["ResourceView"];
type Page = components["schemas"]["ResourceList"];

/** Every page of the list, in order: the table's search, facets and export work
 * over the whole estate, never over a first page that looks like all of it. */
async function allResources(token: string, tenantId: string | null): Promise<Page> {
  const narrowed = customerQuery(tenantId);
  const first = await apiGet(token, "/v1/resources", {}, narrowed);
  const items = [...first.items];
  let next = first.next_cursor;
  while (next !== null && next !== undefined) {
    const page = await apiGet(token, "/v1/resources", {}, { ...narrowed, cursor: next });
    items.push(...page.items);
    next = page.next_cursor;
  }
  return { ...first, items, next_cursor: null };
}

function customer(r: Resource): string | null {
  return "customer" in r ? r.customer : null;
}

/** The canonical type, or the provider's own when the platform has none for it. */
function typeLabel(r: Resource): string {
  return r.type === "other" && "type_native" in r ? r.type_native : r.type;
}

export function ResourceList() {
  useAssistantContext({ screen: "resources" });
  const me = useMe();
  const staff = me.kind === "staff";
  const { tenantId } = useCustomer();
  const loaded = useApi((token) => allResources(token, tenantId), `resources:${tenantId ?? "all"}`);
  const columns: Column<Resource>[] = [
    {
      key: "name",
      header: "Name",
      value: (r) => r.name,
      render: (r) => (
        <Link href={`/resources/${r.id}`} className="link font-medium">
          {r.name}
        </Link>
      ),
    },
    ...(staff ? [{ key: "customer", header: "Customer", value: customer }] : []),
    { key: "type", header: "Type", value: typeLabel },
    { key: "region", header: "Region", value: (r) => r.region },
    { key: "state", header: "State", value: (r) => r.state },
  ];
  return (
    <>
      <PageHeader title="Resources" subtitle="Cloud resources in your customers’ subscriptions." />
      {loaded.status === "loading" ? <Loading what="resources" /> : null}
      {loaded.status === "error" ? <LoadError error={loaded.error} what="resources" /> : null}
      {loaded.status === "ready" ? (
        <>
          {loaded.data.count ? (
            <div className="mb-2">
              <Provenance metric={loaded.data.count} />
            </div>
          ) : null}
          <DataTable
            id="resources"
            caption="Resources"
            rows={loaded.data.items}
            rowKey={(r) => r.id}
            columns={columns}
            facets={[
              ...(staff ? [{ key: "customer", label: "Customer", value: customer }] : []),
              { key: "type", label: "Type", value: typeLabel },
              { key: "region", label: "Region", value: (r) => r.region },
            ]}
            role={me.role}
            empty={{
              title: "No resources yet",
              next: "Resources appear once a customer’s subscription is connected and its inventory has synced.",
            }}
          />
        </>
      ) : null}
    </>
  );
}
