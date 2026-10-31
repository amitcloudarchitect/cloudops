"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { useAuth } from "@/lib/auth";
import { visibleModules } from "@/lib/nav";
import { can, useSession } from "@/lib/session";

import { EmptyState } from "./EmptyState";
import { Loading } from "./Loading";
import { ThemeToggle } from "./ThemeToggle";

// The frame every screen sits in. Navigation lists only the modules the user's
// role can use (lib/nav.ts); the sign-in page renders without it.
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const session = useSession();
  const { signIn, signOut, state } = useAuth();

  if (pathname === "/signin") return <main id="main" className="mx-auto max-w-2xl p-6">{children}</main>;

  let content: ReactNode;
  let nav: ReactNode = null;
  let who: ReactNode = null;
  switch (session.status) {
    case "loading":
      content = <Loading what="your access" />;
      break;
    case "signed-out": {
      const failure = state.status === "signed-out" ? state.failure : undefined;
      content = failure ? (
        <EmptyState
          title="Sign-in did not complete"
          next={`Microsoft did not complete the sign-in (${failure}). Try again; if it keeps failing, quote that code.`}
          action={{ label: "Sign in again", onClick: () => void signIn() }}
        />
      ) : (
        <EmptyState
          title="Sign in to use the console"
          next="Use your Getronics account."
          action={{ label: "Sign in", onClick: () => void signIn() }}
        />
      );
      break;
    }
    case "no-access":
      content = (
        <EmptyState
          title="Your account has no access yet"
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
        />
      );
      break;
    case "error":
      content = (
        <EmptyState
          title="The console could not reach the platform"
          next={`Try again in a moment.${session.requestId ? ` Quote request ${session.requestId}.` : ""}`}
        />
      );
      break;
    case "ready": {
      const me = session.me;
      const modules = visibleModules((p) => can(me, p), me.kind === "staff");
      content = children;
      nav = (
        <nav aria-label="Modules">
          <ul className="flex flex-wrap gap-1 lg:flex-col">
            {modules.map((m) => {
              const current = pathname === m.href || pathname.startsWith(`${m.href}/`);
              return (
                <li key={m.href}>
                  <Link
                    href={m.href}
                    aria-current={current ? "page" : undefined}
                    className={`block rounded-control px-3 py-2 text-sm font-medium ${
                      current ? "bg-accent text-accent-ink" : "text-ink hover:bg-surface-raised"
                    }`}
                  >
                    {m.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
      );
      who = (
        <p className="text-sm text-muted">
          <span className="font-medium text-ink">{state.status === "signed-in" ? state.name : ""}</span>{" "}
          <span data-testid="role">{me.role}</span>
          {me.kind === "customer" ? " · Customer" : ""}
        </p>
      );
    }
  }

  return (
    <div className="min-h-screen">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-10 focus:bg-surface focus:p-2">
        Skip to content
      </a>
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-surface px-4 py-3">
        <Link href="/" className="text-lg font-semibold text-ink">
          CloudOps <span className="font-normal text-muted">Operations</span>
        </Link>
        <div className="flex flex-wrap items-center gap-4">
          {who}
          <ThemeToggle />
          {session.status === "ready" ? (
            <button
              type="button"
              onClick={() => void signOut()}
              className="rounded-control border border-line px-3 py-1 text-sm text-ink hover:bg-surface-raised"
            >
              Sign out
            </button>
          ) : null}
        </div>
      </header>
      <div className="mx-auto flex max-w-screen-2xl flex-col gap-4 p-4 lg:flex-row">
        {nav ? <aside className="lg:w-52 lg:shrink-0">{nav}</aside> : null}
        <main id="main" tabIndex={-1} className="min-w-0 flex-1">
          {content}
        </main>
      </div>
    </div>
  );
}
