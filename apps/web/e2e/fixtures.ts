import { readFileSync } from "node:fs";

import { test as base, expect, type Page } from "@playwright/test";

import type { WorkItemList } from "@cloudops/contracts";

import { TOKEN_DIR, type RoleName } from "./roles";

const API = "http://127.0.0.1:8000";

export function token(role: RoleName): string {
  return readFileSync(new URL(`${role}.txt`, TOKEN_DIR), "utf8");
}

/** Read the API directly, as a role: how a journey finds its test data. */
export async function api<T>(role: RoleName, path: string): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${token(role)}` } });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return (await response.json()) as T;
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
