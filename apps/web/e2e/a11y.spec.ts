import AxeBuilder from "@axe-core/playwright";

import { expect, raiseDrift, test, triagedTicket } from "./fixtures";

// WCAG 2.1 AA on every screen, in both themes.
test.use({ role: "engineer" });

for (const theme of ["light", "dark"] as const) {
  test.describe(`${theme} theme`, () => {
    test.use({ colorScheme: theme });

    test("every screen passes axe (WCAG 2.1 AA)", async ({ signedIn: page }) => {
      const number = await triagedTicket("human");
      for (const path of ["/work", `/work/${number}`, "/performance", "/posture", "/resources", "/customers", "/connectors"]) {
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
      await page.getByRole("button", { name: "Map a name or onboard a customer…" }).click();
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
