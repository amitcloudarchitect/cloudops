// The SecOps workspace (HLD M13, ADR 0036): one journey per staff role that holds
// vulnerabilities.read, the refusals for a role without it and for a customer who
// holds it, and axe in both themes. The figures themselves are the database's
// (db/tests/test_api_secops.py); these check what a person sees and can reach.

import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import type { Me, SecOpsVulnerabilities } from "@cloudops/contracts";

import { api, expect, test, token } from "./fixtures";
import type { RoleName } from "./roles";

const API = process.env.CLOUDOPS_E2E_API ?? "http://127.0.0.1:8000";
const PAGES = ["/secops", "/secops/coverage", "/secops/score"] as const;

async function loaded(page: Page) {
  await expect(page.getByRole("heading", { level: 1, name: "SecOps" })).toBeVisible();
  await expect(page.getByText(/^Loading /)).toHaveCount(0);
}

/** An open finding matched to a resource, as the role sees it. */
async function linkedFinding(role: RoleName) {
  const body = await api<SecOpsVulnerabilities>(role, "/v1/secops/vulnerabilities");
  const finding = body.findings.find((f) => f.resource_id !== null);
  if (!finding) throw new Error("no open finding linked to a resource: run make seed");
  return finding;
}

async function customersOf(role: RoleName): Promise<Set<string>> {
  return new Set((await api<Me>(role, "/v1/me")).tenants.map((t) => t.name));
}

