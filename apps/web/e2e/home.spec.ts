import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";
import type { RoleName } from "./roles";

// The home dashboards (ADR 0030): each role lands on its own widgets, in its
// default order, sees only what it may, can follow each widget to its evidence, and
// can hide and reorder widgets in this browser.

async function widgets(page: Page): Promise<string[]> {
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  const sections = page.locator("section[data-widget]");
  await expect(sections.first()).toBeVisible();
  return (await sections.locator("h2").allTextContents()).map((s) => s.trim());
}

const COMMERCIAL_WIDGETS = ["Margin by customer", "Cloud cost by customer", "Cost reconciliation", "Platform cost", "AI gateway usage"];

const LAYOUTS: { role: RoleName; widgets: string[] }[] = [
  {
    role: "engineer",
    widgets: [
      "Open incidents and SLA risk",
      "Closest to breach",
      "Triage awaiting review",
      "Changes this week",
      "Noisy configuration items",
      "Vulnerabilities",
      "Connector health",
    ],
  },
  {
    // A delivery lead without commercial visibility: the margin widget is not there.
    role: "customerMaster",
    widgets: [
      "SLA attainment",
      "Open incidents and SLA risk",
      "Backlog and MTTR by customer",
      "Backlog and MTTR by workgroup",
      "Changes this week",
      "Connector health",
      "Customer satisfaction",
      "Alias and mapping queue",
    ],
  },
  {
    role: "finops",
    widgets: ["Cloud cost by customer", "Cost anomalies", "Cost reconciliation", "Platform cost", "Margin by customer", "AI gateway usage"],
  },
  {
    role: "admin",
    widgets: [
      "Connector health",
      "Alias and mapping queue",
      "Portal onboarding",
      "AI gateway usage",
      "Platform cost",
      "Triage awaiting review",
      "Open incidents and SLA risk",
      "Changes this week",
    ],
  },
  {
    role: "customer",
    widgets: ["Your service", "Open incidents and SLA risk", "Closest to breach", "SLA attainment", "Cloud consumption", "Vulnerabilities", "Availability"],
  },
  {
    role: "cxo",
    widgets: [
      "Your service",
      "Open incidents and SLA risk",
      "Closest to breach",
      "SLA attainment",
      "Cloud consumption",
      "Vulnerabilities",
      "Availability",
      "Latest report pack",
    ],
  },
  { role: "security", widgets: ["Vulnerabilities", "Your service", "Availability"] },
];

for (const layout of LAYOUTS) {
  test.describe(`${layout.role} home`, () => {
    test.use({ role: layout.role });

    test("lands on its dashboard with its role's widgets, in order", async ({ signedIn: page }) => {
      await page.goto("/");
      expect(await widgets(page)).toEqual(layout.widgets);
      // Every widget settles: data, a named gap, or a refusal, never a spinner left behind.
      await expect(page.getByText(/^Loading /)).toHaveCount(0);
      await expect(page.getByText(/^Could not load/)).toHaveCount(0);
    });
  });
}

test.describe("engineer", () => {
  test.use({ role: "engineer" });

  test("sees SLA risk marked as modelled, and follows triage to its evidence", async ({ signedIn: page }) => {
    await page.goto("/");
    const incidents = page.locator('section[data-widget="incidents"]');
    await expect(incidents.getByText("Modelled", { exact: true }).first()).toBeVisible();
    await expect(incidents.getByText(/at risk is an open incident that ServiceNow has not flagged/)).toBeVisible();
    await expect(incidents.getByRole("table", { name: /Open incidents by priority/ })).toBeVisible();
    for (const title of COMMERCIAL_WIDGETS) await expect(page.getByRole("heading", { name: title })).toHaveCount(0);
    await page.getByRole("link", { name: "View the evidence for Triage awaiting review" }).click();
    await expect(page).toHaveURL(/\/work$/);
  });
});

