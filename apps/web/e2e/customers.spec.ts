// Settings → Manage customers (ADR 0038): a customer master renames a customer; a
// platform administrator deletes it (offboarded at once, gone from the selector and
// every list), restores it within the retention, and cannot purge it before the
// retention ends. Each journey onboards a customer of its own and leaves it deleted,
// so no seeded customer is used up. Purging itself is proven in pytest
// (db/tests/test_tenant_lifecycle.py): the retention cannot pass in a journey.
import AxeBuilder from "@axe-core/playwright";

import { apiPost, expect, navLabels, test } from "./fixtures";
import type { RoleName } from "./roles";

async function ownCustomer(prefix: string): Promise<{ id: string; name: string; code: string }> {
  const code = `${prefix}${Date.now().toString(36).toUpperCase().slice(-6)}${Math.floor(Math.random() * 10)}`.slice(0, 10);
  const created = await apiPost<{ tenant: { id: string; name: string } }>("customerMaster", "/v1/mapping/tenants", {
    canonical_name: `Lifecycle ${code}`,
    short_code: code,
    internal: false,
  });
  expect(created.status).toBe(201);
  return { ...created.body.tenant, code };
}

async function offboard(role: RoleName, id: string, name: string): Promise<void> {
  const done = await apiPost(role, `/v1/tenants/${id}/offboard`, { confirm_name: name, reason: "End-to-end journey clean-up" });
  expect(done.status).toBe(200);
}

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("renames a customer, deletes it with its name typed, and restores it", async ({ signedIn: page }) => {
    const own = await ownCustomer("LC");
    const renamed = `${own.name} Group`;
    await page.goto("/settings/customers");
    expect(await navLabels(page)).toContain("Manage customers");
    await expect(page.getByRole("heading", { level: 1, name: "Manage customers" })).toBeVisible();
    const customers = page.getByRole("table", { name: "Customers", exact: true });
    await expect(customers.getByRole("row").filter({ hasText: own.name })).toBeVisible();

    // Edit: the name.
    await customers.getByRole("button", { name: `Edit ${own.name}` }).click();
    const edit = page.getByRole("form", { name: `Edit ${own.name}` });
    await edit.getByLabel("Customer name").fill(renamed);
    await edit.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByRole("status").filter({ hasText: `Saved: ${renamed} (${own.code})` })).toBeVisible();
    await expect(customers.getByRole("row").filter({ hasText: renamed })).toBeVisible();

    // Delete: refused until the exact name and a reason are given.
    await customers.getByRole("button", { name: `Delete ${renamed}` }).click();
    const form = page.getByRole("form", { name: `Delete ${renamed}` });
    const confirm = form.getByRole("button", { name: "Delete customer" });
    await expect(form).toContainText("it disappears from every list, the customer selector and every dashboard");
    await form.getByLabel(/Type the customer's name exactly/).fill(renamed.toUpperCase());
    await form.getByLabel(/Reason/).fill("Contract ended");
    await expect(confirm).toBeDisabled();
    await form.getByLabel(/Type the customer's name exactly/).fill(renamed);
    await expect(confirm).toBeEnabled();
    await confirm.click();
    await expect(page.getByRole("status").filter({ hasText: `${renamed} is offboarded.` })).toBeVisible();

    // Gone from the list and the selector at once; listed as deleted, restorable, not purgeable.
    await expect(customers.getByRole("row").filter({ hasText: renamed })).toHaveCount(0);
    await expect(page.getByTestId("customer-select").locator("option", { hasText: renamed })).toHaveCount(0);
    const deleted = page.getByRole("table", { name: "Deleted customers" });
    const row = deleted.getByRole("row").filter({ hasText: renamed });
    await expect(row).toContainText("Contract ended");
    await expect(row).toContainText("by you");
    await expect(row).toContainText("Restorable until");
    await expect(row.getByRole("button", { name: `Purge ${renamed}` })).toHaveCount(0);
    // The API refuses a purge before the retention ends, whatever the console shows.
    const early = await apiPost<{ error: { code: string } }>("admin", `/v1/tenants/${own.id}/purge`, { confirm_name: renamed, confirm_code: own.code });
    expect([early.status, early.body.error.code]).toEqual([409, "retention_not_ended"]);

    // Restore: back in the list and the selector.
    await row.getByRole("button", { name: `Restore ${renamed}` }).click();
    await expect(page.getByRole("status").filter({ hasText: `${renamed} is restored.` })).toBeVisible();
    await expect(customers.getByRole("row").filter({ hasText: renamed })).toBeVisible();
    await expect(page.getByTestId("customer-select").locator("option", { hasText: renamed })).toHaveCount(1);
    await offboard("admin", own.id, renamed);
  });

  test("an offboarded customer is gone from the other screens too", async ({ signedIn: page }) => {
    const own = await ownCustomer("LV");
    await offboard("admin", own.id, own.name);
    for (const path of ["/customers", "/mapping", "/onboarding", "/cloud-onboarding"]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await page.waitForLoadState("networkidle");
      await expect(page.getByText(own.name)).toHaveCount(0);
    }
  });
});

test.describe("customer master", () => {
  test.use({ role: "customerMaster" });

  test("may edit but not delete", async ({ signedIn: page }) => {
    const own = await ownCustomer("LM");
    await page.goto("/settings/customers");
    const customers = page.getByRole("table", { name: "Customers", exact: true });
    await expect(customers.getByRole("button", { name: `Edit ${own.name}` })).toBeVisible();
    await expect(customers.getByRole("button", { name: `Delete ${own.name}` })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Deleted customers" })).toHaveCount(0);
    const refused = await apiPost<{ error: { code: string } }>("customerMaster", `/v1/tenants/${own.id}/offboard`, {
      confirm_name: own.name,
      reason: "not mine to do",
    });
    expect([refused.status, refused.body.error.code]).toEqual([403, "forbidden"]);
    await offboard("admin", own.id, own.name);
  });
});

for (const role of ["engineer", "customer"] as const) {
  test.describe(`${role}`, () => {
    test.use({ role });

    test("has no link and no access", async ({ signedIn: page }) => {
      await page.goto("/settings/customers");
      expect(await navLabels(page)).not.toContain("Manage customers");
      await expect(page.getByText("Not available to your role")).toBeVisible();
    });
  });
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`manage customers, ${theme} theme`, () => {
    test.use({ role: "admin", colorScheme: theme });

    test("passes axe (WCAG 2.1 AA) with the delete form open, and does not scroll sideways at tablet width", async ({ signedIn: page }) => {
      const own = await ownCustomer("LA");
      await page.goto("/settings/customers");
      const customers = page.getByRole("table", { name: "Customers", exact: true });
      await customers.getByRole("button", { name: `Delete ${own.name}` }).click();
      await expect(page.getByRole("form", { name: `Delete ${own.name}` })).toBeVisible();
      await page.waitForLoadState("networkidle");
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.map((v) => `/settings/customers: ${v.id} ${v.nodes.length}`)).toEqual([]);
      await page.setViewportSize({ width: 768, height: 1024 });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      await offboard("admin", own.id, own.name);
    });
  });
}
