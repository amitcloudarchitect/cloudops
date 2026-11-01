"use client";

// Who the API says the user is: role, permissions and tenants from /v1/me.
// Navigation and every screen read permissions from here, never from the token.

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

import type { Me } from "@cloudops/contracts";

import { ApiError, apiGet } from "./api";
import { useAuth } from "./auth";
import { TENANT_ADMIN } from "./nav";

type SessionState =
  | { status: "loading" }
  | { status: "signed-out" }
  | { status: "no-access"; code: string }
  | { status: "error"; requestId: string | null }
  | { status: "ready"; me: Me };

const SessionContext = createContext<SessionState>({ status: "loading" });
// Ask /v1/me again: after a change to which customers exist (ADR 0038), so the selector
// and every list built from the session follow at once. The current view stays until then.
const ReloadContext = createContext<() => void>(() => undefined);

export function SessionProvider({ children }: { children: ReactNode }) {
  const { state, token } = useAuth();
  const [session, setSession] = useState<SessionState>({ status: "loading" });
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound((n) => n + 1), []);

  useEffect(() => {
    if (state.status === "loading") return;
    if (state.status === "signed-out") {
      setSession({ status: "signed-out" });
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const me = await apiGet(await token(), "/v1/me");
        if (!cancelled) setSession({ status: "ready", me });
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 401) setSession({ status: "signed-out" });
        else if (error instanceof ApiError && error.status === 403) setSession({ status: "no-access", code: error.code });
        else setSession({ status: "error", requestId: error instanceof ApiError ? error.requestId : null });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [state, token, round]);

  return (
    <SessionContext.Provider value={session}>
      <ReloadContext.Provider value={reload}>{children}</ReloadContext.Provider>
    </SessionContext.Provider>
  );
}

export function useReloadSession(): () => void {
  return useContext(ReloadContext);
}

export function useSession(): SessionState {
  return useContext(SessionContext);
}

/** The signed-in user's view; only for components rendered inside a ready session. */
export function useMe(): Me {
  const session = useSession();
  if (session.status !== "ready") throw new Error("useMe outside a ready session");
  return session.me;
}

export function can(me: Me, permission: string): boolean {
  if (permission === TENANT_ADMIN) return me.tenant_admin;
  return permission === "authenticated" || me.permissions.includes(permission);
}