test.describe("finops", () => {
  test.use({ role: "finops" });

  test("sees cost by customer in its own currencies and reconciliation states", async ({ signedIn: page }) => {
    await page.goto("/");
    const cost = page.locator('section[data-widget="cost_trend"]');
    await expect(cost.getByRole("table", { name: /Cloud cost by customer and currency/ })).toBeVisible();
    await expect(cost.getByText("Getronics' amortised cost", { exact: false })).toBeVisible();
    const recon = page.locator('section[data-widget="reconciliation"]');
    await expect(recon.getByRole("table")).toBeVisible();
    await expect(recon.getByText(/Provisional|Final|Not checked|Unverified/).first()).toBeVisible();
  });
});

test.describe("customer", () => {
  test.use({ role: "customer" });

  test("sees its own service and price, never Getronics' cost or triage", async ({ signedIn: page }) => {
    await page.goto("/");
    await expect(page.locator('section[data-widget="availability"]').getByText(/Not captured/)).toBeVisible();
    await expect(page.locator('section[data-widget="price"]').getByText(/Closed calendar months only/)).toBeVisible();
    for (const title of [...COMMERCIAL_WIDGETS, "Triage awaiting review", "Backlog and MTTR by workgroup"]) {
      await expect(page.getByRole("heading", { name: title })).toHaveCount(0);
    }
    await expect(page.getByText(/Getronics' (amortised )?cost/)).toHaveCount(0);
  });
});

test.describe("arranging widgets", () => {
  test.use({ role: "engineer" });

  test("hides, moves and resets, kept in this browser across a reload", async ({ signedIn: page }) => {
    await page.goto("/");
    const before = await widgets(page);
    await page.getByRole("button", { name: "Arrange widgets" }).click();
    await page.getByRole("button", { name: "Hide Noisy configuration items" }).click();
    await page.getByRole("button", { name: "Move up Closest to breach" }).click();
    await page.getByRole("button", { name: "Done" }).click();
    const arranged = ["Closest to breach", "Open incidents and SLA risk", ...before.slice(2).filter((t) => t !== "Noisy configuration items")];
    expect(await widgets(page)).toEqual(arranged);
    await page.reload();
    expect(await widgets(page)).toEqual(arranged);
    await page.getByRole("button", { name: "Arrange widgets" }).click();
    await page.getByRole("button", { name: "Show Noisy configuration items" }).click();
    await page.getByRole("button", { name: "Reset to default" }).click();
    await page.getByRole("button", { name: "Done" }).click();
    expect(await widgets(page)).toEqual(before);
  });
});

// WCAG 2.1 AA in both themes, keyboard focus order through the widgets, and no
// sideways scroll at tablet width, for a staff and a customer dashboard.
for (const role of ["admin", "finops", "engineer", "cxo"] as const) {
  for (const theme of ["light", "dark"] as const) {
    test.describe(`${role} home, ${theme} theme`, () => {
      test.use({ role, colorScheme: theme });

      test("passes axe (WCAG 2.1 AA) and fits tablet width", async ({ signedIn: page }) => {
        await page.goto("/");
        await widgets(page);
        await expect(page.getByText(/^Loading /)).toHaveCount(0);
        await page.getByRole("button", { name: "Arrange widgets" }).click();
        const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(results.violations.map((v) => `/: ${v.id} ${v.nodes.length}`)).toEqual([]);
        await page.getByRole("button", { name: "Done" }).click();
        await page.setViewportSize({ width: 768, height: 1024 });
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow).toBeLessThanOrEqual(0);
      });
    });
  }
}

test.describe("keyboard", () => {
  test.use({ role: "engineer" });

  test("reaches a widget's evidence link by keyboard alone", async ({ signedIn: page }) => {
    await page.goto("/");
    await widgets(page);
    const target = page.getByRole("link", { name: "View the evidence for Open incidents and SLA risk" });
    for (let i = 0; i < 60; i += 1) {
      await page.keyboard.press("Tab");
      if (await target.evaluate((el) => el === document.activeElement)) break;
    }
    await expect(target).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/work\?kind=Incident$/);
  });
});
