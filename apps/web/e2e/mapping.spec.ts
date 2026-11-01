// Customer mapping (step 14b, ADR 0015) as Delivery's customer master, and as roles
// without mapping.manage. The local stack is migrated as dev (one person may propose
// and approve); the two-person rule is proven in pytest against the database.
import { expect, holdName, navLabels, test } from "./fixtures";

function unique(prefix: string): string {
  return `${prefix} ${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

test.describe("customer master", () => {
  test.use({ role: "customerMaster" });

  test("sees the quarantine, most sighted first, with a suggestion to map to", async ({ signedIn: page }) => {
    await page.goto("/mapping");
    expect(await navLabels(page)).toContain("Customer mapping");
    await expect(page.getByRole("heading", { level: 1, name: "Customer mapping" })).toBeVisible();
    await expect(page.getByText("one person may propose and approve")).toBeVisible();
    const table = page.getByRole("table", { name: "Quarantined names" });
    await expect(table).toBeVisible();
    const sightings = page.getByRole("columnheader", { name: /Sightings/ });
    await expect(sightings).toHaveAttribute("aria-sort", "descending");
    const counts = (await table.locator("tbody tr td:nth-child(3)").allTextContents()).map((t) => Number(t.replace(/,/g, "")));
    expect(counts.length).toBeGreaterThan(1);
    expect(counts).toEqual([...counts].sort((a, b) => b - a));

    // Opening the form from a row carries its name and source, read-only. Nothing is saved.
    const first = table.getByRole("button", { name: /^Map / }).first();
    const alias = ((await first.getAttribute("aria-label")) ?? "").replace(/^Map /, "");
    await first.click();
    const form = page.getByRole("form", { name: "Map a name to a customer" });
    await expect(form.getByLabel("Name, exactly as the source sends it")).toHaveValue(alias);
    await expect(form.getByLabel("Name, exactly as the source sends it")).toHaveAttribute("readonly", "");
    await form.getByRole("button", { name: "Cancel" }).click();
    await expect(form).toHaveCount(0);
  });

  test("maps a name to an existing customer and approves it", async ({ signedIn: page }) => {
    const alias = unique("E2E Northwind Trading");
    await page.goto("/mapping");
    await page.getByRole("button", { name: "Map a name or onboard a tenant…" }).click();
    const form = page.getByRole("form", { name: "Map a name to a customer" });
    await form.getByLabel("Name the source sent").selectOption("");
    await form.getByLabel("Name, exactly as the source sends it").fill(alias);
    const customer = form.getByLabel("Customer to map to");
    const northwind = (await customer.locator("option", { hasText: "Northwind" }).first().textContent()) ?? "";
    await customer.selectOption({ label: northwind });
    await form.getByLabel("Note for the approver (optional)").fill("Seen on new incidents from the service desk.");
    await form.getByRole("button", { name: "Propose mapping" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Mapping proposed." })).toBeVisible();

    const pending = page.getByTestId("pending-proposal").filter({ hasText: alias });
    await expect(pending).toContainText("Proposed by you");
    await pending.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByRole("status").filter({ hasText: `“${alias}” now maps to` })).toBeVisible();
    await expect(page.getByTestId("pending-proposal").filter({ hasText: alias })).toHaveCount(0);
    await expect(page.getByTestId("decided-proposal").filter({ hasText: alias })).toContainText("Approved");
    const customers = page.getByRole("table", { name: "Customers and their names" });
    await expect(customers.getByRole("row").filter({ hasText: alias })).toContainText("Northwind");
  });

  test("maps a held name, then asks for what it held to be re-processed", async ({ signedIn: page }) => {
    const alias = unique("E2E Held Holdings");
    holdName(alias, 7);
    await page.goto("/mapping");
    await page.getByRole("button", { name: `Map ${alias}` }).click();
    const form = page.getByRole("form", { name: "Map a name to a customer" });
    const customer = form.getByLabel("Customer to map to");
    const northwind = (await customer.locator("option", { hasText: "Northwind" }).first().textContent()) ?? "";
    await customer.selectOption({ label: northwind });
    await form.getByRole("button", { name: "Propose mapping" }).click();
    await page.getByTestId("pending-proposal").filter({ hasText: alias }).getByRole("button", { name: "Approve" }).click();

    const decided = page.getByTestId("decided-proposal").filter({ hasText: alias });
    await expect(decided).toContainText("7 sightings were held before approval");
    await expect(page.getByRole("button", { name: `Map ${alias}` })).toHaveCount(0);
    await decided.getByRole("button", { name: "Re-process held records…" }).click();
    const preview = decided.getByRole("group", { name: `Re-process ${alias}` });
    await expect(preview).toContainText("Up to 7 held sightings");
    await preview.getByRole("button", { name: "Request re-process" }).click();
    await expect(page.getByRole("status").filter({ hasText: `Re-process of “${alias}” requested.` })).toBeVisible();
    await expect(decided.getByTestId("reprocess-status")).toContainText("Re-process requested by you");
    // Asked once: no second request while it waits for the worker.
    await expect(decided.getByRole("button", { name: "Re-process held records…" })).toHaveCount(0);
  });

  test("onboards a new customer with a name, then withdraws the mapping with a reason", async ({ signedIn: page }) => {
    const alias = unique("E2E Prospect Ltd");
    const code = `E${Date.now().toString(36).slice(-6).toUpperCase()}`;
    await page.goto("/mapping");
    await page.getByRole("button", { name: "Map a name or onboard a tenant…" }).click();
    const form = page.getByRole("form", { name: "Map a name to a customer" });
    await form.getByLabel("Name the source sent").selectOption("");
    await form.getByLabel("Name, exactly as the source sends it").fill(alias);
    await form.getByLabel("Onboard a new customer").check();
    await form.getByLabel("Canonical name").fill(`E2E Prospect ${code}`);
    await form.getByLabel("Short code").fill(code);
    // Only a platform administrator may onboard an internal tenant (ADR 0031).
    await expect(form.getByLabel("Internal to Getronics")).toBeDisabled();
    await form.getByRole("button", { name: "Onboard and propose" }).click();
    await expect(form.getByRole("alert")).toContainText("customer or internal");
    await form.getByLabel("A customer", { exact: true }).check();
    await form.getByRole("button", { name: "Onboard and propose" }).click();
    await expect(page.getByRole("status").filter({ hasText: "onboarded, and the mapping proposed" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Customers and their names" }).getByRole("row").filter({ hasText: code })).toBeVisible();

    const pending = page.getByTestId("pending-proposal").filter({ hasText: alias });
    await pending.getByRole("button", { name: "Withdraw…" }).click();
    await pending.getByRole("button", { name: "Reject" }).click();
    await expect(pending.getByRole("alert")).toHaveText("Say why it is rejected.");
    await pending.getByLabel("Reason for rejecting").fill("Proposed in error.");
    await pending.getByRole("button", { name: "Reject" }).click();
    await expect(page.getByTestId("decided-proposal").filter({ hasText: alias })).toContainText("Rejected");
  });

  test("picks the source and the name it sent from lists, and every choice is readable", async ({ signedIn: page }) => {
    const alias = unique("E2E Listed Ltd");
    holdName(alias, 3);
    await page.goto("/mapping");
    await page.getByRole("button", { name: "Map a name or onboard a tenant…" }).click();
    const form = page.getByRole("form", { name: "Map a name to a customer" });
    const source = form.getByLabel("Source system");
    for (const name of ["ServiceNow", "SharePoint", "Qualys", "CoreStack"]) {
      await expect(source.locator("option", { hasText: name })).toHaveCount(1);
    }
    // The held name is offered with its sightings, most sighted first; nothing to type.
    const names = form.getByLabel("Name the source sent");
    await expect(names.locator("option", { hasText: `${alias} (3 sightings)` })).toHaveCount(1);
    await names.selectOption(alias);
    await expect(form.getByLabel("Name, exactly as the source sends it")).toHaveCount(0);

    // A name not held yet is typed instead, and changing source starts the name again.
    await names.selectOption("");
    await expect(form.getByLabel("Name, exactly as the source sends it")).toHaveValue("");
    await source.selectOption("corestack");
    await expect(names.locator("option", { hasText: alias })).toHaveCount(0);

    // The options carry the theme's colours, so no choice draws blank in either theme.
    const customer = form.getByLabel("Customer to map to");
    const [field, option] = await customer.evaluate((el) => {
      const style = (e: Element) => getComputedStyle(e);
      return [style(el).color, style(el.querySelectorAll("option")[1] ?? el).backgroundColor];
    });
    expect(field).not.toEqual(option);
    await form.getByRole("button", { name: "Cancel" }).click();
  });

  test("a short code that is not 2 to 10 capitals is caught before sending", async ({ signedIn: page }) => {
    await page.goto("/mapping");
    await page.getByRole("button", { name: "Map a name or onboard a tenant…" }).click();
    const form = page.getByRole("form", { name: "Map a name to a customer" });
    await form.getByLabel("Name the source sent").selectOption("");
    await form.getByLabel("Name, exactly as the source sends it").fill(unique("E2E Bad Code"));
    await form.getByLabel("Onboard a new customer").check();
    await form.getByLabel("Canonical name").fill("Bad Code Co");
    await form.getByLabel("Short code").fill("X");
    await form.getByRole("button", { name: "Onboard and propose" }).click();
    await expect(form.getByRole("alert")).toContainText("short code of 2 to 10");
  });
});

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("onboards an internal tenant, listed like a customer, and turns it into a customer", async ({ signedIn: page }) => {
    const code = `I${Date.now().toString(36).slice(-6).toUpperCase()}`;
    await page.goto("/mapping");
    await page.getByRole("button", { name: "Map a name or onboard a tenant…" }).click();
    const form = page.getByRole("form", { name: "Map a name to a customer" });
    await form.getByLabel("Name the source sent").selectOption("");
    await form.getByLabel("Name, exactly as the source sends it").fill(unique("E2E Getronics Unit"));
    await form.getByLabel("Onboard a new customer").check();
    await form.getByLabel("Canonical name").fill(`E2E Getronics Unit ${code}`);
    await form.getByLabel("Short code").fill(code);
    await form.getByLabel("Internal to Getronics").check();
    await form.getByRole("button", { name: "Onboard and propose" }).click();
    await expect(page.getByRole("status").filter({ hasText: "onboarded, and the mapping proposed" })).toBeVisible();
    const row = page.getByRole("table", { name: "Customers and their names" }).getByRole("row").filter({ hasText: code });
    await expect(row).toContainText("Internal");
    await row.getByRole("button", { name: `Make E2E Getronics Unit ${code} a customer` }).click();
    await expect(page.getByRole("status").filter({ hasText: "is now a customer" })).toBeVisible();
    await expect(row).toContainText("Customer");
  });
});

for (const role of ["engineer", "customer", "finops"] as const) {
  test.describe(`${role}, without mapping.manage`, () => {
    test.use({ role });

    test("has no link to customer mapping, and the page refuses a typed-in URL", async ({ signedIn: page }) => {
      await page.goto("/");
      expect(await navLabels(page)).not.toContain("Customer mapping");
      await page.goto("/mapping");
      await expect(page.getByText("Not available to your role")).toBeVisible();
      await expect(page.getByRole("table", { name: "Quarantined names" })).toHaveCount(0);
    });
  });
}
