"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AREA_ICONS } from "@/lib/nav";
import type { Area, Module } from "@/lib/nav";

import { NavIcon } from "./NavIcon";

// The left navigation (ADR 0032, decision 6). Each area collapses on its own, and
// the whole sidebar collapses to an icon rail, the default below desktop width. Both
// are presentation only: the areas come already filtered by the role's permissions.
// The choice is kept per user in this browser.

/** Below Tailwind's lg breakpoint the rail is the default. */
const NARROW = "(max-width: 1023.98px)";

interface Prefs {
  rail?: boolean;
  collapsed: Area[];
}

function load(key: string): Prefs {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(key) ?? "null");
    if (parsed && typeof parsed === "object") {
      const p = parsed as { rail?: unknown; collapsed?: unknown };
      return {
        rail: typeof p.rail === "boolean" ? p.rail : undefined,
        collapsed: Array.isArray(p.collapsed) ? (p.collapsed.filter((a) => typeof a === "string") as Area[]) : [],
      };
    }
  } catch {
    // Storage blocked or unreadable: the defaults.
  }
  return { collapsed: [] };
}

function store(key: string, prefs: Prefs): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(prefs));
  } catch {
    // Storage blocked: the choice lasts for this page.
  }
}

function narrow(): boolean {
  try {
    return window.matchMedia(NARROW).matches;
  } catch {
    return false;
  }
}

export function SideNav({
  areas,
  pathname,
  user,
}: {
  areas: { area: Area; modules: Module[] }[];
  pathname: string;
  /** Whose preferences: the signed-in account's object id, kept in this browser only. */
  user: string;
}) {
  const key = `cloudops.nav.${user}`;
  // Rendered only once the session is ready, so always on the client.
  const [prefs, setPrefs] = useState<Prefs>(() => load(key));
  const [tip, setTip] = useState<{ label: string; top: number; left: number } | null>(null);
  const rail = prefs.rail ?? narrow();

  const update = (next: Prefs) => {
    setPrefs(next);
    store(key, next);
  };
  const toggleArea = (area: Area) =>
    update({
      ...prefs,
      collapsed: prefs.collapsed.includes(area) ? prefs.collapsed.filter((a) => a !== area) : [...prefs.collapsed, area],
    });

  // Escape dismisses a tooltip (WCAG 1.4.13).
  useEffect(() => {
    if (!tip) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setTip(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [tip]);

  // The rail shows icons only: a tooltip beside each says what it is. Fixed, so the
  // sidebar's own scrolling does not clip it.
  const tipProps = (label: string) =>
    rail
      ? {
          onMouseEnter: (e: { currentTarget: HTMLElement }) => showTip(label, e.currentTarget),
          onFocus: (e: { currentTarget: HTMLElement }) => showTip(label, e.currentTarget),
          onMouseLeave: () => setTip(null),
          onBlur: () => setTip(null),
        }
      : {};
  const showTip = (label: string, el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    setTip({ label, top: r.top + r.height / 2, left: r.right + 6 });
  };

  const railLabel = rail ? "Expand sidebar" : "Collapse sidebar";

  return (
    <aside
      data-testid="sidenav"
      data-rail={rail ? "true" : "false"}
      className={`shrink-0 border-r border-line-subtle bg-surface py-3 lg:sticky lg:top-14 lg:h-[calc(100vh-3.5rem)] lg:overflow-y-auto lg:py-5 ${
        rail ? "w-16 px-2" : "w-60 px-3"
      }`}
    >
      <nav aria-label="Modules" className="flex flex-col gap-3">
        <button
          type="button"
          onClick={() => {
            setTip(null);
            update({ ...prefs, rail: !rail });
          }}
          aria-expanded={!rail}
          aria-controls="nav-areas"
          aria-label={railLabel}
          {...tipProps(railLabel)}
          className={`flex items-center gap-2 rounded-control py-2 text-xs text-muted hover:bg-surface-raised hover:text-ink ${
            rail ? "justify-center px-0" : "px-3"
          }`}
        >
          <NavIcon name="rail" className={`size-5 ${rail ? "rotate-180" : ""}`} />
          {rail ? null : <span>{railLabel}</span>}
        </button>
        <div id="nav-areas" className={`flex flex-col ${rail ? "gap-2" : "gap-3"}`}>
          {areas.map(({ area, modules }, index) => {
            const id = `nav-${area.toLowerCase().replace(/[^a-z]+/g, "-")}`;
            // The rail lists every area's modules; collapsing an area is for the full sidebar.
            const collapsed = !rail && prefs.collapsed.includes(area);
            return (
              <div key={area} className={rail && index > 0 ? "border-t border-line-subtle pt-2" : undefined}>
                {rail ? (
                  <span id={id} className="sr-only">
                    {area}
                  </span>
                ) : (
                  <button
                    type="button"
                    onClick={() => toggleArea(area)}
                    aria-expanded={!collapsed}
                    aria-controls={`${id}-list`}
                    className="flex w-full items-center gap-2 rounded-control px-3 py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-muted hover:bg-surface-raised hover:text-ink"
                  >
                    <NavIcon name={AREA_ICONS[area]} className="size-4" />
                    <span id={id} className="flex-1">
                      {area}
                    </span>
                    <NavIcon name="chevron" className={`size-4 transition-transform ${collapsed ? "" : "rotate-90"}`} />
                  </button>
                )}
                <ul id={`${id}-list`} aria-labelledby={id} hidden={collapsed} className="flex flex-col gap-0.5">
                  {modules.map((m) => {
                    const current = m.href === "/" ? pathname === "/" : pathname === m.href || pathname.startsWith(`${m.href}/`);
                    return (
                      <li key={m.href}>
                        <Link
                          href={m.href}
                          aria-current={current ? "page" : undefined}
                          {...tipProps(m.label)}
                          className={`relative flex items-center gap-2.5 rounded-control py-2 text-sm ${
                            rail ? "justify-center px-0" : "px-3"
                          } ${current ? "bg-selected font-semibold text-ink" : "text-muted hover:bg-surface-raised hover:text-ink"}`}
                        >
                          <NavIcon name={m.icon} className="size-5" />
                          <span className={rail ? "sr-only" : "flex-1"}>{m.label}</span>
                          {/* The brand's one shape, the circle: lit for the current module. */}
                          {current ? (
                            <span
                              aria-hidden="true"
                              className={`size-2 shrink-0 rounded-full bg-brand ${rail ? "absolute top-1 right-1" : ""}`}
                            />
                          ) : null}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}
        </div>
      </nav>
      {tip ? (
        <span
          aria-hidden="true"
          data-testid="nav-tooltip"
          style={{ top: tip.top, left: tip.left }}
          className="pointer-events-none fixed z-30 -translate-y-1/2 whitespace-nowrap rounded-control bg-ink px-2 py-1 text-xs font-semibold text-surface shadow-card"
        >
          {tip.label}
        </span>
      ) : null}
    </aside>
  );
}
