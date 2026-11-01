"use client";

// What a dashboard panel shares (ADR 0030, ADR 0038): its own loading and refusal
// states, so one slow or refused panel never blanks the page; a gap said in words,
// never drawn as zero; its source line; and one request per endpoint per visit.

import { createContext, useContext, useRef, type ReactNode } from "react";

import { ApiError } from "@/lib/api";
import { useApi, type Loaded } from "@/lib/useApi";

import { Provenance, type AnyMetric } from "./Provenance";

// Widgets that read the same endpoint share one request per visit to the page.
const CacheContext = createContext<Map<string, Promise<unknown>> | null>(null);

export function WidgetCache({ children }: { children: ReactNode }) {
  const cache = useRef(new Map<string, Promise<unknown>>());
  return <CacheContext.Provider value={cache.current}>{children}</CacheContext.Provider>;
}

export function useShared<T>(key: string, load: (token: string) => Promise<T>): Loaded<T> {
  const cache = useContext(CacheContext);
  return useApi((token) => {
    if (!cache) return load(token);
    let pending = cache.get(key) as Promise<T> | undefined;
    if (!pending) {
      pending = load(token);
      cache.set(key, pending);
      // A failed load is not remembered: the next widget, or a revisit, asks again.
      pending.catch(() => cache.delete(key));
    }
    return pending;
  }, key);
}

/** A widget's body once its data is in; its loading and refusal states otherwise. */
export function Body<T>({ loaded, what, children }: { loaded: Loaded<T>; what: string; children: (data: T) => ReactNode }) {
  if (loaded.status === "loading") {
    return (
      <p role="status" className="flex items-center gap-2 text-sm text-muted">
        <span aria-hidden="true" className="size-2 animate-pulse rounded-full bg-accent" />
        Loading {what}…
      </p>
    );
  }
  if (loaded.status === "error") {
    const e = loaded.error;
    if (e instanceof ApiError && e.status === 403) return <Gap text="Not available to your role." />;
    const ref = e instanceof ApiError && e.requestId ? ` Quote request ${e.requestId} when you report it.` : "";
    return <Gap text={`Could not load ${what}. Try again in a moment.${ref}`} />;
  }
  return <>{children(loaded.data)}</>;
}

/** Said, never drawn as zero: a figure that is not captured or not permitted. */
export function Gap({ text }: { text: string }) {
  return <p className="rounded-control border border-dashed border-line-subtle px-3 py-4 text-sm text-muted">{text}</p>;
}

/** Where a widget's figures came from and when: one line under the body. */
export function Source({ metric, note }: { metric: AnyMetric | null | undefined; note?: ReactNode }) {
  if (!metric && !note) return null;
  return (
    <div className="mt-3 flex flex-col gap-1 border-t border-line-subtle pt-2">
      {metric ? <Provenance metric={metric} /> : null}
      {note ? <p className="text-xs text-muted">{note}</p> : null}
    </div>
  );
}

/** A small figure with its label, for a row of counts inside a widget. */
export function Stat({ label, value, modelled = false }: { label: string; value: ReactNode; modelled?: boolean }) {
  return (
    <div className={`rounded-control border px-3 py-2 ${modelled ? "border-dashed border-modelled bg-modelled-bg" : "border-line-subtle"}`}>
      <p className="text-xs text-muted">
        {label}
        {modelled ? <span className="ml-1 font-semibold text-modelled">(modelled)</span> : null}
      </p>
      <p className="font-display text-xl font-bold tabular-nums text-ink">{value}</p>
    </div>
  );
}
