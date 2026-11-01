// Settings → Commercial setup (ADR 0033, ADR 0034), end to end from entered data alone:
// a customer the journey onboards through Customer mapping, with billing as the connectors would load it,
// gets its first SOW and an amendment through the page, margin is recomputed on
// request, and the Margin page fills from those figures. The rate card gets a new
// version, and Pricing quotes on the version in force. Every figure is invented.
import type { Me, RateCardVersionView } from "@cloudops/contracts";

import { api, expect, loadBilling, navLabels, recomputeMargins, test } from "./fixtures";

/** The first day of the month `back` months before this one, as YYYY-MM-DD. */
function monthStart(back: number): string {
  const now = new Date();
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - back, 1));
  return d.toISOString().slice(0, 10);
}

function dayBefore(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

function longDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
}

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("a customer's SOW and amendment fill its margin", async ({ signedIn: page }) => {
    test.slow(); // two helper processes and a recompute
    const code = `CS${Date.now().toString(36).slice(-6).toUpperCase()}`;
    const name = `Commercial setup journey ${code}`;
    // Onboarded through Customer mapping, as Delivery onboards a new customer.
    await page.goto("/mapping");
    await page.getByRole("button", { name: "Map a name or onboard a tenant…" }).click();
    const onboard = page.getByRole("form", { name: "Map a name to a customer" });
    await onboard.getByLabel("Name the source sent").selectOption("");
    await onboard.getByLabel("Name, exactly as the source sends it").fill(`${name} Ltd`);
    await onboard.getByLabel("Onboard a new customer").check();
    await onboard.getByLabel("Canonical name").fill(name);
    await onboard.getByLabel("Short code").fill(code);
    await onboard.getByLabel("A customer", { exact: true }).check();
    await onboard.getByRole("button", { name: "Onboard and propose" }).click();
    await expect(page.getByRole("status").filter({ hasText: "onboarded, and the mapping proposed" })).toBeVisible();
    const me = await api<Me>("admin", "/v1/me");
    const tenantId = me.tenants.find((t) => t.name === name)?.id;
    if (!tenantId) throw new Error(`${name} was not onboarded`);
    loadBilling(tenantId, "150", "EUR");

    await page.goto("/settings/commercial");
    await expect(page.getByRole("heading", { level: 1, name: "Commercial setup" })).toBeVisible();
    await page.getByRole("main").getByLabel("Customer").selectOption({ label: name });
    await expect(page.getByRole("heading", { name: `SOW register: ${name}` })).toBeVisible();
    await expect(page.getByText("No SOW recorded")).toBeVisible();

    // The first SOW: 36,000 a quarter, so 12,000 a month.
    const first = monthStart(7);
    await page.getByRole("button", { name: "Record the first SOW" }).click();
    const form = page.getByRole("form", { name: "Record the first SOW" });
    await form.getByLabel("Reference").fill(`SOW-${code}-001`);
    await form.getByLabel("Effective from").fill(first);
    await form.getByLabel("Term starts").fill(first);
    await form.getByLabel("Where the agreement is recorded").fill(`Contracts library: SOW-${code}-001 (journey)`);
    await form.getByLabel("Service scope").fill("Managed Azure operations, 24x7 Gold");
    await form.getByLabel("Contract currency").fill("EUR");
    await form.getByLabel(/^Recurring fee/).fill("36000");
    await form.getByLabel("Billed").selectOption("quarterly");
    await form.getByLabel("Target margin (%)").fill("25");
    await form.getByLabel("Consumption uplift (%)").fill("6");
    await form.getByRole("button", { name: "Record SOW" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Version 1 recorded" })).toBeVisible();
    const register = page.getByRole("table", { name: "SOW versions, newest first" });
    await expect(register.getByRole("row").filter({ hasText: `SOW-${code}-001` })).toContainText("EUR 12,000.00");

    // An amendment: a new version from a later date, the first one closed the day before.
    const amended = monthStart(3);
    await page.getByRole("button", { name: "Record an amendment" }).click();
    const amend = page.getByRole("form", { name: "Record an amendment" });
    await expect(amend.getByLabel("Reference")).toHaveValue(`SOW-${code}-001`);
    await amend.getByLabel("Reference").fill(`SOW-${code}-002`);
    await amend.getByLabel("Effective from").fill(amended);
    await amend.getByLabel(/^Recurring fee/).fill("13500");
    await amend.getByLabel("Billed").selectOption("monthly");
    await expect(amend.getByLabel("Why the SOW is amended")).toHaveAttribute("required", "");
    await amend.getByLabel("Why the SOW is amended").fill("Scope extended to a second subscription (journey)");
    await amend.getByRole("button", { name: "Record amendment" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Version 2 recorded" })).toBeVisible();
    // Version 2 still names the agreement for -001: it was carried over, so match on both.
    const v1 = register.getByRole("row").filter({ hasText: `SOW-${code}-001` }).filter({ hasText: "First version" });
    const v2 = register.getByRole("row").filter({ hasText: `SOW-${code}-002` });
    await expect(v2).toContainText("EUR 13,500.00");
    await expect(v2).toContainText("Scope extended to a second subscription");
    await expect(v1).toContainText("EUR 12,000.00");
    await expect(v1).toContainText(`${longDate(first)} – ${longDate(dayBefore(amended))}`);

    // Recompute now: the worker answers it (here, the local stand-in for its schedule).
    await page.getByRole("button", { name: "Recompute margin now" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Margin recompute requested" })).toBeVisible();
    recomputeMargins();
    await page.reload();
    await page.getByRole("main").getByLabel("Customer").selectOption({ label: name });
    await expect(page.getByText(/^Recomputed \(requested/)).toBeVisible();

    // The Margin page fills from the entered figures alone.
    await page.getByRole("link", { name: "Open this customer’s margin" }).click();
    await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
    await expect(page.getByText(/Not captured/)).toHaveCount(0);
    await expect(page.getByText("EUR 13,500.00").first()).toBeVisible();
  });

  test("a new rate card version is published for a future date; Pricing quotes on the one in force", async ({ signedIn: page }, info) => {
    // Published versions are immutable and must follow the latest one: one project publishes.
    test.skip(info.project.name !== "desktop", "publishes once per run");
    const versions = await api<RateCardVersionView[]>("admin", "/v1/commercial/rate-cards");
    const latest = versions[0];
    if (!latest) throw new Error("no rate card version: run make migrate");
    const inForce = versions.find((v) => v.in_force) ?? latest;
    const next = new Date(`${latest.effective_from}T00:00:00Z`);
    next.setUTCDate(next.getUTCDate() + 1);
    const tomorrow = new Date();
    tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
    const effective = (next > tomorrow ? next : tomorrow).toISOString().slice(0, 10);
    const version = `e2e-${Date.now().toString(36)}`;

    await page.goto("/settings/commercial");
    await page.getByRole("button", { name: "Publish a new version" }).click();
    const form = page.getByRole("form", { name: "Publish a new version" });
    await form.getByLabel("Version name").fill(version);
    await form.getByLabel("Effective from").fill(effective);
    await form.getByLabel("Blended day rate (EUR)").fill("281.50");
    await form.getByLabel("India").fill("1850");
    await form.getByLabel("Full Stack Cloud Engineer").fill("0.5"); // shares now add up to 1.05
    await form.getByLabel("Why this version").fill("Annual review (journey, invented figures)");
    await form.getByRole("button", { name: "Publish version" }).click();
    await expect(form.getByRole("alert")).toHaveText("The role shares add up to exactly 1 (100%).");
    await form.getByLabel("Full Stack Cloud Engineer").fill("0.45");
    await form.getByRole("button", { name: "Publish version" }).click();
    await expect(page.getByRole("status").filter({ hasText: `Rate card ${version} published` })).toBeVisible();
    const row = page.getByRole("table", { name: "Rate card versions, newest first" }).getByRole("row").filter({ hasText: version });
    await expect(row).toContainText("EUR 281.50");
    await expect(row).toContainText("India: EUR 1,850.00");

    // Pricing is unchanged until the new version's date: it quotes on the one in force.
    await page.goto("/pricing");
    await expect(page.getByText(`Rate card ${inForce.version}`)).toBeVisible();
    await page.getByLabel("Azure Virtual Machines / Amazon EC2").fill("40");
    await page.getByRole("button", { name: "Price it" }).click();
    await expect(page.getByRole("region", { name: "Quote" })).toContainText(`Rate card ${inForce.version}`);
  });
});

test.describe("FinOps", () => {
  test.use({ role: "finops" });

  test("reads the register and the rate card, and records nothing", async ({ signedIn: page }) => {
    await page.goto("/");
    expect(await navLabels(page)).toContain("Commercial setup");
    await page.goto("/settings/commercial");
    await expect(page.getByRole("table", { name: "Rate card versions, newest first" })).toBeVisible();
    await expect(page.getByRole("region", { name: "SOW versions, newest first" })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Record/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Publish a new version" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Recompute margin now" })).toHaveCount(0);
  });
});
