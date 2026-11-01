import { expect, navAreas, navLabels, test } from "./fixtures";

test.use({ role: "admin" });

test("sees every module, customer mapping included", async ({ signedIn: page }) => {
  await page.goto("/");
  expect(await navLabels(page)).toEqual([
    "Home",
    "Work queue",
    "Resources",
    "Approvals",
    "Performance",
    "Customers",
    "Observability",
    "Security posture",
    "FinOps",
    "Margin",
    "Pricing",
    "Assurance",
    "Reports",
    "Knowledge",
    "Cloud onboarding",
    "Portal onboarding",
    "Customer mapping",
    "Connectors",
    "Commercial setup",
  ]);
  expect(await navAreas(page)).toEqual(["Operations", "Security", "Commercial", "Assurance & Reports", "Knowledge", "Settings"]);
  const settings = page.getByRole("list", { name: "Settings" });
  await expect(settings.getByRole("link")).toHaveText(["Cloud onboarding", "Portal onboarding", "Customer mapping", "Connectors", "Commercial setup"]);
  await page.goto("/mapping");
  await expect(page.getByRole("heading", { level: 1, name: "Customer mapping" })).toBeVisible();
});
