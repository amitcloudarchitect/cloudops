// AMARA, the read-only assistant panel (ADR 0029). The console, sign-in and every
// screen run against the local API as in the other journeys; the assistant's own
// endpoints are answered here with page.route, shaped by the generated types. A
// live model's answers vary from run to run, and the panel's job is to show what
// the API says: these journeys pin what it says. The API's own behaviour (scope,
// refusals, owner-only history) is tested in db/tests/test_assistant_api.py.

import AxeBuilder from "@axe-core/playwright";
import type { Page, Request } from "@playwright/test";

import type {
  AskRequest,
  AssistantSession,
  ConversationSummary,
  ConversationView,
  EnabledRequest,
  EnabledView,
  SourceView,
  TurnView,
  WorkItemDetail,
} from "@cloudops/contracts";

import { api, expect, test, triagedTicket } from "./fixtures";

const NORTHWIND = { id: "6f1c1c1e-0000-4000-8000-000000000001", name: "Northwind Logistics" };
const CONVERSATION = "7a7a7a7a-0000-4000-8000-000000000001";
const AS_OF = "2026-10-30T09:15:00Z";

interface Reply {
  status: TurnView["status"];
  text: string;
  sources?: SourceView[];
}

interface Mock {
  session?: Partial<AssistantSession>;
  /** The answer to each question, in order; the last one repeats. */
  replies?: Reply[];
  conversations?: ConversationSummary[];
}

interface Recorded {
  sessions: URL[];
  asked: AskRequest[];
  deleted: string[];
}

function session(overrides: Partial<AssistantSession> = {}): AssistantSession {
  return {
    available: true,
    unavailable_reason: null,
    mode: "account",
    tenant: NORTHWIND,
    tenants: [],
    suggestions: ["What can AMARA help me with?"],
    ...overrides,
  };
}

function turn(seq: number, question: string, over: Partial<TurnView> = {}): TurnView {
  return {
    id: `8b8b8b8b-0000-4000-8000-${String(seq).padStart(12, "0")}`,
    conversation_id: CONVERSATION,
    question,
    status: "pending",
    stage: null,
    answer: null,
    reason: null,
    created_at: "2026-11-01T10:00:00Z",
    answered_at: null,
    ...over,
  };
}

/** Answer the assistant's endpoints; a turn is "retrieving" on its first poll, then done. */
async function mockAssistant(page: Page, mock: Mock = {}): Promise<Recorded> {
  const recorded: Recorded = { sessions: [], asked: [], deleted: [] };
  const turns = new Map<string, { view: TurnView; reply: Reply; polls: number }>();
  const replies = mock.replies ?? [{ status: "answered", text: "Here is what the platform shows.", sources: [] }];

  await page.route("**/v1/assistant/**", async (route, request: Request) => {
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (status: number, body: unknown) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

    if (path.endsWith("/v1/assistant/session")) {
      recorded.sessions.push(url);
      return json(200, session(mock.session));
    }
    if (path.endsWith("/v1/assistant/questions") && request.method() === "POST") {
      const body = request.postDataJSON() as AskRequest;
      recorded.asked.push(body);
      const seq = recorded.asked.length;
      const reply = replies[Math.min(seq - 1, replies.length - 1)] as Reply;
      const view = turn(seq, body.question);
      turns.set(view.id, { view, reply, polls: 0 });
      return json(202, view);
    }
    const polled = /\/v1\/assistant\/turns\/([^/]+)$/.exec(path);
    if (polled) {
      const entry = turns.get(polled[1] ?? "");
      if (!entry) return json(404, { error: { code: "not_found", request_id: "test" } });
      entry.polls += 1;
      if (entry.polls === 1) return json(200, { ...entry.view, stage: "retrieving" } satisfies TurnView);
      const done: TurnView = {
        ...entry.view,
        status: entry.reply.status,
        stage: null,
        answer: { text: entry.reply.text, claims: [{ text: entry.reply.text, sources: (entry.reply.sources ?? []).map((s) => s.ref) }], sources: entry.reply.sources ?? [] },
        reason: entry.reply.status === "refused" ? "cross_customer" : null,
        answered_at: "2026-11-01T10:00:05Z",
      };
      return json(200, done);
    }
    const deleted = /\/v1\/assistant\/conversations\/([^/]+)\/delete$/.exec(path);
    if (deleted && request.method() === "POST") {
      recorded.deleted.push(deleted[1] ?? "");
      return route.fulfill({ status: 204 });
    }
    const one = /\/v1\/assistant\/conversations\/([^/]+)$/.exec(path);
    if (one) {
      const summary = (mock.conversations ?? []).find((c) => c.id === one[1]);
      if (!summary) return json(404, { error: { code: "not_found", request_id: "test" } });
      const view: ConversationView = {
        ...summary,
        turns: [
          turn(1, summary.title, {
            id: "9c9c9c9c-0000-4000-8000-000000000001",
            conversation_id: summary.id,
            status: "answered",
            answer: { text: "An earlier answer.", claims: [], sources: [] },
            answered_at: summary.updated_at,
          }),
        ],
      };
      return json(200, view);
    }
    if (path.endsWith("/v1/assistant/conversations")) return json(200, mock.conversations ?? []);
    return json(404, { error: { code: "not_found", request_id: "test" } });
  });
  return recorded;
}

