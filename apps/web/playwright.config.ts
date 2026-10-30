import { defineConfig, devices } from "@playwright/test";

// Critical journeys per role (step 14) against the real API on a seeded local
// database: `make up && make seed` first. Playwright starts the API and the
// console unless they are already running.
const ci = !!process.env.CI;
const repo = new URL("../..", import.meta.url).pathname;

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: true,
  forbidOnly: ci,
  retries: ci ? 1 : 0,
  reporter: ci ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://localhost:3000",
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
      url: "http://127.0.0.1:8000/healthz",
      reuseExistingServer: !ci,
      timeout: 60_000,
    },
    {
      command: ci ? "npm run build && npm run start" : "npm run dev",
      url: "http://localhost:3000/signin",
      reuseExistingServer: !ci,
      timeout: 180_000,
      env: { NEXT_PUBLIC_AUTH_MODE: "local", NEXT_PUBLIC_API_BASE: "http://127.0.0.1:8000", NEXT_TELEMETRY_DISABLED: "1" },
    },
  ],
});
