import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, navAreas, navLabels, test } from "./fixtures";

// The left navigation (ADR 0032, decision 6): areas collapse on their own, the sidebar
// collapses to an icon rail (the default below desktop width), both remembered, and
// neither changes which modules a role sees.
test.use({ role: "engineer" });

const nav = (page: Page) => page.getByRole("navigation", { name: "Modules" });
const sidenav = (page: Page) => page.getByTestId("sidenav");
const sideways = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

test.describe("full sidebar", () => {
  test.beforeEach(({}, info) => test.skip(info.project.name !== "desktop", "the full sidebar is the desktop default"));

  test("an area collapses and expands, by mouse and keyboard, and stays as left", async ({ signedIn: page }) => {
    await page.goto("/");
    await expect(sidenav(page)).toHaveAttribute("data-rail", "false");
    const operations = nav(page).getByRole("button", { name: "Operations" });
    const list = page.getByRole("list", { name: "Operations" });
    await expect(operations).toHaveAttribute("aria-expanded", "true");
    await expect(operations).toHaveAttribute("aria-controls", "nav-operations-list");
    await expect(list.getByRole("link", { name: "Work queue" })).toBeVisible();

    await operations.click();
    await expect(operations).toHaveAttribute("aria-expanded", "false");
    await expect(nav(page).getByRole("link", { name: "Work queue" })).toBeHidden();
    // Other areas are untouched.
    await expect(nav(page).getByRole("link", { name: "Security posture" })).toBeVisible();

    await page.reload();
    await expect(nav(page).getByRole("button", { name: "Operations" })).toHaveAttribute("aria-expanded", "false");
    expect(await navAreas(page)).toEqual(["Security", "Knowledge", "Settings"]);

    await operations.focus();
    await page.keyboard.press("Enter");
    await expect(operations).toHaveAttribute("aria-expanded", "true");
    await page.keyboard.press("Space");
    await expect(operations).toHaveAttribute("aria-expanded", "false");
    await page.keyboard.press("Space");
    await expect(list.getByRole("link", { name: "Work queue" })).toBeVisible();
  });

  test("collapses to a rail with tooltips and accessible names, and stays a rail", async ({ signedIn: page }) => {
    await page.goto("/");
    const before = await navLabels(page);
    const collapse = nav(page).getByRole("button", { name: "Collapse sidebar" });
    await expect(collapse).toHaveAttribute("aria-expanded", "true");
    await expect(collapse).toHaveAttribute("aria-controls", "nav-areas");
    await collapse.focus();
    await page.keyboard.press("Enter");
    await expect(sidenav(page)).toHaveAttribute("data-rail", "true");

    // Icons only, but every link keeps its name; the rail shows every area.
    const work = nav(page).getByRole("link", { name: "Work queue" });
    await expect(work).toBeVisible();
    expect(await navLabels(page)).toEqual(before);
    await work.hover();
    await expect(page.getByTestId("nav-tooltip")).toHaveText("Work queue");
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("nav-tooltip")).toHaveCount(0);
    await nav(page).getByRole("link", { name: "Resources" }).focus();
    await expect(page.getByTestId("nav-tooltip")).toHaveText("Resources");

    await page.reload();
    await expect(sidenav(page)).toHaveAttribute("data-rail", "true");
    const expand = nav(page).getByRole("button", { name: "Expand sidebar" });
    await expect(expand).toHaveAttribute("aria-expanded", "false");
    await expand.focus();
    await page.keyboard.press("Space");
    await expect(sidenav(page)).toHaveAttribute("data-rail", "false");
  });

  test("follows the window to a rail until the user chooses", async ({ signedIn: page }) => {
    await page.goto("/");
    await expect(sidenav(page)).toHaveAttribute("data-rail", "false");
    await page.setViewportSize({ width: 820, height: 1180 });
    await expect(sidenav(page)).toHaveAttribute("data-rail", "true");
    expect(await sideways(page)).toBeLessThanOrEqual(0);
    await nav(page).getByRole("button", { name: "Expand sidebar" }).click();
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.setViewportSize({ width: 820, height: 1180 });
    await expect(sidenav(page)).toHaveAttribute("data-rail", "false");
  });

  test("every nav control is reachable by Tab", async ({ signedIn: page }) => {
    await page.goto("/");
    await expect(nav(page)).toBeVisible();
    const reached = new Set<string>();
    for (let i = 0; i < 60; i++) {
      await page.keyboard.press("Tab");
      const name = await page.evaluate(() => {
        const el = document.activeElement;
        return el?.closest("nav[aria-label='Modules']") ? (el.getAttribute("aria-label") ?? el.textContent ?? "").trim() : null;
      });
      if (name) reached.add(name);
    }
    for (const name of ["Collapse sidebar", "Operations", "Work queue", "Security", "Settings", "Connectors"]) {
      expect(reached).toContain(name);
    }
  });
});

test.describe("tablet width", () => {
  test.beforeEach(({}, info) => test.skip(info.project.name !== "tablet", "the rail is the tablet default"));

  test("opens as a rail, expands, and never scrolls sideways", async ({ signedIn: page }) => {
    await page.goto("/work");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(sidenav(page)).toHaveAttribute("data-rail", "true");
    expect(await sideways(page)).toBeLessThanOrEqual(0);
    await nav(page).getByRole("button", { name: "Expand sidebar" }).click();
    await expect(sidenav(page)).toHaveAttribute("data-rail", "false");
    expect(await sideways(page)).toBeLessThanOrEqual(0);
  });
});

test.describe("permissions", () => {
  test.use({ role: "customer" });

  test("the rail and collapsed areas show only what the role may use", async ({ signedIn: page }) => {
    await page.goto("/");
    const labels = await navLabels(page);
    expect(labels).toEqual(["Home", "Work queue", "Resources", "Performance", "Customers", "Security posture"]);
    const toggle = nav(page).getByRole("button", { name: /(Collapse|Expand) sidebar/ });
    await toggle.click();
    expect(await navLabels(page)).toEqual(labels);
    await expect(nav(page).getByRole("link", { name: "FinOps" })).toHaveCount(0);
    await expect(nav(page).getByRole("link", { name: "Connectors" })).toHaveCount(0);
  });
});

for (const theme of ["light", "dark"] as const) {
  test.describe(`${theme} theme`, () => {
    test.use({ colorScheme: theme });

    test("passes axe (WCAG 2.1 AA) with an area collapsed and as a rail", async ({ signedIn: page }) => {
      await page.goto("/work");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await page.waitForLoadState("networkidle");
      const check = async (state: string) => {
        const results = await new AxeBuilder({ page }).include("[data-testid=sidenav]").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(results.violations.map((v) => `${state}: ${v.id} ${v.nodes.length}`)).toEqual([]);
      };
      if ((await sidenav(page).getAttribute("data-rail")) === "true") {
        await check("rail");
        await nav(page).getByRole("button", { name: "Expand sidebar" }).click();
      }
      await nav(page).getByRole("button", { name: "Security" }).click();
      await check("area collapsed");
      await nav(page).getByRole("button", { name: "Collapse sidebar" }).click();
      await nav(page).getByRole("link", { name: "Work queue" }).hover();
      await check("rail");
    });
  });
}
