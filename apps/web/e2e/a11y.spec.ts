import AxeBuilder from "@axe-core/playwright";

import { expect, test, triagedTicket } from "./fixtures";

// WCAG 2.1 AA on every screen, in both themes.
test.use({ role: "engineer" });

for (const theme of ["light", "dark"] as const) {
  test.describe(`${theme} theme`, () => {
    test.use({ colorScheme: theme });

    test("every screen passes axe (WCAG 2.1 AA)", async ({ signedIn: page }) => {
      const number = await triagedTicket("human");
      for (const path of ["/work", `/work/${number}`, "/resources", "/customers"]) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await page.waitForLoadState("networkidle");
        const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(results.violations.map((v) => `${path}: ${v.id} ${v.nodes.length}`)).toEqual([]);
      }
    });
  });
}

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
  for (const path of ["/work", "/resources", "/customers"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, path).toBeLessThanOrEqual(0);
  }
});
