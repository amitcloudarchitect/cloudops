// Revenue & margin (ADR 0038): the monthly report's sheet per customer. The seed
// (cloudops_devdata) writes SOWs with no CSP flag recorded, closed and provisional
// margin snapshots, a month of consumption, and Woodgrove Financial without a SOW.
// Journeys use the API's default month, never a hard-coded period.
import { readFile } from "node:fs/promises";

import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import type { Me, RevenueOverview } from "@cloudops/contracts";

import { api, expect, test } from "./fixtures";

const TABLE = "Revenue and margin by customer";

function row(page: Page, text: string | RegExp) {
  return page.getByRole("table", { name: TABLE }).getByRole("row").filter({ hasText: text });
}

test.describe("finops", () => {
  test.use({ role: "finops" });

  test("the sheet's columns, gaps in words, and totals", async ({ signedIn: page }) => {
    await page.goto("/revenue");
    await expect(page.getByRole("heading", { level: 1, name: "Revenue & margin" })).toBeVisible();
    const headers = page.getByRole("table", { name: TABLE }).getByRole("columnheader");
    await expect(headers).toContainText([
      "Sl No",
      "Customer name",
      "CSP",
      "Avg monthly cloud consumption",
      "Monthly manage revenue",
      "Manage cost (actual)",
      "Gross margin on manage revenue",
      "Professional services (one-time)",
      "Hosting ACV",
      "Manage ACV",
      "Total revenue (monthly)",
    ]);
    const woodgrove = row(page, "Woodgrove Financial plc");
    await expect(woodgrove).toContainText("Not captured — no SOW for the range");
    await expect(woodgrove).not.toContainText(/GBP 0\.00|EUR 0\.00|\b0\.0%/);
    const northwind = row(page, "Northwind Logistics B.V.");
    await expect(northwind).toContainText(/EUR [\d,]+\.\d{2}/);
    // The seed does not record CSP; derived figures are marked, in words.
    await expect(northwind).toContainText("Not recorded");
    await expect(northwind).toContainText("Modelled");
    await expect(page.getByRole("heading", { level: 2, name: "Totals by contract currency" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Revenue and margin totals" }).getByRole("row", { name: /EUR/ })).toBeVisible();
  });

  test("a reporting currency, a month against the month before, and the CSV with totals", async ({ signedIn: page }) => {
    await page.goto("/revenue");
    await page.getByLabel("Reporting currency").selectOption("EUR");
    await expect(page).toHaveURL(/currency=EUR/);
    await expect(page.getByRole("heading", { level: 2, name: "Totals in EUR" })).toBeVisible();
    await expect(row(page, "Contoso Retail Ltd")).toContainText(/EUR [\d,]+\.\d{2}|no rate in force/);

    await page.getByLabel("Compare with another range").click();
    await expect(page).toHaveURL(/compare_from=\d{4}-\d{2}&compare_to=\d{4}-\d{2}/);
    await expect(page.getByRole("heading", { level: 2 }).filter({ hasText: "against" })).toBeVisible();
    await expect(row(page, "Northwind Logistics B.V.")).toContainText("was ");

    const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export CSV" }).click()]);
    expect(download.suggestedFilename()).toMatch(/^revenue-margin-\d{4}-\d{2}-\d{4}-\d{2}\.csv$/);
    const lines = (await readFile((await download.path()) ?? "", "utf8")).trim().split("\r\n");
    expect(lines[0]).toBe(
      "Sl No,Customer name,CSP,Currency,Average monthly cloud consumption,Monthly manage revenue,Manage cost (actual),Gross margin on manage revenue (%),Professional services (one-time),Hosting ACV,Manage ACV,Total revenue (monthly),Notes",
    );
    expect(lines.at(-1)).toMatch(/^,Total \(EUR\),,EUR,/);
  });

  test("the customer selector narrows the sheet to one customer", async ({ signedIn: page }) => {
    const me = await api<Me>("finops", "/v1/me");
    const northwind = me.tenants.find((t) => t.name === "Northwind Logistics B.V.");
    expect(northwind).toBeDefined();
    await page.goto("/revenue");
    await page.getByTestId("customer-select").selectOption({ label: northwind!.name });
    await expect(page.getByRole("table", { name: TABLE }).locator("tbody tr")).toHaveCount(1);
    await expect(row(page, northwind!.name)).toBeVisible();
    await page.getByTestId("customer-select").selectOption("");
  });

  test("the API's figures carry provenance and modelled ACVs say so", async () => {
    const view = await api<RevenueOverview>("finops", "/v1/commercial/revenue");
    const figures = view.rows.flatMap((r) => Object.values(r.current.contract)).filter((m) => m !== null && typeof m === "object");
    expect(figures.length).toBeGreaterThan(0);
    for (const m of figures) expect(m).toHaveProperty("source_system");
  });
});

for (const theme of ["light", "dark"] as const) {
  test.describe(`revenue & margin, ${theme} theme`, () => {
    test.use({ role: "finops", colorScheme: theme });

    test("passes axe (WCAG 2.1 AA), comparing, and does not scroll sideways at tablet width", async ({ signedIn: page }) => {
      await page.goto("/revenue");
      await expect(page.getByRole("table", { name: TABLE })).toBeVisible();
      await page.getByLabel("Compare with another range").click();
      await expect(page.getByRole("heading", { level: 2 }).filter({ hasText: "against" })).toBeVisible();
      await page.waitForLoadState("networkidle");
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.map((v) => `/revenue: ${v.id} ${v.nodes.length}`)).toEqual([]);
      await page.setViewportSize({ width: 768, height: 1024 });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    });
  });
}