function panel(page: Page) {
  return page.getByRole("complementary", { name: /AMARA/ });
}

/** The turns themselves, apart from the live region that announces them. */
function answers(page: Page) {
  return panel(page).getByRole("list", { name: "Conversation" });
}

async function openPanel(page: Page) {
  await page.getByRole("button", { name: "Ask AMARA" }).click();
  await expect(panel(page)).toBeVisible();
}

test.describe("engineer", () => {
  test.use({ role: "engineer" });

  test("on a ticket: sees ticket suggestions, asks one, and reads an answer with its sources", async ({ signedIn: page }) => {
    const number = await triagedTicket("human");
    const ticket = await api<WorkItemDetail>("engineer", `/v1/work-items/${number}`);
    const suggestions = ["Summarise this ticket and where it stands.", "Is this ticket at risk of breaching its SLA?"];
    const recorded = await mockAssistant(page, {
      session: { tenant: { id: ticket.tenant.id, name: ticket.tenant.name }, tenants: [{ id: ticket.tenant.id, name: ticket.tenant.name }], suggestions },
      replies: [
        {
          status: "answered",
          text: `${number} is open and within its SLA.`,
          sources: [
            { ref: "S1", label: `Ticket ${number}`, source_system: "servicenow", as_of: AS_OF, is_modelled: false, url: null },
            { ref: "P1", label: "Help: Reading a ticket", source_system: "platform_help", as_of: null, is_modelled: false, url: null },
          ],
        },
      ],
    });

    await page.goto(`/work/${number}`);
    await expect(page.getByRole("heading", { level: 1, name: number })).toBeVisible();
    await openPanel(page);
    await expect(panel(page).getByRole("textbox", { name: "Ask AMARA a question" })).toBeFocused();
    await expect(panel(page).getByTestId("assistant-about")).toHaveText(ticket.tenant.name);
    const last = recorded.sessions.at(-1);
    expect(last?.searchParams.get("screen")).toBe("ticket");
    expect(last?.searchParams.get("tenant_id")).toBe(ticket.tenant.id);

    await panel(page).getByRole("button", { name: suggestions[0] }).click();
    await expect(answers(page).getByText(`${number} is open and within its SLA.`)).toBeVisible();
    const sources = answers(page).getByRole("region", { name: "Sources" });
    await expect(sources.getByText(`Ticket ${number}`)).toBeVisible();
    await expect(sources.getByText(/As of 30 Oct 2026, 09:15/)).toBeVisible();
    await expect(sources.getByText("Help: Reading a ticket")).toBeVisible();
    await expect(panel(page).getByText("AMARA is read-only and can be wrong. Check the sources.")).toBeVisible();

    expect(recorded.asked[0]).toEqual({
      question: suggestions[0],
      conversation_id: null,
      tenant_id: ticket.tenant.id,
      context: { screen: "ticket", record_type: "ticket", record_id: number },
    });
  });

  test("a cross-customer question shows the refusal, and the next question continues the conversation", async ({ signedIn: page }) => {
    const refusal = "I can only answer about one customer account at a time, so I can't compare customers or answer about another customer. Ask me about this account on its own.";
    const recorded = await mockAssistant(page, {
      replies: [
        { status: "refused", text: refusal },
        { status: "dont_know", text: "I don't know: I couldn't back an answer with the platform's data, so I won't guess." },
      ],
    });
    await page.goto("/customers");
    await openPanel(page);
    const box = panel(page).getByRole("textbox", { name: "Ask AMARA a question" });
    await box.fill("Compare us with Fabrikam");
    await box.press("Enter");
    await expect(answers(page).getByText("Outside what AMARA can answer")).toBeVisible();
    await expect(answers(page).getByText(refusal)).toBeVisible();
    await expect(panel(page).getByRole("region", { name: "Sources" })).toHaveCount(0);

    await box.fill("How many incidents are open?");
    await box.press("Enter");
    await expect(answers(page).getByText("I don’t know", { exact: true })).toBeVisible();
    expect(recorded.asked[1]?.conversation_id).toBe(CONVERSATION);
    expect(recorded.asked[1]?.tenant_id).toBeNull();
  });

  test("a failed answer offers a retry, and Shift+Enter adds a line rather than sending", async ({ signedIn: page }) => {
    const recorded = await mockAssistant(page, {
      replies: [
        { status: "failed", text: "The assistant couldn't answer just now. Try again in a moment." },
        { status: "answered", text: "Second time lucky." },
      ],
    });
    await page.goto("/work");
    await openPanel(page);
    const box = panel(page).getByRole("textbox", { name: "Ask AMARA a question" });
    await box.fill("First line");
    await box.press("Shift+Enter");
    await box.pressSequentially("second line");
    expect(recorded.asked).toHaveLength(0);
    await panel(page).getByRole("button", { name: "Send" }).click();
    await expect(answers(page).getByText("No answer")).toBeVisible();
    expect(recorded.asked[0]?.question).toBe("First line\nsecond line");
    await panel(page).getByRole("button", { name: "Try again" }).click();
    await expect(answers(page).getByText("Second time lucky.")).toBeVisible();
  });

  test("opens with Ctrl+/, closes with Escape, and focus returns to the button", async ({ signedIn: page }) => {
    await mockAssistant(page);
    await page.goto("/work");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const launcher = page.getByRole("button", { name: "Ask AMARA" });
    await expect(launcher).toHaveAttribute("aria-keyshortcuts", /Control\+\//);
    await page.keyboard.press("Control+/");
    await expect(panel(page)).toBeVisible();
    await expect(panel(page).getByRole("textbox", { name: "Ask AMARA a question" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(panel(page)).toHaveCount(0);
    await expect(launcher).toBeFocused();
    await expect(launcher).toHaveAttribute("aria-expanded", "false");
  });

  test("history: opens an earlier conversation, continues it, and deletes one after confirming", async ({ signedIn: page }) => {
    const conversations: ConversationSummary[] = [
      { id: CONVERSATION, title: "Which tickets breached SLA?", mode: "account", tenant: NORTHWIND, updated_at: AS_OF },
      { id: "7a7a7a7a-0000-4000-8000-000000000002", title: "What do the health bands mean?", mode: "help", tenant: null, updated_at: AS_OF },
    ];
    const recorded = await mockAssistant(page, { conversations });
    await page.goto("/work");
    await openPanel(page);
    await panel(page).getByRole("button", { name: "History" }).click();
    const list = panel(page).getByRole("list", { name: "Your conversations" });
    await expect(list.getByText("Platform help ·")).toBeVisible();
    await expect(list.getByText(`${NORTHWIND.name} ·`)).toBeVisible();

    await list.getByRole("button", { name: "Delete “What do the health bands mean?”" }).click();
    await panel(page).getByRole("button", { name: "Delete conversation" }).click();
    await expect(panel(page).getByText("Conversation deleted.")).toBeVisible();
    expect(recorded.deleted).toEqual(["7a7a7a7a-0000-4000-8000-000000000002"]);
    await expect(list.getByText("What do the health bands mean?")).toHaveCount(0);

    await list.getByRole("button", { name: /^Which tickets breached SLA\?/ }).click();
    await expect(answers(page).getByText("An earlier answer.")).toBeVisible();
    await expect(panel(page).getByTestId("assistant-about")).toHaveText(NORTHWIND.name);
    const box = panel(page).getByRole("textbox", { name: "Ask AMARA a question" });
    await box.fill("And this week?");
    await box.press("Enter");
    await expect(answers(page).getByText("Here is what the platform shows.")).toBeVisible();
    expect(recorded.asked[0]?.conversation_id).toBe(CONVERSATION);
    expect(recorded.asked[0]?.tenant_id).toBeNull();
  });

  test("staff in help mode can choose a customer, which starts a conversation about it", async ({ signedIn: page }) => {
    const other = { id: "6f1c1c1e-0000-4000-8000-000000000002", name: "Contoso Retail" };
    const recorded = await mockAssistant(page, {
      session: { mode: "help", tenant: null, tenants: [NORTHWIND, other], suggestions: ["What can AMARA help me with?"] },
    });
    await page.goto("/work");
    await openPanel(page);
    await expect(panel(page).getByTestId("assistant-about")).toHaveText("Platform help — no customer selected");
    await panel(page).getByLabel("Customer").selectOption(other.id);
    await expect.poll(() => recorded.sessions.at(-1)?.searchParams.get("tenant_id")).toBe(other.id);
  });
});

test.describe("customer in the portal", () => {
  test.use({ role: "cxo" });

  test("asks about their own account; no tenant is sent and no customer can be chosen", async ({ signedIn: page }) => {
    const recorded = await mockAssistant(page, {
      session: { suggestions: ["Give me a summary of service health for this account."] },
      replies: [
        {
          status: "answered",
          text: "Two incidents are open; none has breached its SLA.",
          sources: [{ ref: "S1", label: "Open incidents", source_system: "servicenow", as_of: AS_OF, is_modelled: false, url: null }],
        },
      ],
    });
    await page.goto("/customers");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(/Northwind/);
    await openPanel(page);
    await expect(panel(page).getByLabel("Customer")).toHaveCount(0);
    expect(recorded.sessions.at(-1)?.searchParams.get("screen")).toBe("customer");
    const box = panel(page).getByRole("textbox", { name: "Ask AMARA a question" });
    await box.fill("How are our services doing?");
    await box.press("Enter");
    await expect(answers(page).getByText("Two incidents are open; none has breached its SLA.")).toBeVisible();
    await expect(answers(page).getByText(/As of 30 Oct 2026/)).toBeVisible();
    expect(recorded.asked[0]?.tenant_id).toBeNull();
    expect(recorded.asked[0]?.context?.screen).toBe("customer");
  });

  test("says so when AMARA is not switched on for the organisation", async ({ signedIn: page }) => {
    await mockAssistant(page, { session: { available: false, unavailable_reason: "not_enabled", suggestions: [] } });
    await page.goto("/customers");
    await openPanel(page);
    await expect(panel(page).getByText("AMARA isn’t switched on for your organisation yet.")).toBeVisible();
    await expect(panel(page).getByRole("textbox", { name: "Ask AMARA a question" })).toHaveCount(0);
  });
});

test.describe("FinOps", () => {
  test.use({ role: "finops" });

  test("on cost: sees cost suggestions, asks one, and a modelled source is marked", async ({ signedIn: page }) => {
    const suggestions = ["How has cloud spend moved over the last 30 days?", "How are costs converted between currencies?"];
    const recorded = await mockAssistant(page, {
      session: { mode: "help", tenant: null, tenants: [NORTHWIND], suggestions },
      replies: [
        {
          status: "answered",
          text: "Spend rose 4% on the previous 30 days.",
          sources: [
            { ref: "S1", label: "Cloud cost, last 30 days", source_system: "cost_fact", as_of: AS_OF, is_modelled: false, url: null },
            { ref: "S2", label: "Forecast to month end", source_system: "cost_fact", as_of: AS_OF, is_modelled: true, url: null },
          ],
        },
      ],
    });
    await page.goto("/finops");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await openPanel(page);
    expect(recorded.sessions.at(-1)?.searchParams.get("screen")).toBe("finops");
    for (const s of suggestions) await expect(panel(page).getByRole("button", { name: s })).toBeVisible();
    await panel(page).getByRole("button", { name: suggestions[0] }).click();
    await expect(answers(page).getByText("Spend rose 4% on the previous 30 days.")).toBeVisible();
    const sources = answers(page).getByRole("region", { name: "Sources" });
    await expect(sources.getByRole("listitem").filter({ hasText: "Forecast to month end" }).getByText("Modelled")).toBeVisible();
    await expect(sources.getByRole("listitem").filter({ hasText: "Cloud cost, last 30 days" }).getByText("Modelled")).toHaveCount(0);
    expect(recorded.asked[0]?.context).toEqual({ screen: "finops", record_type: null, record_id: null });
  });
});

test.describe("platform administrator", () => {
  test.use({ role: "admin" });

  test("switches AMARA on for a customer's portal users from onboarding", async ({ signedIn: page }) => {
    const sent: { tenant: string; body: EnabledRequest }[] = [];
    await page.route("**/v1/assistant/tenants/*/enabled", async (route, request) => {
      const tenant = /tenants\/([^/]+)\/enabled/.exec(new URL(request.url()).pathname)?.[1] ?? "";
      const body = request.postDataJSON() as EnabledRequest;
      sent.push({ tenant, body });
      const view: EnabledView = { tenant_id: tenant, enabled: body.enabled };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(view) });
    });
    await page.goto("/onboarding");
    await page.getByRole("table", { name: "Customers and their portal" }).getByRole("row").nth(1).getByRole("button").first().click();
    const toggle = page.getByRole("switch", { name: "AMARA assistant for portal users" });
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(page.getByText("AMARA is on for this customer’s portal users.")).toBeVisible();
    expect(sent).toHaveLength(1);
    expect(sent[0]?.body).toEqual({ enabled: true });
  });
});

// WCAG 2.1 AA with the panel open and answered, in both themes; no sideways scroll at phone width.
for (const theme of ["light", "dark"] as const) {
  test.describe(`assistant panel, ${theme} theme`, () => {
    test.use({ role: "engineer", colorScheme: theme });

    test("passes axe (WCAG 2.1 AA) and fits a phone", async ({ signedIn: page }) => {
      await mockAssistant(page, {
        session: { suggestions: ["What can AMARA help me with?"] },
        replies: [
          {
            status: "answered",
            text: "I answer read-only questions about this account.",
            sources: [{ ref: "S1", label: "Forecast", source_system: "cost_fact", as_of: AS_OF, is_modelled: true, url: null }],
          },
        ],
      });
      await page.goto("/work");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await openPanel(page);
      await panel(page).getByRole("button", { name: "What can AMARA help me with?" }).click();
      await expect(answers(page).getByText("I answer read-only questions about this account.")).toBeVisible();
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.map((v) => `assistant: ${v.id} ${v.nodes.length}`)).toEqual([]);
      await page.setViewportSize({ width: 375, height: 740 });
      const box = await panel(page).boundingBox();
      expect(box?.width).toBeGreaterThanOrEqual(370);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    });
  });
}
