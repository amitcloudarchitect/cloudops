"use client";

// A home dashboard widget (ADR 0030): a titled card that links to the screen
// holding its evidence, with its own loading and refusal states, so one slow or
// refused widget never blanks the dashboard. While the layout is being edited, its
// move and hide controls sit in the header, reachable by keyboard.

import Link from "next/link";
import { createContext, useContext, useRef, type ReactNode } from "react";

import { ApiError } from "@/lib/api";
import { useApi, type Loaded } from "@/lib/useApi";

import { Provenance, type AnyMetric } from "./Provenance";

export interface FrameControls {
  editing: boolean;
  first: boolean;
  last: boolean;
  onUp: () => void;
  onDown: () => void;
  onHide: () => void;
}

export const FrameContext = createContext<FrameControls | null>(null);

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

export function Frame({
  id,
  title,
  href,
  wide = false,
  children,
}: {
  id: string;
  title: string;
  /** The evidence screen. Absent where no screen holds more than the widget does. */
  href?: string;
  wide?: boolean;
  children: ReactNode;
}) {
  const controls = useContext(FrameContext);
  const heading = `widget-${id}`;
  return (
    <section aria-labelledby={heading} data-widget={id} className={`card flex min-w-0 flex-col p-4 ${wide ? "xl:col-span-2" : ""}`}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <h2 id={heading} className="section-title">
          {title}
        </h2>
        {controls?.editing ? (
          <div className="flex gap-1" role="group" aria-label={`Arrange ${title}`}>
            <button type="button" className="btn btn-secondary px-2 py-1 text-xs" disabled={controls.first} onClick={controls.onUp}>
              Move up<span className="sr-only"> {title}</span>
            </button>
            <button type="button" className="btn btn-secondary px-2 py-1 text-xs" disabled={controls.last} onClick={controls.onDown}>
              Move down<span className="sr-only"> {title}</span>
            </button>
            <button type="button" className="btn btn-secondary px-2 py-1 text-xs" onClick={controls.onHide}>
              Hide<span className="sr-only"> {title}</span>
            </button>
          </div>
        ) : href ? (
          <Link href={href} className="link text-xs font-semibold">
            View the evidence<span className="sr-only"> for {title}</span>
          </Link>
        ) : null}
      </div>
      <div className="min-w-0 flex-1">{children}</div>
    </section>
  );
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
