import { expect, navLabels, test } from "./fixtures";

test.use({ role: "admin" });

test("sees every built module, and the reserved mapping module stays hidden", async ({ signedIn: page }) => {
  await page.goto("/");
  expect(await navLabels(page)).toEqual(["Work queue", "Resources", "Customers"]);
  await page.goto("/mapping");
  await expect(page.getByText("No such page")).toBeVisible();
});
