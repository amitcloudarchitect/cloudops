// Connector health (step 15) as a platform administrator (reads and acknowledges
// drift), an engineer (reads only), and roles without connectors.read. The seed
// writes a run log with every band (cloudops_devdata.ingest_health); journeys
// assert only on bands that cannot change as the seeded picture ages.
import { expect, navLabels, raiseDrift, test } from "./fixtures";

function unique(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

function row(page: import("@playwright/test").Page, table: string, text: string) {
  return page.getByRole("table", { name: table }).getByRole("row").filter({ hasText: text });
}

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("sees every connector banded, worst first, with the reasons", async ({ signedIn: page }) => {
    await page.goto("/connectors");
    expect(await navLabels(page)).toContain("Connectors");
    await expect(page.getByRole("heading", { level: 1, name: "Connectors" })).toBeVisible();
    const table = page.getByRole("table", { name: "Connector health" });
    await expect(table).toBeVisible();
    await expect(page.getByRole("columnheader", { name: /Health/ })).toHaveAttribute("aria-sort", "ascending");

    // Four days without a success on a daily schedule: an outage, and a dead letter.
    const qualys = row(page, "Connector health", "host detection");
    await expect(qualys.locator("[data-band]").first()).toHaveAttribute("data-band", "critical");
    await expect(qualys).toContainText("Outage");
    await expect(qualys).toContainText("dead letters wait for a replay");
    // Scheduled, never run: unknown, never healthy.
    const entra = row(page, "Connector health", "role assignment");
    await expect(entra).toContainText("Never run");
    await expect(entra.locator("[data-band]").first()).toHaveAttribute("data-band", "unknown");
    // Worst first: the first data row is an outage.
    await expect(table.locator("tbody tr").first().locator("[data-band]").first()).toHaveAttribute("data-band", "critical");

    await expect(page.getByText("Outages", { exact: true })).toBeVisible();
    await page.getByText("How health is decided").click();
    await expect(page.getByText("more than 3 intervals", { exact: false })).toBeVisible();
  });

  test("acknowledges drift, and it leaves the list", async ({ signedIn: page }) => {
    const field = unique("e2eField");
    raiseDrift("aws", "config", field);
    await page.goto("/connectors");
    const drift = row(page, "Schema drift", field);
    await expect(drift).toContainText("New field");
    await drift.getByRole("button", { name: `Acknowledge ${field} on aws · config` }).click();
    await expect(page.getByRole("status").filter({ hasText: `Drift on ${field} on aws · config acknowledged.` })).toBeVisible();
    await expect(row(page, "Schema drift", field)).toHaveCount(0);
  });

  test("the alias quarantine links to customer mapping", async ({ signedIn: page }) => {
    await page.goto("/connectors");
    const servicenow = row(page, "Connector health", "servicenow").first();
    const link = servicenow.getByRole("link", { name: /names held from servicenow: map them/ });
    await expect(link).toHaveAttribute("href", "/mapping");
  });
});

test.describe("engineer", () => {
  test.use({ role: "engineer" });

  test("reads health and drift, with no acknowledge action", async ({ signedIn: page }) => {
    await page.goto("/");
    expect(await navLabels(page)).toContain("Connectors");
    await page.goto("/connectors");
    await expect(page.getByRole("table", { name: "Connector health" })).toBeVisible();
    const drift = page.getByRole("table", { name: "Schema drift" });
    await expect(drift).toBeVisible();
    await expect(drift.getByRole("row").filter({ hasText: "u_major_incident" })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Acknowledge/ })).toHaveCount(0);
    await expect(drift.getByRole("columnheader", { name: "Action" })).toHaveCount(0);
    await expect(page.getByText("A platform administrator acknowledges drift")).toBeVisible();
    // No mapping.manage: the held names are a count, not a link.
    await expect(page.getByRole("link", { name: /names held from .*: map them/ })).toHaveCount(0);
  });
});

for (const role of ["customer", "finops"] as const) {
  test.describe(`${role}, without connectors.read`, () => {
    test.use({ role });

    test("has no link to connectors, and the page refuses a typed-in URL", async ({ signedIn: page }) => {
      await page.goto("/");
      expect(await navLabels(page)).not.toContain("Connectors");
      await page.goto("/connectors");
      await expect(page.getByText("Not available to your role")).toBeVisible();
      await expect(page.getByRole("table", { name: "Connector health" })).toHaveCount(0);
    });
  });
}
