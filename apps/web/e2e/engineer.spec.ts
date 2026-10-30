import type { WorkItemDetail } from "@cloudops/contracts";

import { api, expect, navLabels, test, triagedTicket } from "./fixtures";

test.use({ role: "engineer" });

test("sees the operations modules and nothing reserved", async ({ signedIn: page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/work$/);
  expect(await navLabels(page)).toEqual(["Work queue", "Resources", "Customers"]);
  await expect(page.getByTestId("role")).toHaveText("Engineer");
});

test("works the queue with a preset view and finds a ticket", async ({ signedIn: page }) => {
  await page.goto("/work");
  const table = page.getByRole("table", { name: "Work items" });
  await expect(table).toBeVisible();
  await expect(table.getByRole("columnheader", { name: /Triage/ })).toBeVisible();
  await page.getByLabel("View").selectOption("Needs a person");
  await expect(table.getByRole("row").nth(1)).toContainText("Needs a person");
  const rows = await table.getByRole("row").count();
  await page.getByLabel("View").selectOption("All");
  expect(await table.getByRole("row").count()).toBeGreaterThan(rows);

  const number = await triagedTicket("human");
  await page.getByLabel("Filter").fill(number);
  await expect(table.getByRole("row")).toHaveCount(2);
  await table.getByRole("link", { name: number }).click();
  await expect(page.getByRole("heading", { level: 1, name: number })).toBeVisible();
});

test("sorts by a column and exports what is shown", async ({ signedIn: page }) => {
  await page.goto("/work");
  const header = page.getByRole("columnheader", { name: /Opened/ });
  await header.getByRole("button").click();
  await expect(header).toHaveAttribute("aria-sort", "ascending");
  await header.getByRole("button").click();
  await expect(header).toHaveAttribute("aria-sort", "descending");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV" }).click();
  expect((await download).suggestedFilename()).toMatch(/^work-queue-\d{4}-\d{2}-\d{2}\.csv$/);
});

test("saves a view for the role and gets it back", async ({ signedIn: page }) => {
  await page.goto("/work");
  await page.getByLabel("Kind").selectOption("Incident");
  page.once("dialog", (d) => void d.accept("My incidents"));
  await page.getByRole("button", { name: "Save view" }).click();
  await page.reload();
  await page.getByLabel("View").selectOption("My incidents");
  await expect(page.getByLabel("Kind")).toHaveValue("Incident");
});

test("accepts a triage, and the review is recorded", async ({ signedIn: page }) => {
  const number = await triagedTicket("ready_for_planning");
  await page.goto(`/work/${number}`);
  const panel = page.getByRole("region", { name: "Agent triage" });
  await expect(panel).toContainText("Ready for planning");
  await expect(panel).toContainText("Planning (stage 4) is not built yet");
  const before = await panel.getByTestId("review").count();
  await panel.getByRole("button", { name: "Accept triage" }).click();
  await expect(panel.getByRole("status")).toHaveText("Triage accepted.");
  await expect(panel.getByTestId("review")).toHaveCount(before + 1);
  const detail = await api<WorkItemDetail>("engineer", `/v1/work-items/${number}`);
  expect(detail.triage?.reviews.at(-1)?.decision).toBe("accept");
});

test("overrides a triage with a reason, and cannot override without one", async ({ signedIn: page }) => {
  const number = await triagedTicket("human");
  await page.goto(`/work/${number}`);
  const panel = page.getByRole("region", { name: "Agent triage" });
  await panel.getByRole("button", { name: "Override…" }).click();
  const form = panel.getByRole("form", { name: "Override the triage" });
  await form.getByLabel("Intent").selectOption("backup_failure");
  await form.getByLabel("Risk").selectOption("high");
  await form.getByRole("button", { name: "Record override" }).click();
  // The reason is required: the browser stops the submit, nothing is recorded.
  await expect(form).toBeVisible();
  await form.getByLabel("Reason (required)").fill("The disk is the backup target; the backup failed.");
  await form.getByRole("button", { name: "Record override" }).click();
  await expect(panel.getByRole("status")).toHaveText("Override recorded.");
  await expect(panel.getByTestId("review").last()).toContainText("Intent → backup_failure");
});

test("follows a ticket to its resource's context", async ({ signedIn: page }) => {
  const number = await triagedTicket("human");
  const detail = await api<WorkItemDetail>("engineer", `/v1/work-items/${number}`);
  await page.goto(`/work/${number}`);
  await page.getByRole("link", { name: detail.configuration_item ?? "Resource" }).click();
  await expect(page).toHaveURL(new RegExp(`/resources/${detail.resource_id}$`));
  await expect(page.getByRole("heading", { name: "Live metrics" })).toBeVisible();
  await expect(page.getByText("telemetry store, which is not built yet")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Tags" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Vulnerabilities", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Open incidents" }).getByRole("link", { name: number })).toBeVisible();
});

test("sees the customers it is assigned, each figure with its source", async ({ signedIn: page }) => {
  await page.goto("/customers");
  const headings = page.getByRole("heading", { level: 2 });
  await expect(headings).toHaveText([/Northwind|Contoso/, /Northwind|Contoso/]);
  const card = page.getByRole("link", { name: /^Open incidents.*View the evidence$/ }).first();
  await expect(card).toContainText("ServiceNow");
  await card.click();
  await expect(page).toHaveURL(/\/work\?customer=.*kind=Incident/);
  await expect(page.getByLabel("Kind")).toHaveValue("Incident");
});
