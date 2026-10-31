import { defineConfig, devices } from "@playwright/test";

// Critical journeys per role (step 14) against the real API on a seeded local
// database: `make up && make seed` first. Playwright starts the API and the
// console unless they are already running.
const ci = !!process.env.CI;
const repo = new URL("../..", import.meta.url).pathname;
// Another checkout's API and console may already hold the default ports; point
// these at your own pair instead (started by you, with CORS for that origin).
const web = process.env.CLOUDOPS_E2E_WEB ?? "http://localhost:3000";
const api = process.env.CLOUDOPS_E2E_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: true,
  forbidOnly: ci,
  retries: ci ? 1 : 0,
  reporter: ci ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: web,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    // Responsive to tablet: the same journeys at iPad width, in Chromium.
    { name: "tablet", use: { ...devices["Desktop Chrome"], viewport: { width: 820, height: 1180 }, hasTouch: true } },
  ],
  webServer: [
    {
      command: "make api",
      cwd: repo,
      url: `${api}/healthz`,
      reuseExistingServer: !ci,
      timeout: 60_000,
    },
    {
      command: ci ? "npm run build && npm run start" : "npm run dev",
      url: `${web}/signin`,
      reuseExistingServer: !ci,
      timeout: 180_000,
      env: { NEXT_PUBLIC_AUTH_MODE: "local", CLOUDOPS_WEB_API_BASE: api, NEXT_TELEMETRY_DISABLED: "1" },
    },
  ],
});
