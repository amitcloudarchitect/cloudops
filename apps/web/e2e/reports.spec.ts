// Monthly report packs (step 17): generated from live data in the contract
// currency, the financial sections from step 16's stored figures, and nothing
// approved or published until a person has written every narrative.
import type { ReportPackView } from "@cloudops/contracts";

import { api, apiPost, expect, navLabels, test } from "./fixtures";

function lastMonth(): string {
  const now = new Date();
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** A customer whose SOW is in force last month: the first one generation accepts. */
async function customerWithSow(): Promise<{ id: string; name: string }> {
  const me = await api<{ tenants: { id: string; name: string }[] }>("admin", "/v1/me");
  for (const t of me.tenants) {
    const got = await apiPost<ReportPackView>("admin", "/v1/reports/packs", { tenant_id: t.id, period: lastMonth() });
    if (got.status === 201) return t;
  }
  throw new Error("no seeded customer has a SOW in force last month: run make seed");
}

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("generates a pack, writes each narrative, then approves and publishes it", async ({ signedIn: page }) => {
    const customer = await customerWithSow();
    await page.goto("/reports");
    expect(await navLabels(page)).toContain("Reports");
    const form = page.getByRole("form", { name: "Generate a monthly pack" });
    await form.getByLabel("Customer").selectOption(customer.id);
    await form.getByLabel("Month").fill(lastMonth());
    await form.getByRole("button", { name: "Generate pack" }).click();
    await expect(page.getByRole("heading", { level: 1, name: `${customer.name} · ${lastMonth()}` })).toBeVisible();

    const financial = page.getByRole("region", { name: "Financial dashboard" });
    // The month's margin snapshot when step 16 computed one, else the engine's reason.
    await expect(financial).toContainText(/Revenue: [A-Z]{3} [\d,.-]+|Not captured: /);
    await expect(page.getByRole("region", { name: "Revenue mix" })).toContainText(
      /Monthly manage revenue: [A-Z]{3} [\d,.-]+|Not captured: /,
    );
    await expect(page.getByRole("region", { name: "Customer satisfaction" })).toContainText("Not captured");
    await expect(page.getByRole("region", { name: "Volumetrics" })).toContainText("Incidents opened");
    const approve = page.getByRole("button", { name: "Approve" });
    await expect(approve).toBeDisabled();

    for (const section of ["executive summary", "volumetrics", "vulnerabilities", "automation progress"]) {
      const field = page.getByLabel(`Narrative for ${section}`);
      if ((await field.count()) === 0) continue; // a section without data has no narrative
      await field.fill(`Reviewed ${section} for the month.`);
      await field.locator("xpath=ancestor::form").getByRole("button", { name: "Save narrative" }).click();
      await expect(page.getByRole("status").filter({ hasText: `Narrative for ${section} saved.` })).toBeVisible();
    }
    await expect(approve).toBeEnabled();
    await approve.click();
    await expect(page.getByText("Approved, not published")).toBeVisible();
    await page.getByRole("button", { name: "Publish" }).click();
    await expect(page.getByText("Published", { exact: true })).toBeVisible();
  });

  test("consolidates the portfolio in the reporting currency", async ({ signedIn: page }) => {
    await page.goto("/reports");
    await page.getByLabel("Reporting currency").selectOption("GBP");
    await expect(page.getByRole("table", { name: "Portfolio in GBP" })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: /Billed cloud consumption \(GBP\)/ })).toBeVisible();
  });
});

test.describe("engineer", () => {
  test.use({ role: "engineer" });

  test("is not offered reports", async ({ signedIn: page }) => {
    await page.goto("/reports");
    expect(await navLabels(page)).not.toContain("Reports");
    await expect(page.getByText("Not available to your role")).toBeVisible();
  });
});
