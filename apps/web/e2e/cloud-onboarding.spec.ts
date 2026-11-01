// Cloud onboarding (ADR 0027, under ADR 0003) as a platform administrator, and as
// staff without access.manage. The read-only check itself runs in the connector
// worker and is proven in pytest; here: the form takes identifiers only, names the
// Key Vault secret to create, and lists the credential in the inventory.
import { expect, navLabels, test } from "./fixtures";

function code(): string {
  return `E2E${Math.random().toString(36).slice(2, 7).toUpperCase()}`.slice(0, 10);
}

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("records an app registration for a new customer and is told the secret's name", async ({ signedIn: page }) => {
    const short = code();
    await page.goto("/cloud-onboarding");
    await expect(page.getByRole("heading", { level: 1, name: "Cloud onboarding" })).toBeVisible();
    const form = page.getByRole("form", { name: "Onboard a cloud account" });
    // Identifiers only: there is no field a secret could be typed into.
    await expect(form.getByLabel(/(?:secret value|client secret)$/i)).toHaveCount(0);
    await form.getByLabel("Customer").selectOption("new");
    await form.getByLabel("New customer’s name").fill(`E2E Lab ${short}`);
    await form.getByLabel("Short code (names its Key Vault secrets)").fill(short);
    await form.getByLabel("Directory (tenant) id").fill(crypto.randomUUID());
    await form.getByLabel("Application (client) id").fill(crypto.randomUUID());
    await form.getByLabel("Subscription ids, one per line").fill(crypto.randomUUID());
    await form.getByRole("button", { name: "Record and check" }).click();
    await expect(page.getByRole("status")).toContainText("recorded");
    await expect(page.getByText(`customer-${short.toLowerCase()}-azure-secret`)).toBeVisible();
    await expect(page.getByText(/Waiting for the connector worker/)).toBeVisible();
    await expect(page.getByRole("table", { name: "Stored customer credentials" })).toContainText(`E2E Lab ${short}`);
  });

  test("refuses an AWS key id that is not one", async ({ signedIn: page }) => {
    await page.goto("/cloud-onboarding");
    const form = page.getByRole("form", { name: "Onboard a cloud account" });
    await form.getByLabel("AWS (IAM access key)").check();
    await form.getByLabel("AWS account id").fill("123456789012");
    await form.getByLabel("Access key id (not the secret key)").fill("not-a-key");
    await form.getByRole("button", { name: "Record and check" }).click();
    await expect(form.getByRole("alert")).toContainText("access key id");
  });
});

test.describe("staff without access.manage", () => {
  test.use({ role: "customerMaster" });

  test("has no Cloud onboarding module", async ({ signedIn: page }) => {
    await page.goto("/");
    expect(await navLabels(page)).not.toContain("Cloud onboarding");
  });
});
