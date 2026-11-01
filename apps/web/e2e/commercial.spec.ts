// FinOps, margin and pricing (step 16) per role. The seed (cloudops_devdata) writes
// a month of cost with reconciliation checks, one AWS account drifting from the
// provider's total (Fabrikam), a cost spike on Northwind, closed and provisional
// margin snapshots, and Woodgrove Financial without a SOW. Journeys use the API's
// default month, which follows the seed's anchor, never a hard-coded period. The
// synthetic series dips at weekends and the detector records drops (and, early in
// a month, a month-over-month move), so anomaly counts depend on the day: journeys
// assert the seeded day spike, never a total.
import type { Page } from "@playwright/test";

import { expect, navLabels, test } from "./fixtures";

const COMMERCIAL = ["FinOps", "Margin", "Revenue & margin", "Pricing"];

function row(page: Page, table: string, text: string | RegExp) {
  return page.getByRole("table", { name: table }).getByRole("row").filter({ hasText: text });
}

test.describe("finops", () => {
  test.use({ role: "finops" });

  test("lists FinOps, Margin and Pricing", async ({ signedIn: page }) => {
    await page.goto("/");
    expect(await navLabels(page)).toEqual(["Home", "Resources", "Customers", ...COMMERCIAL, "Commercial setup"]);
  });

  test("an unverified reconciliation is marked with its reason, not shown as a normal figure", async ({ signedIn: page }) => {
    await page.goto("/finops");
    await expect(page.getByRole("heading", { level: 1, name: "FinOps" })).toBeVisible();
    const fabrikam = row(page, "Cost by customer", "Fabrikam Industries Inc.");
    await expect(fabrikam.locator("[data-band]").first()).toHaveAttribute("data-band", "at-risk");
    await expect(fabrikam).toContainText("Unverified");
    await expect(fabrikam).toContainText(/Fabrikam AWS Prod \(legacy\) drifts 1\.2% from the provider’s total/);
    // Consumption and other spend carry the mark too.
    await expect(fabrikam.getByText("Unverified", { exact: true })).toHaveCount(3);
    const northwind = row(page, "Cost by customer", "Northwind Logistics B.V.");
    await expect(northwind.locator("[data-band]").first()).toHaveAttribute("data-band", "healthy");
    await expect(northwind).toContainText("Verified");
    await expect(northwind.getByText("Unverified", { exact: true })).toHaveCount(0);
    await expect(northwind).toContainText(/[1-9]\d* anomal(y|ies)/);
    // Unverified customers sort first.
    await expect(page.getByRole("table", { name: "Cost by customer" }).locator("tbody tr").first()).toContainText("Fabrikam");
  });

  test("a reporting currency adds converted totals beside the billed ones", async ({ signedIn: page }) => {
    await page.goto("/finops");
    await page.getByLabel("Reporting currency").selectOption("EUR");
    await expect(page).toHaveURL(/currency=EUR/);
    await expect(row(page, "Cost by customer", "Contoso Retail Ltd")).toContainText(/GBP [\d,]+\.\d{2}/);
    await expect(row(page, "Cost by customer", "Contoso Retail Ltd")).toContainText(/≈ EUR [\d,]+\.\d{2}|No EUR rate for every day/);
  });

  test("a customer's cost: checks per account, allocation with unallocated rows, anomalies", async ({ signedIn: page }) => {
    await page.goto("/finops");
    await page.getByRole("link", { name: "Fabrikam Industries Inc." }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Fabrikam Industries Inc." })).toBeVisible();
    await expect(page.getByText("These figures are not verified against the provider’s total.")).toBeVisible();
    const aws = row(page, "Reconciliation checks", "Fabrikam AWS Prod (legacy)");
    await expect(aws).toContainText("Unverified");
    await expect(aws).toContainText("drifts 1.2% from the provider’s total");
    await expect(aws).toContainText("1.19%");
    await expect(row(page, "Charges by class", "Consumption")).toContainText("Yes");
    await expect(row(page, "Charges by class", "Licence")).toContainText("No");
    await expect(row(page, "Allocation by application", "Unallocated")).toHaveCount(1);
    await expect(row(page, "Allocation by cost centre", "Unallocated")).toHaveCount(1);

    await page.getByRole("link", { name: "All customers" }).click();
    const listed = row(page, "Cost by customer", "Northwind Logistics B.V.");
    await expect(listed).toContainText(/[1-9]\d* anomal(y|ies)/);
    const counted = Number(/(\d+) anomal(?:y|ies)/.exec(await listed.innerText())?.[1]);
    await page.getByRole("link", { name: "Northwind Logistics B.V." }).click();
    await expect(page.getByText("Every account matches the provider’s own total.")).toBeVisible();
    // Every anomaly the overview counts is listed, and exactly one is the seeded day
    // spike (matched by its Window cell: a row's text runs its cells together).
    await expect(page.getByRole("table", { name: "Cost anomalies" }).locator("tbody tr")).toHaveCount(counted);
    const daySpikes = row(page, "Cost anomalies", "Spike").filter({ has: page.getByRole("cell", { name: "Day", exact: true }) });
    await expect(daySpikes).toHaveCount(1);
  });

  test("records an FX correction with its reason, and it is listed", async ({ signedIn: page }) => {
    const reason = `e2e check ${Date.now().toString(36)}`;
    // A pair and day nothing else reads, with a rate unique to this run.
    const rate = `1.${String(Date.now()).slice(-6)}`;
    await page.goto("/finops");
    const form = page.getByRole("form", { name: "Correct a rate" });
    await form.getByLabel("From currency").fill("CHF");
    await form.getByLabel("To currency").fill("EUR");
    await form.getByLabel("Rate type").selectOption("closing");
    await form.getByLabel("Effective date").fill("2025-01-02");
    await form.getByLabel("Rate", { exact: true }).fill(rate);
    await form.getByLabel("Reason").fill(reason);
    await form.getByRole("button", { name: "Record correction" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Correction recorded: CHF to EUR, closing" })).toBeVisible();
    // The correction, its inverse and any cross through EUR, each with an entry.
    const entries = page.getByRole("table", { name: "FX corrections" }).getByRole("row").filter({ hasText: reason });
    await expect(entries.filter({ hasText: "CHF → EUR" })).toHaveCount(1);
    await expect(entries.filter({ hasText: "EUR → CHF" })).toContainText("Derived from correction");
  });

  test("a pair without EUR asks which EUR leg moves", async ({ signedIn: page }) => {
    await page.goto("/finops");
    const form = page.getByRole("form", { name: "Correct a rate" });
    await form.getByLabel("From currency").fill("GBP");
    await form.getByLabel("To currency").fill("EUR");
    await expect(form.getByLabel("EUR leg that moves")).toHaveCount(0);
    await form.getByLabel("To currency").fill("USD");
    const leg = form.getByLabel("EUR leg that moves");
    await expect(leg).toBeVisible();
    await expect(leg.getByRole("option", { name: "EUR → USD moves, EUR → GBP held" })).toHaveCount(1);
    await form.getByLabel("Effective date").fill("2025-01-02");
    await form.getByLabel("Rate", { exact: true }).fill("1.25");
    await form.getByLabel("Reason").fill("e2e leg check");
    await form.getByRole("button", { name: "Record correction" }).click();
    await expect(form.getByRole("alert")).toContainText("choose which EUR leg moves");
  });

  test("margin: banded, with not captured in words for a customer without a SOW", async ({ signedIn: page }) => {
    await page.goto("/margin");
    await expect(page.getByRole("heading", { level: 1, name: "Margin" })).toBeVisible();
    const woodgrove = row(page, "Margin by customer", "Woodgrove Financial plc");
    await expect(woodgrove).toContainText("Not captured — no SOW");
    await expect(woodgrove.locator("[data-band]").first()).toHaveAttribute("data-band", "unknown");
    // Never a zero or a dash that reads as zero.
    await expect(woodgrove).not.toContainText(/\b0\.0%|GBP 0\.00/);
    await expect(row(page, "Margin by customer", "Fabrikam Industries Inc.")).toContainText("Not captured — no people cost for the month");
    const northwind = row(page, "Margin by customer", "Northwind Logistics B.V.");
    await expect(northwind.locator("[data-band]").first()).toHaveAttribute("data-band", /^(healthy|watch|at-risk|critical)$/);
    await expect(northwind).toContainText(/\d+\.\d%/);
    await expect(northwind).toContainText(/Closed|Provisional/);
    await expect(northwind).toContainText(/EUR [\d,]+\.\d{2}/);
    await expect(northwind.getByRole("list", { name: "Margin, last six months" })).toBeVisible();
  });

  test("margin in a reporting currency shows the FX impact as its own line", async ({ signedIn: page }) => {
    await page.goto("/margin");
    await page.getByLabel("Reporting currency").selectOption("EUR");
    await expect(page.getByRole("columnheader", { name: /Margin in EUR/ })).toBeVisible();
    await expect(row(page, "Margin by customer", "Contoso Retail Ltd")).toContainText(/FX impact EUR -?[\d,]+\.\d{2}/);
    await expect(row(page, "Margin by customer", "Woodgrove Financial plc")).toContainText("Not captured — no SOW");
  });

  test("a customer's margin: variance by workgroup and the version history", async ({ signedIn: page }) => {
    await page.goto("/margin");
    await page.getByRole("link", { name: "Northwind Logistics B.V." }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Northwind Logistics B.V." })).toBeVisible();
    await expect(page.getByText("Margin %", { exact: true })).toBeVisible();
    await expect(page.getByRole("table", { name: "Variance by workgroup and activity type" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Variance by workgroup and activity type" }).locator("tbody tr").first()).toBeVisible();
    await expect(row(page, "Margin versions", "Version 1")).toHaveCount(1);

    await page.getByRole("link", { name: "All customers" }).click();
    await page.getByRole("link", { name: "Woodgrove Financial plc" }).click();
    await expect(page.getByText("Not captured — no SOW").first()).toBeVisible();
    await expect(page.getByText("There is no SOW for this customer")).toBeVisible();
  });

  test("the pricing form produces a quote on the rate card in force", async ({ signedIn: page }) => {
    await page.goto("/pricing");
    await expect(page.getByRole("heading", { level: 1, name: "Pricing" })).toBeVisible();
    await expect(page.getByText(/Rate card \S+, in force from/)).toBeVisible();
    const form = page.getByRole("form", { name: "Quote" });
    await form.getByRole("button", { name: "Price it" }).click();
    await expect(form.getByRole("alert")).toContainText("Enter a count for at least one resource type.");
    await form.getByLabel("Azure Virtual Machines / Amazon EC2").fill("40");
    await form.getByLabel("MS SQL DB / Azure SQL / Amazon RDS").fill("6");
    await form.getByLabel(/Monthly cloud consumption/).fill("25000");
    await form.getByLabel("Coverage").selectOption("24x7");
    await form.getByLabel("Support model").selectOption("Gold");
    await form.getByRole("button", { name: "Price it" }).click();
    const quote = page.getByRole("region", { name: "Quote" });
    await expect(quote).toBeVisible();
    await expect(quote.getByText(/slab \S+/)).toBeVisible();
    await expect(quote.getByText("Total per month")).toBeVisible();
    await expect(quote).toContainText(/EUR [\d,]+\.\d{2}/);
    await expect(quote.getByText("Critical CIs")).toBeVisible();
    await expect(quote.getByRole("table", { name: "Implied team by role" })).toBeVisible();
    await expect(quote.getByText("Modelled").first()).toBeVisible();
  });
});

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("lists the commercial modules too", async ({ signedIn: page }) => {
    await page.goto("/");
    const labels = await navLabels(page);
    for (const label of COMMERCIAL) expect(labels).toContain(label);
  });
});

for (const role of ["engineer", "customer"] as const) {
  test.describe(`${role}, without the commercial modules`, () => {
    test.use({ role });

    test("has no links to them, and typed-in URLs are refused", async ({ signedIn: page }) => {
      await page.goto("/");
      const labels = await navLabels(page);
      for (const label of [...COMMERCIAL, "Commercial setup"]) expect(labels).not.toContain(label);
      for (const path of ["/finops", "/margin", "/revenue", "/pricing", "/settings/commercial", "/finops/54190aa0-4b8a-5d71-b0f1-4fea5cf3fb7b", "/margin/54190aa0-4b8a-5d71-b0f1-4fea5cf3fb7b"]) {
        await page.goto(path);
        await expect(page.getByText("Not available to your role"), path).toBeVisible();
        await expect(page.getByRole("table"), path).toHaveCount(0);
        await expect(page.getByRole("form"), path).toHaveCount(0);
      }
    });
  });
}
