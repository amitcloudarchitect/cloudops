import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

// The strict CSP (ADR 0017): every module renders and fetches under it, with no
// violation, and each page carries a fresh nonce.

test.use({ role: "admin" });

function violations(page: Page): string[] {
  const seen: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && /Content Security Policy/i.test(message.text())) seen.push(message.text());
  });
  return seen;
}

test("every module works under the CSP, with no violation", async ({ signedIn: page }) => {
  const seen = violations(page);
  for (const path of ["/", "/work", "/performance", "/posture", "/approvals", "/resources", "/customers", "/mapping", "/onboarding", "/connectors", "/finops", "/margin", "/pricing"]) {
    const response = await page.goto(path);
    const csp = response?.headers()["content-security-policy"] ?? "";
    expect(csp).toContain("default-src 'none'");
    expect(csp).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/);
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toContain("unsafe-inline");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  }
  // The client router's own navigation, as well as full page loads.
  await page.getByRole("navigation", { name: "Modules" }).getByRole("link", { name: "Work queue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Work queue" })).toBeVisible();
  expect(seen).toEqual([]);
});

test("connect-src names this origin, the API and Entra only", async ({ page }) => {
  const response = await page.goto("/signin");
  const csp = response?.headers()["content-security-policy"] ?? "";
  const connect = csp.split("; ").find((d) => d.startsWith("connect-src "));
  const api = new URL(process.env.CLOUDOPS_E2E_API ?? "http://127.0.0.1:8000").origin;
  const sources = connect?.split(" ").slice(1).filter((s) => s !== "ws:"); // ws: only under next dev
  expect(sources).toEqual(["'self'", api, "https://login.microsoftonline.com"]);
});

test("each page gets its own nonce", async ({ page }) => {
  const nonce = async () =>
    /'nonce-([^']+)'/.exec((await page.goto("/signin"))?.headers()["content-security-policy"] ?? "")?.[1];
  const first = await nonce();
  expect(first).toBeTruthy();
  expect(await nonce()).not.toEqual(first);
});
