import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import type { SizeBandSettings } from "@cloudops/contracts";

import { api, apiPost, expect, test } from "./fixtures";
import type { RoleName } from "./roles";

// The home sections (ADR 0038, amending ADR 0030): each role sees only the sections
// it may, its landing view's first; each section has a date range and a compare
// mode; the customer selector narrows every section; modelled figures say so.

async function sections(page: Page): Promise<string[]> {
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  const found = page.locator("section[data-section]");
  await expect(found.first()).toBeVisible();
  return (await found.locator("h2").allTextContents()).map((s) => s.trim());
}

async function settled(page: Page) {
  // Every section settles: data, a named gap, or a refusal, never a spinner left behind.
  await expect(page.getByText(/^Loading /)).toHaveCount(0, { timeout: 15_000 });
  await expect(page.getByText(/^Could not load/)).toHaveCount(0);
}

const LAYOUTS: { role: RoleName; sections: string[] }[] = [
  // No cost.read: no revenue section.
  { role: "engineer", sections: ["Operations", "Security & compliance", "Platform", "Top 10 customers"] },
  // No tickets.read or vulnerabilities.read: no operations, security or top 10.
  { role: "finops", sections: ["Revenue & margin", "Platform"] },
  { role: "admin", sections: ["Operations", "Revenue & margin", "Security & compliance", "Platform", "Top 10 customers"] },
  // A customer: its own service, never the platform or other customers.
  { role: "customer", sections: ["Operations", "Your cloud consumption", "Security & compliance"] },
  { role: "cxo", sections: ["Operations", "Your cloud consumption", "Security & compliance"] },
  // The security officer's landing view is the posture: its section leads.
  { role: "security", sections: ["Security & compliance"] },
];

for (const layout of LAYOUTS) {
  test.describe(`${layout.role} home`, () => {
    test.use({ role: layout.role });

    test("shows its role's sections, in order, and each settles", async ({ signedIn: page }) => {
      await page.goto("/");
      expect(await sections(page)).toEqual(layout.sections);
      await settled(page);
    });
  });
}

