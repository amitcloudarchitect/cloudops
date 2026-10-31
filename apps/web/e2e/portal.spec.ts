// Step 18: the customer portal, one journey per customer role (ADR 0018). The same
// console through a customer role: each role opens on its own view, sees only its
// modules, and never a Getronics-only one. The data boundary itself is the
// database's (db/tests/test_customer_portal.py); these check what a person sees.

import type { Me, WorkItemList } from "@cloudops/contracts";

import { api, expect, navLabels, test, token } from "./fixtures";
import type { RoleName } from "./roles";

const API = process.env.CLOUDOPS_E2E_API ?? "http://127.0.0.1:8000";
const STAFF_ONLY = ["Customer mapping", "Portal onboarding", "Connectors"];

async function post<T>(role: RoleName, path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token(role)}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return (await response.json()) as T;
}

function uuid(): string {
  return crypto.randomUUID();
}

interface Journey {
  role: RoleName;
  landing: RegExp;
  heading: RegExp | string;
  nav: string[];
  roleName: string;
}

const JOURNEYS: Journey[] = [
  { role: "cxo", landing: /\/customers$/, heading: /Northwind/, nav: ["Work queue", "Performance", "Security posture", "Resources", "Customers", "Users"], roleName: "CXO" },
  { role: "vp", landing: /\/performance$/, heading: "Performance", nav: ["Work queue", "Performance", "Resources", "Customers"], roleName: "VP or director" },
  { role: "architect", landing: /\/resources$/, heading: "Resources", nav: ["Resources", "Customers"], roleName: "Architect" },
  { role: "customerEngineer", landing: /\/work$/, heading: "Work queue", nav: ["Work queue", "Performance", "Resources", "Customers"], roleName: "Customer engineer" },
  { role: "security", landing: /\/posture$/, heading: "Security posture", nav: ["Security posture", "Resources", "Customers"], roleName: "Security officer" },
  { role: "approver", landing: /\/approvals$/, heading: "Approvals", nav: ["Approvals", "Resources", "Customers"], roleName: "Approver" },
];

for (const journey of JOURNEYS) {
  test.describe(`${journey.roleName}`, () => {
    test.use({ role: journey.role });

    test("lands on its own view, with its modules and no Getronics-only ones", async ({ signedIn: page }) => {
      await page.goto("/");
      await expect(page).toHaveURL(journey.landing);
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(journey.heading);
      const labels = await navLabels(page);
      expect(labels).toEqual(journey.nav);
      for (const hidden of STAFF_ONLY) expect(labels).not.toContain(hidden);
      await expect(page.getByTestId("role")).toHaveText(journey.roleName);
    });

    test("a typed-in Getronics-only module says it is not available", async ({ signedIn: page }) => {
      for (const path of ["/onboarding", "/mapping", "/connectors"]) {
        await page.goto(path);
        await expect(page.getByText("Not available to your role")).toBeVisible();
        await expect(page.getByRole("table")).toHaveCount(0);
      }
    });
  });
}

