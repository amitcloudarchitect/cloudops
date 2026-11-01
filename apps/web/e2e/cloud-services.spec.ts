// Cloud Services (ADR 0038): the drill-down a person walks, customer to account to
// resource group to resource, what each role may see (cost for staff holding
// cost.read only; the customer roles granted the area see their own tenant), the
// refusal for a role without it, and axe in both themes. The figures themselves are
// the database's (db/tests/test_api_cloud_services.py). The seed runs the audit
// connector over its made-up fixtures for one synthetic subscription.

import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import type { CloudOverview } from "@cloudops/contracts";

import { api, expect, test } from "./fixtures";

async function loaded(page: Page, heading: string | RegExp) {
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
  await expect(page.getByText(/^Loading /)).toHaveCount(0);
}

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("drills from the overview to an account, a resource group and a resource", async ({ signedIn: page }) => {
    const overview = await api<CloudOverview>("admin", "/v1/cloud-services/overview");
    const audited = overview.accounts.find((a) => Number(a.summary.issues.total.value) > 0);
    if (!audited) throw new Error("no audited account: run make seed");

    await page.goto("/cloud-services");
    await loaded(page, "Cloud Services");
    await expect(page.getByRole("navigation", { name: "Modules" }).getByRole("link", { name: "Overview" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Highest-impact open findings" }).getByRole("row").nth(1)).toBeVisible();
    await expect(page.getByRole("table", { name: "Cloud accounts" }).getByRole("columnheader", { name: /Cost/ })).toBeVisible();

    await page.getByRole("table", { name: "Cloud accounts" }).getByRole("link", { name: new RegExp(audited.account.display_name) }).first().click();
    await loaded(page, audited.account.display_name);
    const groups = page.getByRole("table", { name: "Account by resource group" });
    await expect(groups).toBeVisible();
    await groups.getByRole("link").first().click();
    const resources = page.getByRole("table", { name: /^Resources in / });
    await expect(resources).toBeVisible();
    await resources.getByRole("link").first().click();
    await expect(page.getByRole("heading", { level: 2, name: "Checks" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "History of changes" })).toBeVisible();
    await expect(page.getByText("CMDB CI")).toBeVisible();
  });

  test("filters findings by source and impact, kept in the address", async ({ signedIn: page }) => {
    await page.goto("/cloud-services/findings");
    await loaded(page, "Cloud findings");
    await page.getByRole("navigation", { name: "Finding filters" }).getByRole("link", { name: "Advisor" }).click();
    await expect(page).toHaveURL(/kind=advisor/);
    await expect(page.getByRole("table", { name: "Cloud findings" })).toBeVisible();
    await page.getByRole("navigation", { name: "Finding filters" }).getByRole("link", { name: "High" }).click();
    await expect(page).toHaveURL(/severity=high/);
    await expect(page).toHaveURL(/kind=advisor/);
  });
});

test.describe("engineer, without cost.read", () => {
  test.use({ role: "engineer" });

  test("sees the area and no cost", async ({ signedIn: page }) => {
    await page.goto("/cloud-services");
    await loaded(page, "Cloud Services");
    await expect(page.getByText("Provider cost is shown to Getronics staff with cost access only.")).toBeVisible();
    await expect(page.getByRole("columnheader", { name: /Cost/ })).toHaveCount(0);
  });
});

for (const role of ["architect", "security"] as const) {
  test.describe(`customer ${role}`, () => {
    test.use({ role });

    test("sees its own tenant's view, without cost", async ({ signedIn: page }) => {
      await page.goto("/cloud-services");
      await loaded(page, "Cloud Services");
      await expect(page.getByText("Provider cost is shown to Getronics staff with cost access only.")).toBeVisible();
      await expect(page.getByRole("table", { name: "Cloud Services by customer" })).toHaveCount(0);
    });
  });
}

for (const role of ["customerEngineer", "customer"] as const) {
  test.describe(`${role}, without cloud_services.read`, () => {
    test.use({ role });

    test("has no link, and a typed-in URL is refused", async ({ signedIn: page }) => {
      await page.goto("/");
      await expect(page.getByRole("list", { name: "Cloud Services" })).toHaveCount(0);
      for (const path of ["/cloud-services", "/cloud-services/findings"]) {
        await page.goto(path);
        await expect(page.getByText("Not available to your role"), path).toBeVisible();
        await expect(page.getByRole("table"), path).toHaveCount(0);
      }
    });
  });
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`Cloud Services, ${theme} theme`, () => {
    test.use({ role: "admin", colorScheme: theme });

    test("passes axe (WCAG 2.1 AA)", async ({ signedIn: page }) => {
      const overview = await api<CloudOverview>("admin", "/v1/cloud-services/overview");
      const account = overview.accounts.find((a) => Number(a.summary.issues.total.value) > 0)?.account.id;
      const paths = ["/cloud-services", "/cloud-services/findings", ...(account ? [`/cloud-services/accounts/${account}`] : [])];
      for (const path of paths) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await page.waitForLoadState("networkidle");
        const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(results.violations.map((v) => `${path}: ${v.id} ${v.nodes.length}`)).toEqual([]);
      }
    });
  });
}
