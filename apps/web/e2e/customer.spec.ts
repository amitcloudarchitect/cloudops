import type { WorkItemList } from "@cloudops/contracts";

import { api, expect, navLabels, test, triagedTicket } from "./fixtures";

test.use({ role: "customer" });

test("sees its own services, without staff-only detail", async ({ signedIn: page }) => {
  await page.goto("/");
  // The viewer role opens on its overview (its landing, migration 0011).
  await expect(page).toHaveURL(/\/customers$/);
  expect(await navLabels(page)).toEqual(["Work queue", "Performance", "Security posture", "Resources", "Customers"]);
  await page.goto("/work");
  await expect(page.getByText("Customer viewer · Customer")).toBeVisible();
  const table = page.getByRole("table", { name: "Work items" });
  await expect(table).toBeVisible();
  await expect(table.getByRole("columnheader", { name: /Triage/ })).toHaveCount(0);
  await expect(table.getByRole("columnheader", { name: /Customer/ })).toHaveCount(0);
});

test("never sees the agents' triage of its ticket", async ({ signedIn: page }) => {
  const number = await triagedTicket("human");
  await page.goto(`/work/${number}`);
  await expect(page.getByRole("heading", { level: 1, name: number })).toBeVisible();
  await expect(page.getByRole("region", { name: "Agent triage" })).toHaveCount(0);
  await expect(page.getByText("Assigned to")).toHaveCount(0);
});

test("another customer's ticket is not found", async ({ signedIn: page }) => {
  const theirs = await api<WorkItemList>("engineer", "/v1/work-items?status=all");
  const foreign = theirs.items.find((w) => !w.tenant.name.startsWith("Northwind"));
  expect(foreign, "the engineer sees a second customer").toBeTruthy();
  await page.goto(`/work/${foreign!.number}`);
  await expect(page.getByText("No such ticket")).toBeVisible();
});

test("its overview is one customer, figures it may see", async ({ signedIn: page }) => {
  await page.goto("/customers");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(/Northwind/);
  await expect(page.getByRole("link", { name: /^Open incidents.*View the evidence$/ })).toHaveCount(1);
  await expect(page.getByText(/Cloud cost, 30 days/).first()).toBeVisible();
});