test.describe("CXO", () => {
  test.use({ role: "cxo" });

  test("sees one customer's figures and nothing commercial", async ({ signedIn: page }) => {
    await page.goto("/customers");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(/Northwind/);
    // The price Getronics charges, never Getronics' cost (ADR 0021).
    await expect(page.getByText(/Cloud consumption, 30 days \([A-Z]{3}\)/).first()).toBeVisible();
    await expect(page.getByText(/Cloud cost/)).toHaveCount(0);
    await expect(page.getByText(/Not yet priced/)).toHaveCount(0); // Northwind's SOW has an uplift
    await expect(page.getByText(/margin|rate card|revenue|SOW/i)).toHaveCount(0);
  });

  test("administers its organisation's users: adds, changes and removes one", async ({ signedIn: page }) => {
    const oid = uuid();
    const name = `Portal user ${oid.slice(0, 6)}`;
    await page.goto("/users");
    const table = page.getByRole("table", { name: "Your organisation’s users" });
    await expect(table).toBeVisible();
    await expect(table.getByText("customer-nwl-vp")).toBeVisible();
    await expect(table.getByText("engineer-nwl-crl")).toHaveCount(0);

    await page.getByRole("button", { name: "Add a user" }).click();
    const form = page.getByRole("form", { name: "Add a user" });
    await form.getByLabel("Object id").fill(oid);
    await form.getByLabel("Name", { exact: true }).fill(name);
    await form.getByLabel("Role").selectOption("Security officer");
    await form.getByRole("button", { name: "Add user" }).click();
    await expect(page.getByRole("status").filter({ hasText: `${name} added.` })).toBeVisible();
    await expect(table.getByRole("row").filter({ hasText: name })).toContainText("Security officer");

    await page.getByRole("button", { name: `Change ${name}` }).click();
    const change = page.getByRole("form", { name: `Change ${name}` });
    await change.getByLabel("Role").selectOption("Approver");
    await change.getByRole("button", { name: "Save" }).click();
    await expect(table.getByRole("row").filter({ hasText: name })).toContainText("Approver");

    page.once("dialog", (dialog) => void dialog.accept());
    await page.getByRole("button", { name: `Remove ${name}` }).click();
    await expect(page.getByRole("status").filter({ hasText: `${name} no longer has access.` })).toBeVisible();
    await expect(table.getByRole("row").filter({ hasText: name })).toContainText("Removed");
  });

  test("cannot take over a Getronics user by object id", async ({ signedIn: page }) => {
    const engineer = await api<Me>("engineer", "/v1/me");
    await page.goto("/users");
    await page.getByRole("button", { name: "Add a user" }).click();
    const form = page.getByRole("form", { name: "Add a user" });
    await form.getByLabel("Object id").fill(engineer.oid);
    await form.getByLabel("Name", { exact: true }).fill("Not mine");
    await form.getByLabel("Role").selectOption("CXO");
    await form.getByRole("button", { name: "Add user" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "no such user in your organisation" })).toBeVisible();
  });
});

test.describe("VP or director", () => {
  test.use({ role: "vp" });

  test("reads six months of service performance", async ({ signedIn: page }) => {
    await page.goto("/performance");
    const table = page.getByRole("table", { name: "Monthly performance" });
    await expect(table.getByRole("row")).toHaveCount(7);
    await expect(page.getByRole("link", { name: /^Open incidents.*View the evidence$/ })).toBeVisible();
  });
});

test.describe("Customer engineer", () => {
  test.use({ role: "customerEngineer" });

  test("works its own queue without Getronics workgroups or another customer's tickets", async ({ signedIn: page }) => {
    await page.goto("/work");
    const table = page.getByRole("table", { name: "Work items" });
    await expect(table).toBeVisible();
    await expect(table.getByRole("columnheader", { name: /Workgroup/ })).toHaveCount(0);
    await expect(table.getByRole("columnheader", { name: /Triage/ })).toHaveCount(0);
    const theirs = await api<WorkItemList>("engineer", "/v1/work-items?status=all");
    const foreign = theirs.items.find((w) => !w.tenant.name.startsWith("Northwind"));
    expect(foreign, "the engineer sees a second customer").toBeTruthy();
    await page.goto(`/work/${foreign!.number}`);
    await expect(page.getByText("No such ticket")).toBeVisible();
  });
});

test.describe("Security officer", () => {
  test.use({ role: "security" });

  test("reads open findings by severity and age", async ({ signedIn: page }) => {
    await page.goto("/posture");
    await expect(page.getByText("Open findings").first()).toBeVisible();
    await expect(page.getByText("Open over 90 days")).toBeVisible();
    const table = page.getByRole("table", { name: "Open findings" });
    await expect(table.getByRole("row").nth(1)).toBeVisible();
  });
});

