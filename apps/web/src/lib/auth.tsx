"use client";

// Sign-in (ADR 0014). Entra through MSAL in the browser: the console is a public
// client, holds no secret, and sends the user's access token to the API, which
// resolves scope from the token's object id alone. Local mode reads a dev token
// (make token) from session storage; the API outside this machine refuses it.

import {
  AuthError,
  CacheLookupPolicy,
  InteractionRequiredAuthError,
  PublicClientApplication,
  type AccountInfo,
} from "@azure/msal-browser";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { authMode, config } from "./config";

export const LOCAL_TOKEN_KEY = "cloudops.dev-token";

type AuthState =
  | { status: "loading" }
  // failure: why the last sign-in did not complete, as MSAL's code and Entra's
  // AADSTS number; identifiers only, shown so the user can quote them.
  | { status: "signed-out"; failure?: string }
  // objectId is shown, never sent: a user with no access gives it to whoever
  // assigns their role. Scope is always the API's, from the verified token.
  | { status: "signed-in"; name: string; objectId: string | null };

interface Auth {
  state: AuthState;
  token: () => Promise<string>;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<Auth | null>(null);

// One client per page, shared by every caller: two callers creating their own would
// each handle the redirect response, and the second finds it already consumed.
let msal: Promise<PublicClientApplication> | null = null;
let redirectFailure: string | undefined;

export function describeFailure(error: unknown): string {
  if (error instanceof AuthError) {
    const aadsts = /AADSTS\d+/.exec(error.errorMessage)?.[0];
    return aadsts ? `${error.errorCode} (${aadsts})` : error.errorCode;
  }
  return "unknown_error";
}

function entraClient(): Promise<PublicClientApplication> {
  msal ??= (async () => {
    const { entra } = config();
    const client = new PublicClientApplication({
      auth: {
        clientId: entra.clientId,
        authority: `https://login.microsoftonline.com/${entra.tenantId}`,
        redirectUri: window.location.origin,
        postLogoutRedirectUri: window.location.origin,
      },
      // Tokens live for the tab only; a closed tab signs out.
      cache: { cacheLocation: "sessionStorage" },
    });
    await client.initialize();
    try {
      const result = await client.handleRedirectPromise();
      if (result?.account) client.setActiveAccount(result.account);
    } catch (error) {
      // A failed return from Entra (a code it would not redeem, say) leaves the user
      // signed out; say why rather than show the same button again.
      redirectFailure = describeFailure(error);
      console.error("Sign-in did not complete:", error);
    }
    return client;
  })().catch((error: unknown) => {
    msal = null; // initialisation failed: let the next call try again
    throw error;
  });
  return msal;
}

function activeAccount(client: PublicClientApplication): AccountInfo | null {
  return client.getActiveAccount() ?? client.getAllAccounts()[0] ?? null;
}

function localToken(): string | null {
  try {
    return window.sessionStorage.getItem(LOCAL_TOKEN_KEY);
  } catch {
    return null;
  }
}

function claims(token: string): Record<string, unknown> {
  try {
    const part = token.split(".")[1] ?? "";
    return JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/"))) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function claimName(token: string): string {
  const name = claims(token).name;
  return typeof name === "string" ? name : "Local user";
}

function claimOid(token: string): string | null {
  const oid = claims(token).oid;
  return typeof oid === "string" ? oid : null;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const mode = authMode();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (mode === "local") {
        const token = localToken();
        if (!cancelled) {
          setState(token ? { status: "signed-in", name: claimName(token), objectId: claimOid(token) } : { status: "signed-out" });
        }
        return;
      }
      const client = await entraClient();
      const account = activeAccount(client);
      if (!cancelled) {
        setState(
          account
            ? { status: "signed-in", name: account.name ?? account.username, objectId: account.localAccountId || null }
            : { status: "signed-out", failure: redirectFailure },
        );
      }
    })().catch((error: unknown) => {
      console.error("Sign-in is unavailable:", error);
      if (!cancelled) setState({ status: "signed-out", failure: describeFailure(error) });
    });
    return () => {
      cancelled = true;
    };
  }, [mode]);

  const token = useCallback(async (): Promise<string> => {
    if (mode === "local") {
      const value = localToken();
      if (!value) throw new Error("signed out");
      return value;
    }
    const client = await entraClient();
    const account = activeAccount(client);
    if (!account) throw new Error("signed out");
    const scopes = [config().entra.apiScope];
    try {
      // The cached token, else the refresh token over fetch; never the hidden iframe,
      // which the CSP forbids (src/lib/csp.ts). When the refresh token is spent, the
      // user goes round Entra again by redirect.
      const cacheLookupPolicy = CacheLookupPolicy.AccessTokenAndRefreshToken;
      return (await client.acquireTokenSilent({ scopes, account, cacheLookupPolicy })).accessToken;
    } catch (error) {
      if (error instanceof InteractionRequiredAuthError) {
        await client.acquireTokenRedirect({ scopes, account });
      }
      throw error;
    }
  }, [mode]);

  const signIn = useCallback(async () => {
    if (mode === "local") {
      window.location.assign("/signin");
      return;
    }
    try {
      const client = await entraClient();
      await client.loginRedirect({ scopes: [config().entra.apiScope] });
    } catch (error) {
      console.error("Sign-in could not start:", error);
      setState({ status: "signed-out", failure: describeFailure(error) });
    }
  }, [mode]);

  const signOut = useCallback(async () => {
    if (mode === "local") {
      window.sessionStorage.removeItem(LOCAL_TOKEN_KEY);
      window.location.assign("/signin");
      return;
    }
    const client = await entraClient();
    await client.logoutRedirect();
  }, [mode]);

  const value = useMemo(() => ({ state, token, signIn, signOut }), [state, token, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth outside AuthProvider");
  return auth;
}
