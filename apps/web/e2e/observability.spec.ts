// Platform observability (migration 0032) as a platform administrator, who holds
// platform.observe, and as roles without it: an engineer and a customer. The seed's
// figures move as the local stack is used, so journeys assert on structure,
// provenance and refusals, never on a count.
import AxeBuilder from "@axe-core/playwright";

import type { Me } from "@cloudops/contracts";

import { api, expect, navLabels, test } from "./fixtures";

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("sees every section, with its provenance, and the monitoring links", async ({ signedIn: page }) => {
    await page.goto("/");
    await page.getByRole("navigation", { name: "Modules" }).getByRole("link", { name: "Observability" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Observability" })).toBeVisible();
    for (const name of ["Usage", "AI gateway", "Triage throughput", "Connectors", "Platform cost"]) {
      await expect(page.getByRole("heading", { level: 2, name })).toBeVisible();
    }
    // Stack 03-observability is not applied locally (nor in dev yet): no link is invented.
    const monitoring = page.getByRole("list", { name: "Platform monitoring" });
    await expect(monitoring.getByText("Not yet applied")).toHaveCount(2);
    await expect(monitoring.getByRole("link")).toHaveCount(0);

    // Usage: from the action log, sessions marked modelled in words.
    await expect(page.getByRole("table", { name: "Active users by role, last 30 days" })).toBeVisible();
    await expect(page.getByText("Action log").first()).toBeVisible();
    const sessions = page.getByRole("heading", { level: 3, name: "Sessions (console page loads)" }).locator("xpath=ancestor::div[contains(@class, 'rounded-card')][1]");
    await expect(sessions.getByText("Modelled")).toBeVisible();

    // AI: one row per agent, including the assistant.
    const agents = page.getByRole("table", { name: "Tokens by agent, last 30 days" });
    for (const agent of ["Classifier", "Enricher", "Drafter (reports and controls)", "AMARA assistant"]) {
      await expect(agents.getByRole("rowheader", { name: agent })).toBeVisible();
    }
    await expect(page.getByRole("table", { name: /AI usage by customer/ }).getByRole("columnheader", { name: /AMARA sub-budget/ })).toBeVisible();

    // Triage and connectors; the connector detail stays on its own page.
    await expect(page.getByRole("table", { name: "Triage by customer" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Connector health by source" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Connectors page" })).toHaveAttribute("href", "/connectors");
  });

  test("the window narrows every dated section", async ({ signedIn: page }) => {
    await page.goto("/observability");
    await expect(page.getByRole("table", { name: "Active users by role, last 30 days" })).toBeVisible();
    await page.getByRole("combobox", { name: "Window" }).selectOption("7");
    await expect(page.getByRole("table", { name: "Active users by role, last 7 days" })).toBeVisible();
    for (const summary of await page.getByText("Daily figures").all()) await summary.click();
    const daily = page.getByRole("region", { name: "Active users by day" });
    await expect(daily.getByText("Showing 7 of 7")).toBeVisible();
    await expect(page.getByRole("region", { name: "AI gateway calls by day" }).getByText("Showing 7 of 7")).toBeVisible();
  });

  test("the customer selector narrows the per-customer sections to one customer", async ({ signedIn: page }) => {
    const me = await api<Me>("admin", "/v1/me");
    const [first] = me.tenants;
    await page.goto("/observability");
    const byCustomer = page.getByRole("region", { name: /AI usage by customer/ });
    await expect(byCustomer.getByText(`Showing ${me.tenants.length} of ${me.tenants.length}`)).toBeVisible();
    await page.getByTestId("customer-select").selectOption(first!.id);
    await expect(byCustomer.getByText("Showing 1 of 1")).toBeVisible();
    await expect(byCustomer.getByRole("cell", { name: first!.name })).toBeVisible();
    await expect(page.getByRole("region", { name: "Triage by customer" }).getByText("Showing 1 of 1")).toBeVisible();
    await page.getByTestId("customer-select").selectOption("");
    await expect(byCustomer.getByText(`Showing ${me.tenants.length} of ${me.tenants.length}`)).toBeVisible();
  });
});

for (const role of ["engineer", "cxo"] as const) {
  test.describe(`${role}, without platform.observe`, () => {
    test.use({ role });

    test("is told the screen is not available", async ({ signedIn: page }) => {
      await page.goto("/");
      expect(await navLabels(page)).not.toContain("Observability");
      await page.goto("/observability");
      await expect(page.getByText("Not available to your role")).toBeVisible();
      await expect(page.getByRole("heading", { level: 2, name: "Usage" })).toHaveCount(0);
    });
  });
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`observability, ${theme} theme`, () => {
    test.use({ role: "admin", colorScheme: theme });

    test("passes axe (WCAG 2.1 AA) and does not scroll sideways at tablet width", async ({ signedIn: page }) => {
      await page.goto("/observability");
      await expect(page.getByRole("table", { name: "Connector health by source" })).toBeVisible();
      await expect(page.getByRole("table", { name: "Triage by customer" })).toBeVisible();
      await page.waitForLoadState("networkidle");
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.map((v) => `/observability: ${v.id} ${v.nodes.length}`)).toEqual([]);
      await page.setViewportSize({ width: 768, height: 1024 });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    });
  });
}
