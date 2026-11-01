"use client";

// AMARA, the read-only assistant (ADR 0029): a side panel on every screen. It asks
// the API what it offers here (/session), sends a question with where the user is,
// and polls the turn through its stages to the answer. The answer is plain text,
// never HTML: it is model output over attacker-influenceable data (CLAUDE.md, rule 4).
// Every source is listed with its as-of time; a modelled one is marked as on every
// other screen.

import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";

import type { AssistantSession, ConversationSummary, SourceView, TenantOption, TurnView } from "@cloudops/contracts";

import { ApiError, apiGet, apiPost, apiPostEmpty } from "@/lib/api";
import { useAssistant, type Where } from "@/lib/assistant";
import { useAuth } from "@/lib/auth";
import { dateTime, safeHttps } from "@/lib/format";
import { useMe } from "@/lib/session";

import { ModelledMark } from "./Provenance";

export const SHORTCUT_LABEL = "Ctrl+/ or ⌘/";
export const SHORTCUT_ARIA = "Control+/ Meta+/";

const POLL_MS = 1_000;
const POLL_LIMIT_MS = 120_000;
const MAX_QUESTION = 2_000;

const STAGE: Record<NonNullable<TurnView["stage"]>, string> = {
  understanding: "Understanding your question…",
  retrieving: "Searching knowledge…",
  looking_up: "Looking up records…",
  composing: "Composing an answer…",
  checking: "Checking sources…",
};

function stageText(turn: TurnView): string {
  return turn.stage ? STAGE[turn.stage] : STAGE.understanding;
}

/** What went wrong asking, by the API's error code: never its message. */
function askFailure(error: unknown): { text: string; restart?: boolean } {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "assistant_not_enabled":
        return { text: "AMARA isn’t switched on for your organisation yet." };
      case "turn_in_progress":
        return { text: "AMARA is still answering the last question in this conversation. Wait for it, then ask again." };
      case "conversation_turn_ceiling":
        return { text: "This conversation has reached its limit of questions. Start a new conversation to carry on.", restart: true };
      case "assistant_unavailable":
        return { text: "AMARA is unavailable just now. Try again in a moment." };
      case "not_found":
        return { text: "That conversation or customer is no longer available to you. Start a new conversation.", restart: true };
      case "invalid_request":
        return { text: `Questions are 1 to ${MAX_QUESTION.toLocaleString("en-GB")} characters.` };
    }
    if (error.requestId) return { text: `Your question was not sent. Try again in a moment; quote request ${error.requestId} if it keeps failing.` };
  }
  return { text: "Your question was not sent. Try again in a moment." };
}

function loadFailure(error: unknown, what: string): string {
  if (error instanceof ApiError && error.status === 404) return `That ${what} is not available to you.`;
  const ref = error instanceof ApiError && error.requestId ? ` Quote request ${error.requestId} if it keeps failing.` : "";
  return `Could not load ${what}. Try again in a moment.${ref}`;
}

type Loaded<T> = { status: "loading" } | { status: "error"; error: unknown } | { status: "ready"; data: T };

interface Conversation {
  id: string;
  mode: AssistantSession["mode"];
  tenant: TenantOption | null;
}

// --- the answer ------------------------------------------------------------------------

