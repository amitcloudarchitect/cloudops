"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect } from "react";
import type { ReactNode } from "react";

import { useAssistant } from "@/lib/assistant";
import { useAuth } from "@/lib/auth";
import { visibleAreas } from "@/lib/nav";
import { can, useSession } from "@/lib/session";

import { AssistantPanel, SHORTCUT_ARIA, SHORTCUT_LABEL } from "./AssistantPanel";
import { BrandLogo } from "./BrandLogo";
import { BrandPhoto } from "./BrandPhoto";
import { CustomerSelect } from "./CustomerSelect";
import { EmptyState } from "./EmptyState";
import { Loading } from "./Loading";
import { SideNav } from "./SideNav";
import { ThemeToggle } from "./ThemeToggle";

// The frame every screen sits in. Navigation lists only the modules the user's
// role can use (lib/nav.ts, SideNav.tsx); the sign-in page renders without it.
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const session = useSession();
  const { signIn, signOut, state } = useAuth();
  const assistant = useAssistant();
  const ready = session.status === "ready" && pathname !== "/signin";
  const { toggle, setOpen } = assistant;

  // Ctrl+/ (⌘/ on a Mac) opens and closes AMARA, from anywhere in the console.
  useEffect(() => {
    if (!ready) {
      setOpen(false);
      return;
    }
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key === "/") {
        event.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ready, toggle, setOpen]);
  const panelOpen = ready && assistant.open;

  if (pathname === "/signin") return <main id="main">{children}</main>;

  // Before the console opens (signed out, no access, unreachable): the state beside a
  // Getronics background, the one place inside the shell where imagery appears (ADR 0024).
  const welcome = (state: ReactNode) => (
    <div className="card mx-auto grid max-w-5xl overflow-hidden md:grid-cols-2">
      <BrandPhoto photo="atrium" className="min-h-56 md:min-h-[26rem]" />
      <div className="flex items-center justify-center p-4">{state}</div>
    </div>
  );

  let content: ReactNode;
  let nav: ReactNode = null;
  let who: ReactNode = null;
  switch (session.status) {
    case "loading":
      content = <Loading what="your access" />;
      break;
    case "signed-out": {
      const failure = state.status === "signed-out" ? state.failure : undefined;
      content = welcome(failure ? (
        <EmptyState
          title="Sign-in did not complete"
          plain
          next={`Microsoft did not complete the sign-in (${failure}). Try again; if it keeps failing, quote that code.`}
          action={{ label: "Sign in again", onClick: () => void signIn() }}
        />
      ) : (
        <EmptyState
          title="Sign in to use the console"
          plain
          next="Use your Getronics account."
          action={{ label: "Sign in", onClick: () => void signIn() }}
        />
      ));
      break;
    }
    case "no-access":
      content = welcome(
        <EmptyState
          title="Your account has no access yet"
          plain
          next={
            <>
              Getronics staff: a platform administrator assigns your role and customers. Customers: your organisation’s
              portal administrator adds you. Ask them, then sign in again.
              {state.status === "signed-in" && state.objectId ? (
                <span className="mt-2 block">
                  Give them your object id: <code className="font-mono text-ink">{state.objectId}</code>
                </span>
              ) : null}
            </>
          }
        />,
      );
      break;
    case "error":
      content = welcome(
        <EmptyState
          title="The console could not reach the platform"
          plain
          next={`Try again in a moment.${session.requestId ? ` Quote request ${session.requestId}.` : ""}`}
        />,
      );
      break;
    case "ready": {
      const me = session.me;
      const areas = visibleAreas((p) => can(me, p), me.kind === "staff");
      content = children;
      nav = (
        <SideNav
          areas={areas}
          pathname={pathname}
          user={(state.status === "signed-in" && state.objectId) || me.role}
        />
      );
      who = (
        <p className="flex flex-wrap items-center gap-2 text-sm text-chrome-muted">
          <span className="font-semibold text-chrome-ink">{state.status === "signed-in" ? state.name : ""}</span>{" "}
          <span className="rounded-full border border-chrome-line px-2 py-0.5 text-xs">
            <span data-testid="role">{me.role}</span>
            {me.kind === "customer" ? " · Customer" : ""}
          </span>
        </p>
      );
    }
  }

  return (
    <div className={`flex min-h-screen flex-col ${panelOpen ? "lg:pr-[26rem]" : ""}`}>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-20 focus:rounded-control focus:bg-surface focus:p-2 focus:text-ink"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-10 flex min-h-14 flex-wrap items-center justify-between gap-x-6 gap-y-2 border-b border-chrome-line bg-chrome px-4 py-1.5 text-chrome-ink shadow-card lg:px-6">
        <div className="flex items-center gap-3">
          <BrandLogo />
          <span aria-hidden="true" className="h-6 w-px bg-chrome-line" />
          <Link href="/" className="font-display text-base font-bold tracking-tight text-chrome-ink">
            CloudOps <span className="font-normal text-chrome-muted">Operations</span>
          </Link>
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          {ready ? <CustomerSelect /> : null}
          {who}
          <ThemeToggle />
          {ready ? (
            <button
              ref={assistant.launcher}
              type="button"
              onClick={toggle}
              aria-expanded={panelOpen}
              aria-controls={panelOpen ? "amara-panel" : undefined}
              aria-keyshortcuts={SHORTCUT_ARIA}
              title={`Ask AMARA (${SHORTCUT_LABEL})`}
              className="btn btn-primary"
            >
              <span aria-hidden="true" className="size-2 rounded-full bg-accent-ink" />
              Ask AMARA
            </button>
          ) : null}
          {session.status === "ready" ? (
            <button
              type="button"
              onClick={() => void signOut()}
              className="btn btn-secondary"
            >
              Sign out
            </button>
          ) : null}
        </div>
      </header>
      <div className="flex flex-1">
        {nav}
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 px-4 py-5 lg:px-8 lg:py-7">
          <div className="mx-auto max-w-screen-2xl">{content}</div>
        </main>
      </div>
      {panelOpen ? <AssistantPanel /> : null}
    </div>
  );
}
