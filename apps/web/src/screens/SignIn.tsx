"use client";

import { useEffect, useState } from "react";
import type { FormEvent } from "react";

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

  if (mode === "entra") return <p role="status">Redirecting to Microsoft sign-in…</p>;

  function submit(event: FormEvent) {
    event.preventDefault();
    window.sessionStorage.setItem(LOCAL_TOKEN_KEY, token.trim());
    window.location.assign("/");
  }

  return (
    <form onSubmit={submit} className="space-y-3 rounded-card border border-line bg-surface p-6">
      <h1 className="text-xl font-semibold text-ink">Local sign-in</h1>
      <p className="text-sm text-muted">
        Development only. On the devbox, mint a token for a seeded principal and paste it here (others: customer-nwl, finops-all, platform-admin):
        <code className="mt-2 block rounded-control bg-surface-raised p-2 font-mono text-xs text-ink">
          cd ~/cloudops && make token PRINCIPAL=engineer-nwl-crl
        </code>
      </p>
      <label className="flex flex-col gap-1 text-sm font-medium text-ink">
        Token
        <textarea
          className="rounded-control border border-line bg-surface p-2 font-mono text-xs text-ink"
          rows={5}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          required
        />
      </label>
      <button type="submit" className="rounded-control bg-accent px-3 py-1.5 font-medium text-accent-ink">
        Sign in
      </button>
    </form>
  );
}