test.describe("operations", () => {
  test.use({ role: "engineer" });

  test("says auto-remediation is not yet available, and marks SLA attainment as modelled", async ({ signedIn: page }) => {
    await page.goto("/");
    const ops = page.locator('section[data-section="operations"]');
    await settled(page);
    await expect(ops.getByTestId("remediation-note")).toContainText("Auto-remediation: not yet available.");
    await expect(ops.getByRole("heading", { level: 3, name: "Auto-remediated" })).toBeVisible();
    await expect(ops.getByRole("table", { name: "Work received by kind", exact: true })).toBeVisible();
    await expect(ops.getByRole("table", { name: /Incidents resolved within their priority's modelled target/ })).toBeVisible();
    await expect(ops.getByText("Modelled", { exact: true }).first()).toBeVisible();
  });

  test("compares two ranges side by side, by component", async ({ signedIn: page }) => {
    await page.goto("/");
    const ops = page.locator('section[data-section="operations"]');
    await settled(page);
    await ops.getByLabel("Range", { exact: true }).selectOption("last_month");
    await ops.getByLabel("Compare with another range").check();
    const table = ops.getByRole("table", { name: "Work received by kind", exact: true });
    await expect(table.getByRole("columnheader", { name: "Change" })).toBeVisible();
    // Last month against the month before it: both named as whole months.
    await expect(table.getByRole("columnheader")).toHaveCount(4);
    await expect(table.getByRole("rowheader", { name: "Incidents" })).toBeVisible();
    await settled(page);
  });

  test("refuses a range that ends before it starts, without asking the API", async ({ signedIn: page }) => {
    await page.goto("/");
    const ops = page.locator('section[data-section="operations"]');
    await ops.getByLabel("From", { exact: true }).fill("2026-10-20");
    await ops.getByLabel("To", { exact: true }).fill("2026-10-01");
    await expect(ops.getByRole("alert")).toHaveText("The range ends before it starts.");
  });
});

test.describe("revenue and margin", () => {
  test.use({ role: "finops" });

  test("names each component and says it has no data source yet, never a figure of its own", async ({ signedIn: page }) => {
    await page.goto("/");
    const revenue = page.locator('section[data-section="revenue"]');
    await settled(page);
    const table = revenue.getByRole("table", { name: /Revenue and margin by component/ });
    for (const component of ["Hosting cost (Azure and AWS consumption)", "Managed services revenue", "Margin", "New project price (closed project tasks)"]) {
      const row = table.getByRole("row").filter({ has: page.getByRole("rowheader", { name: component, exact: true }) });
      await expect(row).toContainText("No data source yet");
    }
    await expect(revenue.getByText(/^EUR /)).toHaveCount(0);
  });
});

test.describe("a customer's revenue section", () => {
  test.use({ role: "cxo" });

  test("shows its consumption price, never Getronics' cost or margin", async ({ signedIn: page }) => {
    await page.goto("/");
    await settled(page);
    const revenue = page.locator('section[data-section="revenue"]');
    await expect(revenue.getByRole("heading", { level: 2, name: "Your cloud consumption" })).toBeVisible();
    for (const text of ["Hosting cost", "Managed services", "Margin", "Top 10 customers", "Platform"]) {
      await expect(page.getByRole("heading", { name: text })).toHaveCount(0);
    }
    await expect(page.getByText(/Routed to a person|Auto-triaged/)).toHaveCount(0);
  });
});

test.describe("platform and size bands", () => {
  test.use({ role: "admin" });

  test("bands each customer, and saves new thresholds as a new version", async ({ signedIn: page }) => {
    await page.goto("/");
    const platform = page.locator('section[data-section="platform"]');
    await settled(page);
    await expect(platform.getByRole("table", { name: /Customers onboarded by the end of each range/ })).toBeVisible();
    await expect(platform.getByText(/The higher of two bands/)).toBeVisible();
    await platform.getByRole("link", { name: "change them in Settings" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Size bands" })).toBeVisible();
    const before = await api<SizeBandSettings>("admin", "/v1/settings/size-bands");
    // The same thresholds again: a new version, without moving anyone's band.
    const reason = `Journey: confirm the thresholds ${Date.now()}`;
    await page.getByLabel("Reason").fill(reason);
    await page.getByRole("button", { name: "Save as a new version" }).click();
    const next = Number(before.current.version.value) + 1;
    await expect(page.getByRole("status")).toHaveText(`Version ${next} is in force from now.`);
    await expect(page.getByRole("table", { name: /Every version of the size band thresholds/ }).getByRole("row").filter({ hasText: reason })).toBeVisible();
  });
});

test.describe("size bands without the permission", () => {
  test.use({ role: "engineer" });

  test("reads the thresholds but cannot set them; the API refuses too", async ({ signedIn: page }) => {
    await page.goto("/settings/size-bands");
    await expect(page.getByRole("heading", { level: 2, name: /In force: version/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Save as a new version" })).toHaveCount(0);
    const refused = await apiPost("engineer", "/v1/settings/size-bands", {
      spend_currency: "EUR", spend_medium_from: "1", spend_large_above: "2",
      resources_medium_from: 1, resources_large_above: 2, reason: "Not mine to set",
    });
    expect(refused.status).toBe(403);
  });
});

test.describe("top 10 customers", () => {
  test.use({ role: "admin" });

  test("ranks by modelled health, says revenue ranking has no source yet, and narrows every section to a chosen customer", async ({ signedIn: page }) => {
    await page.goto("/");
    const top = page.locator('section[data-section="top"]');
    await settled(page);
    const table = top.getByTestId("top-customers");
    await expect(table).toBeVisible();
    await expect(top.getByText(/Health is modelled/)).toBeVisible();
    await expect(top.getByTestId("top-revenue-pending")).toContainText("no data source yet");
    const first = table.getByRole("rowheader").first().getByRole("button");
    const name = ((await first.textContent()) ?? "").replace(": show this customer on every section", "").trim();
    await first.click();
    const chosen = () => page.getByTestId("customer-select").evaluate((el) => (el as HTMLSelectElement).selectedOptions[0]?.text ?? "");
    await expect.poll(chosen).toBe(name);
    await expect(page.getByText(`${name} only.`)).toBeVisible();
    await settled(page);
  });
});

test.describe("an engineer's top 10", () => {
  test.use({ role: "engineer" });

  test("ranks by health, with nothing about revenue: it is commercial", async ({ signedIn: page }) => {
    await page.goto("/");
    const top = page.locator('section[data-section="top"]');
    await settled(page);
    await expect(top.getByTestId("top-customers")).toBeVisible();
    await expect(top.getByTestId("top-revenue-pending")).toHaveCount(0);
    await expect(top.getByText(/revenue/i)).toHaveCount(0);
  });
});

// WCAG 2.1 AA in both themes, with compare on, and no sideways scroll at tablet
// width, for a staff and a customer dashboard.
for (const role of ["admin", "finops", "engineer", "cxo"] as const) {
  for (const theme of ["light", "dark"] as const) {
    test.describe(`${role} home, ${theme} theme`, () => {
      test.use({ role, colorScheme: theme });

      test("passes axe (WCAG 2.1 AA) and fits tablet width", async ({ signedIn: page }) => {
        await page.goto("/");
        await sections(page);
        await settled(page);
        await page.locator("section[data-section]").first().getByLabel("Compare with another range").check();
        await settled(page);
        const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(results.violations.map((v) => `/: ${v.id} ${v.nodes.length}`)).toEqual([]);
        await page.setViewportSize({ width: 768, height: 1024 });
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow).toBeLessThanOrEqual(0);
      });
    });
  }
}

test.describe("keyboard", () => {
  test.use({ role: "engineer" });

  test("reaches a section's evidence link by keyboard alone", async ({ signedIn: page }) => {
    await page.goto("/");
    await sections(page);
    const target = page.getByRole("link", { name: "View the evidence for Operations" });
    for (let i = 0; i < 60; i += 1) {
      await page.keyboard.press("Tab");
      if (await target.evaluate((el) => el === document.activeElement)) break;
    }
    await expect(target).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/work$/);
  });
});
