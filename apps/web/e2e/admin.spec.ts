import { expect, navLabels, test } from "./fixtures";

test.use({ role: "admin" });

test("sees every module, customer mapping included", async ({ signedIn: page }) => {
  await page.goto("/");
  expect(await navLabels(page)).toEqual([
    "Work queue",
    "Performance",
    "Security posture",
    "Approvals",
    "Resources",
    "Customers",
    "Customer mapping",
    "Portal onboarding",
    "Connectors",
    "FinOps",
    "Margin",
    "Pricing",
  ]);
  await page.goto("/mapping");
  await expect(page.getByRole("heading", { level: 1, name: "Customer mapping" })).toBeVisible();
});
