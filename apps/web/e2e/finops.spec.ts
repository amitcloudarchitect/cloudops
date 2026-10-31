import { expect, navLabels, test } from "./fixtures";

test.use({ role: "finops" });

test("has no work queue in its navigation", async ({ signedIn: page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/resources$/);
  expect(await navLabels(page)).toEqual(["Resources", "Customers", "FinOps", "Margin", "Pricing"]);
});

test("a typed-in work queue URL says it is not available", async ({ signedIn: page }) => {
  await page.goto("/work");
  await expect(page.getByText("Not available to your role")).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);
});

test("sees cost for every customer, and ticket figures as unavailable, not zero", async ({ signedIn: page }) => {
  await page.goto("/customers");
  await expect(page.getByRole("heading", { level: 2 }).first()).toBeVisible();
  expect(await page.getByRole("heading", { level: 2 }).count()).toBeGreaterThanOrEqual(5);
  await expect(page.getByText("Not available to your role").first()).toBeVisible();
  await expect(page.getByRole("link", { name: /^Open incidents.*View the evidence$/ })).toHaveCount(0);
  await expect(page.getByText(/Cloud cost, 30 days \([A-Z]{3}\)/).first()).toBeVisible();
});

test("opens a resource without tickets or findings", async ({ signedIn: page }) => {
  await page.goto("/resources");
  await page.getByRole("table", { name: "Resources" }).getByRole("link").first().click();
  await expect(page.getByText("Tickets are not available to your role.").first()).toBeVisible();
  await expect(page.getByText("Vulnerability findings need the vulnerabilities permission.")).toBeVisible();
});
