// The resource list (staff): every page loaded, the true total shown, the customer as
// a column and a facet, and the provider's own type where the platform has none.
import type { Page } from "@playwright/test";

import type { MetricInt, ResourceList, ResourceStaffView } from "@cloudops/contracts";

import { api, expect, test } from "./fixtures";

test.use({ role: "engineer" });

/** Every resource the engineer can see, page by page, as the API returns them. */
async function everyResource(): Promise<{ total: number; items: ResourceStaffView[] }> {
  let page = await api<ResourceList>("engineer", "/v1/resources");
  const total = page.count.value;
  const items = [...page.items];
  while (page.next_cursor !== null) {
    page = await api<ResourceList>("engineer", `/v1/resources?cursor=${page.next_cursor}`);
    items.push(...page.items);
  }
  return { total, items: items.filter((r): r is ResourceStaffView => "customer" in r) };
}

test("shows every resource in scope, with its customer", async ({ signedIn: page }) => {
  const { total, items } = await everyResource();
  expect(items).toHaveLength(total);
  const customer = items[0]?.customer;
  if (!customer) throw new Error("no seeded resource: run make seed");
  await page.goto("/resources");
  const table = page.getByRole("table", { name: "Resources" });
  await expect(table.getByRole("columnheader", { name: /Customer/ })).toBeVisible();
  await expect(page.getByText(`Showing ${total} of ${total}`)).toBeVisible();

  await page.locator("#main").getByLabel("Customer").selectOption(customer);
  const theirs = items.filter((r) => r.customer === customer).length;
  await expect(page.getByText(`Showing ${theirs} of ${total}`)).toBeVisible();
});

const COUNT: MetricInt = {
  value: 3,
  unit: "resources",
  source_system: "platform_inventory",
  as_of: "2026-11-01T00:00:00Z",
  query_ref: "inventory.resource_count@1",
  is_modelled: false,
};

function resource(n: number, name: string, type: string, typeNative: string, customer: string): ResourceStaffView {
  return {
    id: `00000000-0000-4000-8000-00000000010${n}`,
    tenant_id: "00000000-0000-4000-8000-000000000001",
    customer,
    account_id: "00000000-0000-4000-8000-000000000002",
    native_id: `/x/${name}`,
    name,
    type,
    type_native: typeNative,
    region: "westeurope",
    state: "Succeeded",
    tags: {},
  };
}

/** Two pages of a three-resource estate, as the API pages it. */
async function twoPages(page: Page): Promise<string[]> {
  const asked: string[] = [];
  await page.route(/\/v1\/resources(\?.*)?$/, async (route, request) => {
    const cursor = new URL(request.url()).searchParams.get("cursor") ?? "0";
    asked.push(cursor);
    const body: ResourceList =
      cursor === "0"
        ? { items: [resource(1, "alpha-vm", "compute.vm", "Microsoft.Compute/virtualMachines", "Alpha"), resource(2, "alpha-key", "other", "Microsoft.Compute/sshPublicKeys", "Alpha")], count: COUNT, next_cursor: "2" }
        : { items: [resource(3, "beta-disk", "storage.disk", "Microsoft.Compute/disks", "Beta")], count: COUNT, next_cursor: null };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  return asked;
}

test("loads every page, and names a type the platform does not map by the provider's type", async ({ signedIn: page }) => {
  const asked = await twoPages(page);
  await page.goto("/resources");
  const table = page.getByRole("table", { name: "Resources" });
  await expect(page.getByText("Showing 3 of 3")).toBeVisible();
  // Both pages, each once per load (the dev server's strict mode loads twice).
  expect([...new Set(asked)].sort()).toEqual(["0", "2"]);
  expect(asked.filter((o) => o === "0")).toHaveLength(asked.filter((o) => o === "2").length);
  await expect(table.getByRole("row", { name: /alpha-key/ })).toContainText("Microsoft.Compute/sshPublicKeys");
  await expect(table.getByRole("row", { name: /alpha-vm/ })).toContainText("compute.vm");
  await expect(page.locator("#main").getByLabel("Type").locator("option", { hasText: "Microsoft.Compute/sshPublicKeys" })).toHaveCount(1);
  await expect(page.locator("#main").getByLabel("Customer").locator("option")).toHaveText(["Any", "Alpha", "Beta"]);
});
