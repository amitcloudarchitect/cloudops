import { expect, test } from "@playwright/test";

test("without a token the console asks the user to sign in", async ({ page }) => {
  await page.goto("/work");
  await expect(page.getByText("Sign in to use the console")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Modules" })).toHaveCount(0);
});

test("a forged token signs nobody in", async ({ page }) => {
  await page.addInitScript(() => window.sessionStorage.setItem("cloudops.dev-token", "e30.e30.c2ln"));
  await page.goto("/work");
  await expect(page.getByText("Sign in to use the console")).toBeVisible();
});
