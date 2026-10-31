// Assurance (step 17). The real control library comes from Security and is not
// in the repository, so each run loads a small test framework of its own
// (E2E-* controls) through the API, as a platform administrator would.
import { apiPost, expect, navLabels, test } from "./fixtures";

function unique(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

async function loadLibrary(framework: string): Promise<void> {
  const tag = framework.slice(-6).toUpperCase();
  const loaded = await apiPost("admin", "/v1/assurance/library", {
    framework,
    version: "e2e-1",
    expected_count: 2,
    controls: [
      {
        code: `E2E-${tag}-IAM`,
        domain: "Identity",
        title: "Global administrators",
        requirement: "At most two accounts hold Global Administrator.",
        assessment: "automated",
        evidence_source: "entra",
        evidence_query: "entra.role_holders",
        parameters: { role_name: "Global Administrator", compliant_max: 2 },
      },
      {
        code: `E2E-${tag}-PHY`,
        domain: "Physical",
        title: "Data centre access",
        requirement: "Physical access to the data centre is badge controlled.",
        assessment: "manual",
        evidence_source: "human",
      },
    ],
  });
  expect(loaded.status).toBe(201);
}

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("opens an assessment, collects evidence, approves, and the score follows approvals only", async ({ signedIn: page }) => {
    const framework = unique("e2e");
    await loadLibrary(framework);
    const tag = framework.slice(-6).toUpperCase();
    await page.goto("/assurance");
    expect(await navLabels(page)).toContain("Assurance");
    const form = page.getByRole("form", { name: "Open an assessment" });
    await form.getByLabel("Framework").selectOption(framework);
    await form.getByLabel("Title").fill(`Assessment ${framework}`);
    await form.getByRole("button", { name: "Open assessment" }).click();
    await expect(page.getByRole("heading", { level: 1, name: `Assessment ${framework}` })).toBeVisible();
    await expect(page.getByText("Not scored: nothing approved yet")).toBeVisible();

    await page.getByRole("button", { name: "Collect evidence" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Evidence collected for 1 automated controls." })).toBeVisible();
    const iam = page.getByRole("article", { name: new RegExp(`E2E-${tag}-IAM`) });
    await expect(iam).toContainText("Proposed by the evidence rule");
    await expect(iam).toContainText("Awaiting approval");

    // A manual control is approved as manual and never scores.
    const phy = page.getByRole("article", { name: new RegExp(`E2E-${tag}-PHY`) });
    await phy.getByLabel(`Result for E2E-${tag}-PHY`).selectOption("manual");
    await phy.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByRole("status").filter({ hasText: `E2E-${tag}-PHY approved as manual.` })).toBeVisible();
    await expect(page.getByText("Not scored: nothing approved yet")).toBeVisible();

    // Compliant needs a narrative first; then it scores.
    await iam.getByLabel(`Result for E2E-${tag}-IAM`).selectOption("compliant");
    await iam.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "A compliant result needs a narrative" })).toBeVisible();
    await iam.getByLabel(`Narrative for E2E-${tag}-IAM`).fill("Directory evidence shows the Global Administrator holders.");
    await iam.getByRole("button", { name: "Save narrative" }).click();
    await expect(page.getByRole("status").filter({ hasText: `Narrative for E2E-${tag}-IAM saved.` })).toBeVisible();
    await iam.getByLabel(`Result for E2E-${tag}-IAM`).selectOption("compliant");
    await iam.getByRole("button", { name: "Approve" }).click();
    await expect(iam).toContainText("Compliant");
    await expect(page.getByText("100.00%")).toBeVisible();
  });
});

test.describe("engineer", () => {
  test.use({ role: "engineer" });

  test("is not offered assurance, and the screen says so", async ({ signedIn: page }) => {
    await page.goto("/assurance");
    expect(await navLabels(page)).not.toContain("Assurance");
    await expect(page.getByText("Not available to your role")).toBeVisible();
  });
});
