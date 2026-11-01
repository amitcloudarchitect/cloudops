import type { Me } from "@cloudops/contracts";

import { api, expect, test } from "./fixtures";

// The customer selector (top bar): every staff user narrows customer-specific screens
// to one customer; the API refuses a tenant outside scope. A customer sees its own
// organisation's name there instead, never a choice.

test.describe("staff with several customers", () => {
  test.use({ role: "engineer" });

  test("narrows the screens to one customer, and remembers the choice", async ({ signedIn: page }) => {
    const me = await api<Me>("engineer", "/v1/me");
    expect(me.tenants.length).toBeGreaterThan(1);
    const [first] = me.tenants;

    await page.goto("/customers");
    const select = page.getByTestId("customer-select");
    await expect(select).toBeVisible();
    await expect(select.getByRole("option")).toHaveText(["All customers", ...me.tenants.map((t) => t.name)]);
    const sections = page.getByRole("main").getByRole("heading", { level: 2 });
    await expect(sections).toHaveCount(me.tenants.length);

    await select.selectOption({ label: first!.name });
    await expect(sections).toHaveText([first!.name]);

    await page.goto("/work");
    const table = page.getByRole("table", { name: "Work items" });
    await expect(table).toBeVisible();
    await page.getByLabel("View").selectOption("All");
    const customers = await table.locator("tbody tr").evaluateAll((rows) =>
      rows.map((r) => Array.from(r.querySelectorAll("td")).map((c) => c.textContent ?? "")),
    );
    expect(customers.length).toBeGreaterThan(0);
    expect(customers.every((cells) => cells.includes(first!.name))).toBe(true);

    await page.reload();
    await expect(page.getByTestId("customer-select")).toHaveValue(first!.id);
    await page.getByTestId("customer-select").selectOption("");
    await page.goto("/customers");
    await expect(sections).toHaveCount(me.tenants.length);
  });

  test("forgets a remembered customer no longer in scope", async ({ signedIn: page }) => {
    const me = await api<Me>("engineer", "/v1/me");
    await page.addInitScript((oid) => {
      window.localStorage.setItem(`cloudops.customer.${oid}`, "00000000-0000-4000-8000-000000000000");
    }, me.oid);
    await page.goto("/customers");
    await expect(page.getByTestId("customer-select")).toHaveValue("");
    await expect(page.getByRole("main").getByRole("heading", { level: 2 })).toHaveCount(me.tenants.length);
  });
});

test.describe("a customer", () => {
  test.use({ role: "customer" });

  test("has no selector, and the top bar names its own organisation", async ({ signedIn: page }) => {
    await page.goto("/customers");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(/Northwind/);
    await expect(page.getByTestId("customer-select")).toHaveCount(0);
    await expect(page.getByTestId("customer-name")).toContainText("Northwind");
  });
});