function Sources({ sources }: { sources: SourceView[] }) {
  const headingId = useId();
  if (sources.length === 0) return null;
  return (
    <section aria-labelledby={headingId} className="mt-3 border-t border-line-subtle pt-2">
      <h4 id={headingId} className="text-xs font-semibold text-muted">
        Sources
      </h4>
      <ul className="mt-1 space-y-1.5">
        {sources.map((s) => {
          const href = safeHttps(s.url);
          return (
            <li key={s.ref} className="text-xs text-muted">
              <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="font-semibold text-ink">
                  {href ? (
                    <a href={href} target="_blank" rel="noopener noreferrer" className="link">
                      {s.label}
                      <span className="sr-only"> (opens in a new tab)</span>
                    </a>
                  ) : (
                    s.label
                  )}
                </span>
                {s.as_of ? (
                  <>
                    <span aria-hidden="true">·</span>
                    <span>
                      As of <time dateTime={s.as_of}>{dateTime(s.as_of)}</time>
                    </span>
                  </>
                ) : null}
                {s.is_modelled ? <ModelledMark /> : null}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Turn({ turn, timedOut, onRetry, onResume }: { turn: TurnView; timedOut: boolean; onRetry: () => void; onResume: () => void }) {
  const text = turn.answer?.text ?? "";
  let reply: ReactNode;
  if (turn.status === "pending") {
    reply = timedOut ? (
      <div className="rounded-card border border-line-subtle bg-surface-raised p-3 text-sm text-ink">
        <p>AMARA is taking longer than usual.</p>
        <button type="button" className="btn btn-secondary mt-2" onClick={onResume}>
          Check again
        </button>
      </div>
    ) : (
      <p className="flex items-center gap-2 text-sm text-muted">
        <span aria-hidden="true" className="size-2 animate-pulse rounded-full bg-accent" />
        {stageText(turn)}
      </p>
    );
  } else if (turn.status === "answered") {
    reply = (
      <div className="rounded-card border border-line-subtle bg-surface p-3">
        <p className="text-sm whitespace-pre-wrap text-ink">{text}</p>
        <Sources sources={turn.answer?.sources ?? []} />
      </div>
    );
  } else if (turn.status === "dont_know") {
    reply = (
      <div className="rounded-card border border-line bg-surface-raised p-3" data-status="dont_know">
        <p className="text-xs font-semibold text-muted">I don’t know</p>
        <p className="mt-1 text-sm whitespace-pre-wrap text-ink">{text || "I couldn’t back an answer with the platform’s data, so I won’t guess."}</p>
        <Sources sources={turn.answer?.sources ?? []} />
      </div>
    );
  } else if (turn.status === "refused") {
    reply = (
      <div className="rounded-card border border-line-subtle bg-selected p-3" data-status="refused">
        <p className="text-xs font-semibold text-muted">Outside what AMARA can answer</p>
        <p className="mt-1 text-sm whitespace-pre-wrap text-ink">{text}</p>
      </div>
    );
  } else {
    reply = (
      <div className="rounded-card border border-band-critical bg-band-critical-bg p-3" data-status="failed">
        <p className="text-sm font-semibold text-band-critical">No answer</p>
        <p className="mt-1 text-sm whitespace-pre-wrap text-ink">{text || "AMARA couldn’t answer just now."}</p>
        <button type="button" className="btn btn-secondary mt-2" onClick={onRetry}>
          Try again
        </button>
      </div>
    );
  }
  return (
    <li className="space-y-2" data-testid="assistant-turn">
      <div className="ml-6 rounded-card bg-surface-raised px-3 py-2">
        <p className="sr-only">You asked:</p>
        <p className="text-sm whitespace-pre-wrap text-ink">{turn.question}</p>
      </div>
      <div>
        <p className="sr-only">AMARA:</p>
        {reply}
      </div>
    </li>
  );
}

// --- history ---------------------------------------------------------------------------

function History({ onOpen, onDeleted, currentId }: { onOpen: (id: string) => void; onDeleted: (id: string) => void; currentId: string | null }) {
  const { token } = useAuth();
  const [list, setList] = useState<Loaded<ConversationSummary[]>>({ status: "loading" });
  const [confirming, setConfirming] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiGet(await token(), "/v1/assistant/conversations");
        if (!cancelled) setList({ status: "ready", data });
      } catch (error) {
        if (!cancelled) setList({ status: "error", error });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  async function remove(id: string) {
    setBusy(true);
    setMessage(null);
    try {
      await apiPostEmpty(await token(), "/v1/assistant/conversations/{conversation_id}/delete", { conversation_id: id }, undefined);
      setList((now) => (now.status === "ready" ? { status: "ready", data: now.data.filter((c) => c.id !== id) } : now));
      setConfirming(null);
      setMessage("Conversation deleted.");
      onDeleted(id);
    } catch (error) {
      setMessage(loadFailure(error, "conversation"));
    } finally {
      setBusy(false);
    }
  }

  if (list.status === "loading") {
    return (
      <p role="status" className="text-sm text-muted">
        Loading your conversations…
      </p>
    );
  }
  if (list.status === "error") return <p className="text-sm text-band-critical">{loadFailure(list.error, "your conversations")}</p>;
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted">Only you can see your conversations. They are kept for 90 days.</p>
      {message ? (
        <p role="status" className="text-sm text-ink">
          {message}
        </p>
      ) : null}
      {list.data.length === 0 ? (
        <p className="text-sm text-muted">No conversations yet. Ask a question to start one.</p>
      ) : (
        <ul className="divide-y divide-line-subtle" aria-label="Your conversations">
          {list.data.map((c) => (
            <li key={c.id} className="py-2">
              <div className="flex items-start justify-between gap-2">
                <button
                  type="button"
                  className="min-w-0 flex-1 rounded-control text-left hover:bg-surface-raised"
                  aria-current={c.id === currentId ? "true" : undefined}
                  onClick={() => onOpen(c.id)}
                >
                  <span className="block text-sm font-semibold break-words text-ink">{c.title}</span>
                  <span className="block text-xs text-muted">
                    {c.mode === "help" || !c.tenant ? "Platform help" : c.tenant.name} · <time dateTime={c.updated_at}>{dateTime(c.updated_at)}</time>
                  </span>
                </button>
                {confirming === c.id ? null : (
                  <button type="button" className="btn btn-secondary" onClick={() => setConfirming(c.id)} aria-label={`Delete “${c.title}”`}>
                    Delete
                  </button>
                )}
              </div>
              {confirming === c.id ? (
                <div role="group" aria-label="Confirm delete" className="mt-2 rounded-card border border-line-subtle bg-surface-raised p-2">
                  <p className="text-sm text-ink">Delete this conversation? This can’t be undone.</p>
                  <div className="mt-2 flex gap-2">
                    <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void remove(c.id)}>
                      Delete conversation
                    </button>
                    <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => setConfirming(null)}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// --- the panel -------------------------------------------------------------------------

/** The record hint goes with a question only when it belongs to the account asked about. */
function contextFor(where: Where, tenantId: string | null) {
  const sameAccount = !where.tenantId || where.tenantId === tenantId;
  return {
    screen: where.screen,
    record_type: sameAccount && where.record ? where.record.type : null,
    record_id: sameAccount && where.record ? where.record.id : null,
  };
}

export function AssistantPanel() {
  const { where, setOpen, launcher } = useAssistant();
  const { token } = useAuth();
  const me = useMe();
  const titleId = useId();
  const inputId = useId();
  const panel = useRef<HTMLElement | null>(null);
  const heading = useRef<HTMLHeadingElement | null>(null);
  const input = useRef<HTMLTextAreaElement | null>(null);
  const turnsEnd = useRef<HTMLDivElement | null>(null);

  const [view, setView] = useState<"chat" | "history">("chat");
  // Staff may choose the account: undefined follows the screen, "" is platform help.
  const [chosen, setChosen] = useState<string | undefined>(undefined);
  // The last session loaded, and which screen and account it was for: a reload keeps
  // showing the last one, but a new conversation waits for the current one.
  const [loadedSession, setLoadedSession] = useState<{ key: string; value: Loaded<AssistantSession> }>({ key: "", value: { status: "loading" } });
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [turns, setTurns] = useState<TurnView[]>([]);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [timedOut, setTimedOut] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<{ text: string; restart?: boolean } | null>(null);
  const [announcement, setAnnouncement] = useState("");

  const sessionTenant = chosen === undefined ? (where.tenantId ?? undefined) : chosen || undefined;
  const sessionKey = `${where.screen}|${sessionTenant ?? ""}`;
  const session = loadedSession.value;
  const current = loadedSession.key === sessionKey;

  // What the panel offers here.
  useEffect(() => {
    let cancelled = false;
    const [screen = "other", tenant] = sessionKey.split("|");
    (async () => {
      try {
        const data = await apiGet(await token(), "/v1/assistant/session", {}, { screen, tenant_id: tenant || undefined });
        if (!cancelled) setLoadedSession({ key: sessionKey, value: { status: "ready", data } });
      } catch (err) {
        if (!cancelled) setLoadedSession({ key: sessionKey, value: { status: "error", error: err } });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, sessionKey]);

  const available = session.status === "ready" && session.data.available;

  // Focus: the heading at once, then the question box once there is one.
  useEffect(() => {
    heading.current?.focus();
  }, []);
  useEffect(() => {
    if (!available || view !== "chat") return;
    const active = document.activeElement;
    if (active === heading.current || active === document.body || !panel.current?.contains(active)) input.current?.focus();
  }, [available, view]);

  // Poll the pending turn about once a second, for up to two minutes.
  useEffect(() => {
    if (!pendingId || timedOut) return;
    let cancelled = false;
    const started = Date.now();
    let lastStage: string | null = null;
    const timer = window.setInterval(() => {
      void (async () => {
        if (Date.now() - started > POLL_LIMIT_MS) {
          window.clearInterval(timer);
          if (!cancelled) {
            setTimedOut(true);
            setAnnouncement("AMARA is taking longer than usual.");
          }
          return;
        }
        try {
          const turn = await apiGet(await token(), "/v1/assistant/turns/{turn_id}", { turn_id: pendingId });
          if (cancelled) return;
          setTurns((now) => now.map((t) => (t.id === turn.id ? turn : t)));
          if (turn.status === "pending") {
            if (turn.stage !== lastStage) {
              lastStage = turn.stage;
              setAnnouncement(stageText(turn));
            }
            return;
          }
          window.clearInterval(timer);
          setPendingId(null);
          const text = turn.answer?.text ?? "";
          setAnnouncement(
            turn.status === "answered"
              ? `AMARA answered: ${text}`
              : turn.status === "dont_know"
                ? `AMARA doesn’t know: ${text}`
                : turn.status === "refused"
                  ? `AMARA can’t answer that: ${text}`
                  : "AMARA couldn’t answer just now.",
          );
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) {
            window.clearInterval(timer);
            if (!cancelled) {
              setPendingId(null);
              setError({ text: "That conversation is no longer available to you.", restart: true });
            }
          }
          // Anything else: a blip; the next tick tries again.
        }
      })();
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [pendingId, timedOut, token]);

  useEffect(() => {
    turnsEnd.current?.scrollIntoView?.({ block: "end" });
  }, [turns]);

  const close = useCallback(() => {
    setOpen(false);
    launcher.current?.focus();
  }, [setOpen, launcher]);

  function startNew(nextChosen?: string) {
    setConversation(null);
    setTurns([]);
    setPendingId(null);
    setTimedOut(false);
    setError(null);
    setChosen(nextChosen);
    setView("chat");
    setAnnouncement("New conversation.");
  }

  async function ask(question: string) {
    const text = question.trim();
    if (!text || pendingId || sending || session.status !== "ready" || (!conversation && !current)) return;
    if (text.length > MAX_QUESTION) {
      setError({ text: `Questions are 1 to ${MAX_QUESTION.toLocaleString("en-GB")} characters.` });
      return;
    }
    const s = session.data;
    const tenantId = conversation ? (conversation.tenant?.id ?? null) : (s.tenant?.id ?? null);
    setSending(true);
    setError(null);
    try {
      const turn = await apiPost(await token(), "/v1/assistant/questions", {}, {
        question: text,
        conversation_id: conversation?.id ?? null,
        // Staff choose the account when a conversation starts; a customer's is always its own.
        tenant_id: !conversation && me.kind === "staff" && s.mode === "account" ? tenantId : null,
        context: contextFor(where, tenantId),
      });
      if (!conversation) setConversation({ id: turn.conversation_id, mode: s.mode, tenant: s.tenant });
      setTurns((now) => [...now, turn]);
      setDraft("");
      setTimedOut(false);
      setPendingId(turn.status === "pending" ? turn.id : null);
      setAnnouncement(stageText(turn));
    } catch (err) {
      const failure = askFailure(err);
      setError(failure);
      setAnnouncement(failure.text);
    } finally {
      setSending(false);
    }
  }

  async function openConversation(id: string) {
    setError(null);
    try {
      const c = await apiGet(await token(), "/v1/assistant/conversations/{conversation_id}", { conversation_id: id });
      setConversation({ id: c.id, mode: c.mode, tenant: c.tenant });
      setTurns(c.turns);
      const pending = c.turns.find((t) => t.status === "pending");
      setTimedOut(false);
      setPendingId(pending?.id ?? null);
      setView("chat");
      setAnnouncement(`Opened conversation: ${c.title}`);
    } catch (err) {
      setError({ text: loadFailure(err, "conversation") });
      setView("chat");
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void ask(draft);
  }

  function onKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void ask(draft);
    }
  }

  function onPanelKeyDown(event: ReactKeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
    }
  }

  const busy = sending || pendingId !== null || (!conversation && !current);
  const about: ReactNode = (() => {
    if (conversation) return conversation.mode === "help" || !conversation.tenant ? "Platform help — no customer selected" : conversation.tenant.name;
    if (session.status !== "ready") return null;
    return session.data.mode === "help" || !session.data.tenant ? "Platform help — no customer selected" : session.data.tenant.name;
  })();
  const tenants = session.status === "ready" ? session.data.tenants : [];
  const selectValue = conversation ? (conversation.tenant?.id ?? "") : session.status === "ready" ? (session.data.tenant?.id ?? "") : (sessionTenant ?? "");

  let body: ReactNode;
  if (view === "history") {
    body = (
      <History
        currentId={conversation?.id ?? null}
        onOpen={(id) => void openConversation(id)}
        onDeleted={(id) => {
          if (conversation?.id === id) startNew(chosen);
        }}
      />
    );
  } else if (session.status === "loading" && turns.length === 0) {
    body = (
      <p role="status" className="text-sm text-muted">
        Loading AMARA…
      </p>
    );
  } else if (session.status === "error" && turns.length === 0) {
    body = <p className="text-sm text-band-critical">{loadFailure(session.error, "AMARA")}</p>;
  } else if (session.status === "ready" && !session.data.available) {
    body =
      session.data.unavailable_reason === "not_enabled" ? (
        <div role="status" className="rounded-card border border-line-subtle bg-surface-raised p-3 text-sm text-ink">
          <p className="font-semibold">AMARA isn’t switched on for your organisation yet.</p>
          <p className="mt-1 text-muted">Your Getronics service manager can tell you when it will be.</p>
        </div>
      ) : (
        <div role="status" className="rounded-card border border-line-subtle bg-surface-raised p-3 text-sm text-ink">
          <p className="font-semibold">AMARA isn’t available in this environment yet.</p>
          <p className="mt-1 text-muted">The platform team has not configured its AI service. Everything else in the console works as usual.</p>
        </div>
      );
  } else {
    const suggestions = session.status === "ready" ? session.data.suggestions : [];
    body = (
      <>
        {turns.length === 0 && suggestions.length > 0 ? (
          <section aria-label="Suggested questions" className="space-y-2">
            <p className="text-xs font-semibold text-muted">Try asking</p>
            <ul className="flex flex-wrap gap-2">
              {suggestions.map((q) => (
                <li key={q}>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void ask(q)}
                    className="rounded-full border border-line bg-surface px-3 py-1 text-left text-sm text-ink hover:border-ink hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-55"
                  >
                    {q}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        {turns.length > 0 ? (
          <ol className="space-y-4" aria-label="Conversation">
            {turns.map((t) => (
              <Turn
                key={t.id}
                turn={t}
                timedOut={timedOut && t.id === pendingId}
                onResume={() => setTimedOut(false)}
                onRetry={() => void ask(t.question)}
              />
            ))}
          </ol>
        ) : null}
        <div ref={turnsEnd} />
      </>
    );
  }

  return (
    <aside
      ref={panel}
      id="amara-panel"
      aria-labelledby={titleId}
      onKeyDown={onPanelKeyDown}
      className="fixed inset-y-0 right-0 z-30 flex w-full max-w-full flex-col border-l border-line-subtle bg-surface text-ink shadow-raised sm:w-[26rem]"
    >
      <div className="flex items-center justify-between gap-2 border-b border-line-subtle px-4 py-3">
        <div className="flex items-center gap-2">
          <span aria-hidden="true" className="size-2.5 rounded-full bg-brand" />
          <h2 id={titleId} ref={heading} tabIndex={-1} className="section-title">
            AMARA <span className="text-sm font-normal text-muted">assistant</span>
          </h2>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-1.5">
          {view === "chat" ? (
            <button type="button" className="btn btn-secondary" onClick={() => setView("history")}>
              History
            </button>
          ) : (
            <button type="button" className="btn btn-secondary" onClick={() => setView("chat")}>
              Back
            </button>
          )}
          <button type="button" className="btn btn-secondary" onClick={() => startNew(undefined)}>
            New
            <span className="sr-only"> conversation</span>
          </button>
          <button type="button" className="btn btn-secondary" onClick={close} aria-label="Close AMARA">
            <span aria-hidden="true">✕</span>
          </button>
        </div>
      </div>

      {view === "chat" && about ? (
        <div className="space-y-2 border-b border-line-subtle bg-canvas px-4 py-2">
          <p className="text-xs text-muted">
            About: <span className="font-semibold text-ink" data-testid="assistant-about">{about}</span>
          </p>
          {me.kind === "staff" && tenants.length > 0 ? (
            <label className="flex items-center gap-2 text-xs font-semibold text-muted">
              Customer
              <select
                className="field min-w-0 flex-1"
                value={selectValue}
                disabled={busy}
                onChange={(e) => startNew(e.target.value)}
              >
                <option value="">Platform help — no customer</option>
                {tenants.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto px-4 py-3">{body}</div>

      <p aria-live="polite" role="status" className="sr-only">
        {announcement}
      </p>

      {view === "chat" && available ? (
        <form onSubmit={submit} className="space-y-2 border-t border-line-subtle px-4 py-3">
          {error ? (
            <div role="alert" className="rounded-card border border-band-critical bg-band-critical-bg p-2 text-sm text-ink">
              <p>{error.text}</p>
              {error.restart ? (
                <button type="button" className="btn btn-secondary mt-2" onClick={() => startNew(chosen)}>
                  New conversation
                </button>
              ) : null}
            </div>
          ) : null}
          <label htmlFor={inputId} className="sr-only">
            Ask AMARA a question
          </label>
          <textarea
            id={inputId}
            ref={input}
            className="field block max-h-40 min-h-16 w-full resize-y"
            rows={2}
            maxLength={MAX_QUESTION}
            value={draft}
            placeholder="Ask about this screen…"
            aria-describedby={`${inputId}-hint`}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <div className="flex items-center justify-between gap-2">
            <p id={`${inputId}-hint`} className="text-xs text-muted">
              Enter sends; Shift+Enter adds a line.
            </p>
            <button type="submit" className="btn btn-primary" disabled={busy || draft.trim() === ""}>
              {sending || pendingId ? "Answering…" : "Send"}
            </button>
          </div>
        </form>
      ) : error && view === "chat" ? (
        <div role="alert" className="border-t border-line-subtle px-4 py-2 text-sm text-band-critical">
          {error.text}
        </div>
      ) : null}

      <p className="border-t border-line-subtle bg-canvas px-4 py-2 text-xs text-muted">AMARA is read-only and can be wrong. Check the sources.</p>
    </aside>
  );
}
