"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useAuth } from "./auth";

export type Loaded<T> =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ready"; data: T };

/** Load with the user's token; re-runs when `key` changes. A reload of the same
 * key keeps showing the current data until the new data arrives. */
export function useApi<T>(load: (token: string) => Promise<T>, key: string): Loaded<T> & { reload: () => void } {
  const { token } = useAuth();
  const [state, setState] = useState<Loaded<T>>({ status: "loading" });
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound((n) => n + 1), []);
  const shownKey = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (shownKey.current !== key) setState({ status: "loading" });
    (async () => {
      try {
        const data = await load(await token());
        if (!cancelled) {
          shownKey.current = key;
          setState({ status: "ready", data });
        }
      } catch (error) {
        if (!cancelled) setState({ status: "error", error });
      }
    })();
    return () => {
      cancelled = true;
    };
    // `load` is a fresh closure each render; `key` names what it loads.
  }, [key, round, token]);

  return { ...state, reload };
}
