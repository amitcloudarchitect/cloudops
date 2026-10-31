"use client";

import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import { BrandPhoto } from "@/components/BrandPhoto";

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
    <form onSubmit={submit} className="space-y-4">
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

// The sign-in page has no shell: a Getronics background fills the screen (the
// building by day, the sunset office in dark mode), and the form sits in a card on
// its left, clear of the logo in the image's top-right corner. No text on the photo.
function Frame({ children }: { children: ReactNode }) {
  return (
    <BrandPhoto photo="building" dark="sunset" className="min-h-screen">
      <div className="flex min-h-screen items-start px-4 pt-32 pb-8 sm:px-10 lg:items-center lg:px-[6vw] lg:pt-8">
        <div className="w-full max-w-md space-y-5 rounded-card bg-surface p-6 shadow-raised sm:p-8">
          <div>
            <p className="font-display text-xl leading-tight font-bold text-ink">
              CloudOps <span className="font-normal text-muted">Operations</span>
            </p>
            <p className="mt-1 text-sm text-muted">Getronics CloudOps operations console</p>
          </div>
          <div className="border-t border-line-subtle pt-5">{children}</div>
        </div>
      </div>
    </BrandPhoto>
  );
}
