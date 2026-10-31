import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

import { test as base, expect, type Page } from "@playwright/test";

import type { WorkItemList } from "@cloudops/contracts";

import { TOKEN_DIR, type RoleName } from "./roles";

// Overridable so a second checkout can run its own API and console beside a running pair.
const API = process.env.CLOUDOPS_E2E_API ?? "http://127.0.0.1:8000";

export function token(role: RoleName): string {
  return readFileSync(new URL(`${role}.txt`, TOKEN_DIR), "utf8");
}

/** Read the API directly, as a role: how a journey finds its test data. */
export async function api<T>(role: RoleName, path: string): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${token(role)}` } });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return (await response.json()) as T;
}

/** Write through the API directly, as a role: how a journey sets up what it needs. */
export async function apiPost<T>(role: RoleName, path: string, body?: unknown): Promise<{ status: number; body: T }> {
  const response = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token(role)}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, body: (await response.json()) as T };
}

/** Hold a company name in quarantine as a connector would (local only), so a journey
 * maps a name of its own and never uses up the seeded quarantine. */
export function holdName(alias: string, sightings: number): void {
  const repo = new URL("../../../", import.meta.url).pathname;
  execFileSync("uv", ["run", "--quiet", "python", "-m", "cloudops_devdata.quarantine", "--alias", alias, "--count", String(sightings)], {
    cwd: repo,
    env: { ...process.env, CLOUDOPS_ENV: "local", CLOUDOPS_LOCAL_SECRETS_FILE: `${repo}.env.local` },
  });
}

/** Raise schema drift on one field as a connector would (local only), so a journey
 * acknowledges drift of its own and never uses up the seeded rows. */
export function raiseDrift(source: string, entity: string, field: string): void {
  const repo = new URL("../../../", import.meta.url).pathname;
  execFileSync("uv", ["run", "--quiet", "python", "-m", "cloudops_devdata.ingest_health", "--drift", source, entity, field], {
    cwd: repo,
    env: { ...process.env, CLOUDOPS_ENV: "local", CLOUDOPS_LOCAL_SECRETS_FILE: `${repo}.env.local` },
  });
}

export async function triagedTicket(route: "human" | "ready_for_planning"): Promise<string> {
  const list = await api<WorkItemList>("engineer", "/v1/work-items?status=open");
  const item = list.items.find((w) => w.triage_route === route);
  if (!item) throw new Error(`no seeded ticket triaged ${route}: run make seed`);
  return item.number;
}

export const test = base.extend<{ role: RoleName; signedIn: Page }>({
  role: ["engineer", { option: true }],
  signedIn: async ({ page, role }, use) => {
    const value = token(role);
    await page.addInitScript((t) => window.sessionStorage.setItem("cloudops.dev-token", t), value);
    await use(page);
  },
});

export { expect };

export async function navLabels(page: Page): Promise<string[]> {
  const nav = page.getByRole("navigation", { name: "Modules" });
  await expect(nav).toBeVisible();
  return (await nav.getByRole("link").allTextContents()).map((s) => s.trim());
}
