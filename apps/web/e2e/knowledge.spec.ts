// Knowledge (step 17): staleness and gaps for staff, never offered to customers.
// The seed's SharePoint fixtures supply the documents (make seed, then the
// SharePoint connector with --fixtures); journeys assert only on what the
// fixtures fix: kinds come from file names and folders.
import { expect, navLabels, test } from "./fixtures";

test.describe("engineer", () => {
  test.use({ role: "engineer" });

  test("sees each customer's design and handover coverage, and documents with their status", async ({ signedIn: page }) => {
    await page.goto("/knowledge");
    expect(await navLabels(page)).toContain("Knowledge");
    await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
    const gaps = page.getByRole("table", { name: "Design and handover coverage" });
    await expect(gaps).toBeVisible();
    // No fixture site holds a design document: every customer in scope shows the gap.
    await expect(gaps.locator("tbody tr").first()).toContainText("Design: Missing");
    await page.getByText("How staleness is decided").click();
    await expect(page.getByText("names a resource removed from the estate", { exact: false })).toBeVisible();
  });
});

test.describe("customer", () => {
  test.use({ role: "customer" });

  test("is not offered knowledge", async ({ signedIn: page }) => {
    await page.goto("/work");
    expect(await navLabels(page)).not.toContain("Knowledge");
  });
});