test.describe("Approver", () => {
  test.use({ role: "approver" });

  test("sees the changes awaiting it, read-only", async ({ signedIn: page }) => {
    await page.goto("/approvals");
    await expect(page.getByRole("note")).toContainText("Read-only for now");
    await expect(page.getByRole("table", { name: "Changes awaiting approval" })).toBeVisible();
    await expect(page.getByRole("button", { name: /approve|reject/i })).toHaveCount(0);
  });
});

test.describe("Portal onboarding", () => {
  test.use({ role: "admin" });

  test("a platform administrator onboards a customer in order, then opens its portal", async ({ signedIn: page }) => {
    // A customer of its own, so the journey never uses up a seeded one.
    const code = `P${Date.now().toString(36).toUpperCase().slice(-6)}`;
    const created = await post<{ tenant: { id: string; name: string } }>("customerMaster", "/v1/mapping/tenants", {
      canonical_name: `Portal Pilot ${code}`,
      short_code: code,
    });
    const name = created.tenant.name;

    await page.goto("/onboarding");
    const table = page.getByRole("table", { name: "Customers and their portal" });
    await expect(table.getByRole("row").filter({ hasText: "Northwind" })).toContainText("Live");
    await table.getByRole("button", { name }).click();
    const steps = page.getByRole("region", { name });
    await expect(steps.getByRole("button", { name: "Go live" })).toBeDisabled();

    await steps.getByRole("button", { name: "Record the role set" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Choose the agreed roles" })).toBeVisible();

    await steps.getByLabel("Customer’s Entra tenant id").fill(uuid());
    await steps.getByLabel("Verified domains, comma separated").fill(`${code.toLowerCase()}.example.com`);
    await steps.getByRole("button", { name: "Record federation" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Federation recorded." })).toBeVisible();

    for (const role of ["CXO", "Security officer", "Approver"]) await steps.getByLabel(role).check();
    await steps.getByLabel("Where the agreement is recorded").fill(`SOW-${code} schedule 3`);
    await steps.getByRole("button", { name: "Record the role set" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Role set agreed." })).toBeVisible();

    const firstAdmin = uuid();
    await steps.getByLabel("Object id (B2B guest)").fill(firstAdmin);
    await steps.getByLabel("Name", { exact: true }).fill("Pilot IT lead");
    await steps.getByRole("combobox", { name: /^Role/ }).selectOption("CXO");
    await steps.getByRole("button", { name: "Assign administrator" }).click();
    await expect(page.getByRole("status").filter({ hasText: "First administrator assigned." })).toBeVisible();

    await steps.getByRole("button", { name: "Go live" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Portal live." })).toBeVisible();
    await expect(table.getByRole("row").filter({ hasText: name })).toContainText("Live");
    await expect(steps.getByRole("button", { name: "Suspend portal access" })).toBeVisible();
  });
});

test.describe("an account with no access", () => {
  test.use({ role: "customer" });

  test("is told who to ask, with its object id", async ({ page }) => {
    // A dev token for an object id nobody assigned: what a new B2B guest sees.
    const stranger = uuid();
    const { execFileSync } = await import("node:child_process");
    const repo = new URL("../../../", import.meta.url).pathname;
    const value = execFileSync("uv", ["run", "--quiet", "python", "-m", "cloudops_devdata.tokens", "--oid", stranger], {
      cwd: repo,
      env: { ...process.env, CLOUDOPS_ENV: "local", CLOUDOPS_LOCAL_SECRETS_FILE: process.env.CLOUDOPS_LOCAL_SECRETS_FILE ?? `${repo}.env.local` },
      encoding: "utf8",
    }).trim();
    await page.addInitScript((t) => window.sessionStorage.setItem("cloudops.dev-token", t), value);
    await page.goto("/");
    await expect(page.getByText("Your account has no access yet")).toBeVisible();
    await expect(page.getByText(stranger)).toBeVisible();
  });
});
