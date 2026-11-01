"use client";

import Link from "next/link";

import type { components } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

type Resource = components["schemas"]["ResourceStaffView"] | components["schemas"]["ResourceView"];

export function ResourceList() {
  useAssistantContext({ screen: "resources" });
  const me = useMe();
  const loaded = useApi((token) => apiGet(token, "/v1/resources"), "resources");
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
    { key: "type", header: "Type", value: (r) => r.type },
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
              { key: "type", label: "Type", value: (r) => r.type },
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
