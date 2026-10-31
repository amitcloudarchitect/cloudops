"use client";

import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import { BrandLogo } from "@/components/BrandLogo";

import { LOCAL_TOKEN_KEY, useAuth } from "@/lib/auth";
import { authMode } from "@/lib/config";

// Entra: straight to Microsoft's sign-in. Local: paste a dev token, which only
// the API on this machine accepts (make token).
export function SignIn() {
  const { signIn } = useAuth();
  const mode = authMode();
  const [token, setToken] = useState("");

  useEffect(() => {
    if (mode === "entra") void signIn();
  }, [mode, signIn]);

  if (mode === "entra") {
    return (
      <Frame>
        <p role="status" className="text-sm text-muted">
          Redirecting to Microsoft sign-in…
        </p>
      </Frame>
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    window.sessionStorage.setItem(LOCAL_TOKEN_KEY, token.trim());
    window.location.assign("/");
  }

  return (
    <Frame>
    <form onSubmit={submit} className="card space-y-4 p-6 sm:p-8">
      <h1 className="font-display text-2xl font-bold text-ink">Local sign-in</h1>
      <p className="text-sm text-muted">
        Development only. On the devbox, mint a token for a seeded principal and paste it here (others: customer-nwl, finops-all, platform-admin):
        <code className="mt-2 block rounded-control bg-surface-raised p-2 font-mono text-xs text-ink">
          cd ~/cloudops && make token PRINCIPAL=engineer-nwl-crl
        </code>
      </p>
      <label className="flex flex-col gap-1.5 text-sm font-semibold text-ink">
        Token
        <textarea
          className="field font-mono text-xs text-ink"
          rows={5}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          required
        />
      </label>
      <button type="submit" className="btn btn-primary w-full">
        Sign in
      </button>
    </form>
    </Frame>
  );
}

// The sign-in page has no shell: the brand panel on the left (above, on a narrow
// screen), the form on the right.
function Frame({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-screen lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
      <div data-chrome className="relative flex flex-col justify-between gap-10 overflow-hidden bg-chrome p-8 text-chrome-ink lg:p-12">
        {/* The brand's circle, its one graphic element: green, large, at 20% opacity. */}
        <span aria-hidden="true" className="pointer-events-none absolute -right-24 -bottom-24 size-96 rounded-full bg-accent opacity-20" />
        <div className="self-start">
          <BrandLogo />
        </div>
        <div className="relative max-w-sm">
          <p className="font-display text-3xl leading-tight font-bold">
            CloudOps <span className="font-normal text-chrome-muted">Operations</span>
          </p>
          <p className="mt-3 text-sm text-chrome-muted">Getronics CloudOps operations console</p>
        </div>
      </div>
      <div className="flex items-center justify-center bg-canvas p-6 sm:p-10">
        <div className="w-full max-w-lg">{children}</div>
      </div>
    </div>
  );
}