// Each staff role with vulnerabilities.read: the SecOps role across every customer,
// an engineer with two, and the platform administrator.
for (const role of ["secops", "engineer", "admin"] as const) {
  test.describe(`${role} in the SecOps workspace`, () => {
    test.use({ role });

    test("ages findings against modelled targets and drills down to the resource and its tickets", async ({ signedIn: page }) => {
      await page.goto("/secops");
      await loaded(page);
      // The Security area offers the workspace to staff.
      await expect(page.getByRole("navigation", { name: "Modules" }).getByRole("link", { name: "SecOps" })).toBeVisible();
      const ageing = page.getByRole("table", { name: /Open findings by severity/ });
      await expect(ageing.getByRole("columnheader", { name: "Breached" })).toBeVisible();
      // The targets are modelled, and the page says so in words.
      await expect(page.getByText("Modelled").first()).toBeVisible();
      await expect(page.getByText(/HLD and LLD set no remediation target/)).toBeVisible();
      // Severity 1 has no target: a dash, never zero.
      await expect(ageing.getByRole("row", { name: /1 · Minimal/ }).getByRole("cell").nth(0)).toHaveText("—");

      // Only the role's own customers appear.
      const mine = await customersOf(role);
      const names = await page.getByRole("table", { name: "Open findings by customer" }).locator("tbody th").allTextContents();
      expect(names.length).toBeGreaterThan(0);
      for (const name of names) expect(mine.has(name.trim())).toBe(true);

      // The breached card opens the breached findings.
      await page.getByRole("link", { name: /Breached target/ }).click();
      await expect(page).toHaveURL(/\/secops\?sla=breached#findings$/);
      await expect(page.getByRole("heading", { name: "Findings: breached" })).toBeVisible();
      const rows = page.getByRole("table", { name: /Open findings, most overdue first/ }).locator("tbody tr");
      await expect(rows.first()).toContainText("Breached");

      // A finding matched to a resource: its resource and linked tickets.
      const finding = await linkedFinding(role);
      await page.goto(`/secops/findings/${finding.id}`);
      await expect(page.getByRole("heading", { level: 1, name: finding.title })).toBeVisible();
      await expect(page.getByRole("heading", { name: "Resource" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "Linked tickets" })).toBeVisible();
      await page.getByRole("link", { name: /Open the resource’s context/ }).click();
      await expect(page).toHaveURL(new RegExp(`/resources/${finding.resource_id}$`));
    });

    test("shows scanner coverage and the posture score", async ({ signedIn: page }) => {
      await page.goto("/secops/coverage");
      await loaded(page);
      await expect(page.getByRole("link", { name: "Scanner coverage" })).toHaveAttribute("aria-current", "page");
      await expect(page.getByRole("heading", { name: "Resources with no Qualys host match" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "Qualys hosts with no resource match" })).toBeVisible();
      await expect(page.getByRole("table", { name: "Scanner coverage by customer" })).toBeVisible();

      await page.getByRole("link", { name: "Posture score" }).click();
      await expect(page).toHaveURL(/\/secops\/score$/);
      await loaded(page);
      await expect(page.getByRole("heading", { name: /How the score is made/ })).toContainText("Modelled");
      await expect(page.getByText(/Weighted mean/)).toBeVisible();
      const mine = await customersOf(role);
      const table = page.getByRole("table", { name: /Posture score by customer/ });
      await expect(table.locator("tbody tr")).toHaveCount(mine.size);
    });
  });
}

test.describe("the customer selector in the SecOps workspace", () => {
  test.use({ role: "secops" });

  test("narrows every SecOps page to one customer", async ({ signedIn: page }) => {
    const me = await api<Me>("secops", "/v1/me");
    const vulns = await api<SecOpsVulnerabilities>("secops", "/v1/secops/vulnerabilities");
    const chosen = vulns.by_customer.find((c) => Number(c.open.value) > 0);
    if (!chosen) throw new Error("no customer with open findings: run make seed");
    expect(me.tenants.length).toBeGreaterThan(1);

    await page.goto("/secops");
    await loaded(page);
    await page.getByTestId("customer-select").selectOption({ label: chosen.tenant.name });
    const customers = page.getByRole("table", { name: "Open findings by customer" }).locator("tbody th");
    await expect(customers).toHaveText([chosen.tenant.name]);
    for (const [path, table] of [
      ["/secops/coverage", "Scanner coverage by customer"],
      ["/secops/score", /Posture score by customer/],
    ] as const) {
      await page.goto(path);
      await loaded(page);
      await expect(page.getByRole("table", { name: table }).locator("tbody tr")).toHaveCount(1);
      await expect(page.getByRole("table", { name: table }).locator("tbody tr")).toContainText(chosen.tenant.name);
    }
    await page.getByTestId("customer-select").selectOption("");
  });
});

test.describe("finops, without vulnerabilities.read", () => {
  test.use({ role: "finops" });

  test("is not offered the workspace", async ({ signedIn: page }) => {
    for (const path of PAGES) {
      await page.goto(path);
      await expect(page.getByText("Not available to your role")).toBeVisible();
    }
    const response = await fetch(`${API}/v1/secops/vulnerabilities`, { headers: { Authorization: `Bearer ${token("finops")}` } });
    expect(response.status).toBe(403);
  });
});

test.describe("a customer's security officer", () => {
  test.use({ role: "security" });

  test("cannot reach the staff workspace, and keeps its own posture view", async ({ signedIn: page }) => {
    const finding = await linkedFinding("secops");
    await page.goto("/posture");
    await expect(page.getByRole("navigation", { name: "Modules" }).getByRole("link", { name: "SecOps" })).toHaveCount(0);
    for (const path of [...PAGES, `/secops/findings/${finding.id}`]) {
      await page.goto(path);
      await expect(page.getByText("Not available to your role")).toBeVisible();
    }
    for (const path of ["/v1/secops/vulnerabilities", "/v1/secops/coverage", "/v1/secops/posture-score", `/v1/secops/findings/${finding.id}`]) {
      const response = await fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${token("security")}` } });
      expect(response.status, path).toBe(403);
    }
    await page.goto("/posture");
    await expect(page.getByRole("heading", { level: 1, name: "Security posture" })).toBeVisible();
  });
});

// WCAG 2.1 AA on every SecOps page, in both themes, and no sideways scroll at tablet width.
for (const theme of ["light", "dark"] as const) {
  test.describe(`SecOps pages, ${theme} theme`, () => {
    test.use({ role: "secops", colorScheme: theme });

    test("pass axe (WCAG 2.1 AA) and fit tablet width", async ({ signedIn: page }) => {
      test.setTimeout(120_000); // four pages, each analysed in full
      const finding = await linkedFinding("secops");
      for (const path of [...PAGES, `/secops/findings/${finding.id}`]) {
        await page.goto(path);
        await expect(page.getByText(/^Loading /)).toHaveCount(0);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
        expect(results.violations.map((v) => `${path}: ${v.id} ${v.nodes.length}`)).toEqual([]);
        await page.setViewportSize({ width: 768, height: 1024 });
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow, path).toBeLessThanOrEqual(0);
        await page.setViewportSize({ width: 1280, height: 720 });
      }
    });
  });
}
