import AxeBuilder from "@axe-core/playwright";

import type { FinOpsOverview, MarginOverview } from "@cloudops/contracts";

import { api, expect, raiseDrift, test, triagedTicket } from "./fixtures";

// WCAG 2.1 AA on every screen, in both themes.
test.use({ role: "engineer" });

for (const theme of ["light", "dark"] as const) {
  test.describe(`${theme} theme`, () => {
    test.use({ colorScheme: theme });

    test("every screen passes axe (WCAG 2.1 AA)", async ({ signedIn: page }) => {
      const number = await triagedTicket("human");
      // The shell is part of every screen: navigation areas and, for staff with several
      // customers, the customer selector in the top bar.
      await page.goto("/");
      await expect(page.getByTestId("customer-select")).toBeVisible();
      for (const path of ["/work", `/work/${number}`, "/performance", "/posture", "/resources", "/customers", "/connectors", "/knowledge"]) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await page.waitForLoadState("networkidle");
        const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(results.violations.map((v) => `${path}: ${v.id} ${v.nodes.length}`)).toEqual([]);
      }
    });
  });
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`customer mapping, ${theme} theme`, () => {
    test.use({ role: "customerMaster", colorScheme: theme });

    test("passes axe (WCAG 2.1 AA) and does not scroll sideways at tablet width", async ({ signedIn: page }) => {
      await page.goto("/mapping");
      await expect(page.getByRole("table", { name: "Quarantined names" })).toBeVisible();
      await page.getByRole("button", { name: "Map a name or onboard a tenant…" }).click();
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.map((v) => `/mapping: ${v.id} ${v.nodes.length}`)).toEqual([]);
      await page.setViewportSize({ width: 768, height: 1024 });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    });
  });
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`connectors with acknowledge, ${theme} theme`, () => {
    test.use({ role: "admin", colorScheme: theme });

    test("passes axe (WCAG 2.1 AA) and does not scroll sideways at tablet width", async ({ signedIn: page }) => {
      await page.goto("/connectors");
      await expect(page.getByRole("table", { name: "Connector health" })).toBeVisible();
      await page.getByText("How health is decided").click();
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.map((v) => `/connectors: ${v.id} ${v.nodes.length}`)).toEqual([]);
      await page.setViewportSize({ width: 768, height: 1024 });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    });
  });
}

// Step 18: the portal's own screens, as the customer roles and the onboarding admin see them.
for (const theme of ["light", "dark"] as const) {
  for (const [role, path] of [["approver", "/approvals"], ["cxo", "/users"], ["admin", "/onboarding"]] as const) {
    test.describe(`${path} as ${role}, ${theme} theme`, () => {
      test.use({ role, colorScheme: theme });

      test("passes axe (WCAG 2.1 AA) and does not scroll sideways at tablet width", async ({ signedIn: page }) => {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        if (path === "/users") await page.getByRole("button", { name: "Add a user" }).click();
        if (path === "/onboarding") await page.getByRole("table", { name: "Customers and their portal" }).getByRole("button").first().click();
        await page.waitForLoadState("networkidle");
        const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(results.violations.map((v) => `${path}: ${v.id} ${v.nodes.length}`)).toEqual([]);
        await page.setViewportSize({ width: 768, height: 1024 });
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow).toBeLessThanOrEqual(0);
      });
    });
  }
}

// FinOps, margin and pricing (step 16), as the FinOps role: each screen, its detail
// views, the FX form and a produced quote.
async function commercialPaths(): Promise<string[]> {
  const cost = await api<FinOpsOverview>("finops", "/v1/finops/overview");
  const margin = await api<MarginOverview>("finops", "/v1/commercial/margin");
  const unverified = cost.customers.find((c) => c.verification === "unverified") ?? cost.customers[0];
  const captured = margin.customers.find((m) => !m.not_captured) ?? margin.customers[0];
  if (!unverified || !captured) throw new Error("no seeded cost or margin: run make seed");
  return ["/finops", `/finops/${unverified.tenant.id}`, "/margin?currency=EUR", `/margin/${captured.tenant.id}`, "/pricing", "/settings/commercial"];
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`commercial screens, ${theme} theme`, () => {
    test.use({ role: "finops", colorScheme: theme });

    test("pass axe (WCAG 2.1 AA) and do not scroll sideways at tablet width", async ({ signedIn: page }) => {
      test.slow(); // six screens, twice
      const paths = await commercialPaths();
      for (const width of [undefined, 768] as const) {
        if (width) await page.setViewportSize({ width, height: 1024 });
        for (const path of paths) {
          await page.goto(path);
          await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
          await expect(page.getByRole("status").filter({ hasText: /^Loading/ })).toHaveCount(0);
          if (path === "/pricing") {
            await page.getByLabel("Azure Virtual Machines / Amazon EC2").fill("12");
            await page.getByRole("button", { name: "Price it" }).click();
            await expect(page.getByRole("region", { name: "Quote" })).toBeVisible();
          }
          await page.waitForLoadState("networkidle");
          if (width) {
            const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
            expect(overflow, path).toBeLessThanOrEqual(0);
          } else {
            const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
            expect(results.violations.map((v) => `${path}: ${v.id} ${v.nodes.length}`)).toEqual([]);
          }
        }
      }
    });
  });
}

test.describe("connectors from the keyboard", () => {
  test.use({ role: "admin" });

  test("drift is acknowledged with the keyboard alone", async ({ signedIn: page }) => {
    const field = `e2eKeys_${Date.now().toString(36)}`;
    raiseDrift("aws", "config", field);
    await page.goto("/connectors");
    const button = page.getByRole("button", { name: `Acknowledge ${field} on aws · config` });
    await expect(button).toBeVisible();
    await button.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("status").filter({ hasText: `Drift on ${field} on aws · config acknowledged.` })).toBeVisible();
    await expect(button).toHaveCount(0);
  });
});

test("the queue is usable from the keyboard alone", async ({ signedIn: page }) => {
  await page.goto("/work");
  await expect(page.getByRole("table", { name: "Work items" })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  const firstTicket = page.getByRole("table", { name: "Work items" }).getByRole("link").first();
  const name = (await firstTicket.textContent()) ?? "";
  await firstTicket.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
});

test("no screen scrolls sideways at tablet width", async ({ signedIn: page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  for (const path of ["/work", "/performance", "/posture", "/resources", "/customers", "/connectors"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, path).toBeLessThanOrEqual(0);
  }
});

// Commercial setup's forms (ADR 0033, ADR 0034), open, as the platform administrator.
for (const theme of ["light", "dark"] as const) {
  test.describe(`commercial setup forms, ${theme} theme`, () => {
    test.use({ role: "admin", colorScheme: theme });

    test("pass axe (WCAG 2.1 AA) with the SOW and rate card forms open", async ({ signedIn: page }) => {
      await page.goto("/settings/commercial");
      await expect(page.getByRole("table", { name: "Rate card versions, newest first" })).toBeVisible();
      await page.getByRole("button", { name: /^Record (an amendment|the first SOW)$/ }).click();
      await page.getByRole("button", { name: "Publish a new version" }).click();
      await expect(page.getByRole("form", { name: "Publish a new version" })).toBeVisible();
      await page.waitForLoadState("networkidle");
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.map((v) => `/settings/commercial: ${v.id} ${v.nodes.length}`)).toEqual([]);
    });
  });
}
