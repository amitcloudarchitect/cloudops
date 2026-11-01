"use client";

// The console's one table (HLD, UI patterns): sortable, filterable, saved views
// per role, export. Views hold the filter text, facet choices and sort. Each role
// has preset views; a user's own views are kept in this browser, per role.

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { EmptyState } from "./EmptyState";

export interface Column<Row> {
  key: string;
  header: string;
  /** Plain value: what sorting, filtering and export use. */
  value: (row: Row) => string | number | null;
  render?: (row: Row) => ReactNode;
  sortable?: boolean;
  className?: string;
}

export interface Facet<Row> {
  key: string;
  label: string;
  value: (row: Row) => string | null;
}

export interface View {
  name: string;
  query: string;
  facets: Record<string, string>;
  sort: { key: string; dir: "asc" | "desc" } | null;
}

interface Props<Row> {
  id: string;
  caption: string;
  rows: Row[];
  rowKey: (row: Row) => string;
  columns: Column<Row>[];
  facets?: Facet<Row>[];
  /** Views every member of the role starts with. */
  presets?: View[];
  /** Saved views are kept per role. */
  role: string;
  initial?: Partial<View>;
  empty: { title: string; next: ReactNode };
  /** Replaces the table's own export: a server-rendered file (with totals, say). */
  onExport?: () => void;
}

const BLANK: View = { name: "All", query: "", facets: {}, sort: null };

function storageKey(id: string, role: string): string {
  return `cloudops.views.${id}.${role}`;
}

function loadSaved(id: string, role: string): View[] {
  try {
    const raw = window.localStorage.getItem(storageKey(id, role));
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as View[]).filter((v) => typeof v?.name === "string") : [];
  } catch {
    return [];
  }
}

function storeSaved(id: string, role: string, views: View[]): void {
  try {
    window.localStorage.setItem(storageKey(id, role), JSON.stringify(views));
  } catch {
    // Storage blocked: views last for this page only.
  }
}

function csvCell(value: string | number | null): string {
  const text = value === null ? "" : String(value);
  // A leading formula character would execute in a spreadsheet: neutralise it.
  const safe = /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
  return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

function compare(a: string | number | null, b: string | number | null): number {
  if (a === b) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), "en", { numeric: true, sensitivity: "base" });
}

export function DataTable<Row>({
  id,
  caption,
  rows,
  rowKey,
  columns,
  facets = [],
  presets = [],
  role,
  initial,
  empty,
  onExport,
}: Props<Row>) {
  const [view, setView] = useState<View>({ ...BLANK, ...initial });
  const [saved, setSaved] = useState<View[]>([]);
  useEffect(() => setSaved(loadSaved(id, role)), [id, role]);

  const views = [BLANK, ...presets, ...saved];

  const options = useMemo(
    () =>
      Object.fromEntries(
        facets.map((f) => [
          f.key,
          [...new Set(rows.map(f.value).filter((v): v is string => !!v))].sort((a, b) => a.localeCompare(b)),
        ]),
      ),
    [facets, rows],
  );

  const shown = useMemo(() => {
    const needle = view.query.trim().toLowerCase();
    const filtered = rows.filter((row) => {
      for (const facet of facets) {
        const chosen = view.facets[facet.key];
        if (chosen && facet.value(row) !== chosen) return false;
      }
      if (!needle) return true;
      return columns.some((c) => String(c.value(row) ?? "").toLowerCase().includes(needle));
    });
    const sort = view.sort;
    const column = sort ? columns.find((c) => c.key === sort.key) : undefined;
    if (!sort || !column) return filtered;
    const sign = sort.dir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => sign * compare(column.value(a), column.value(b)));
  }, [rows, view, columns, facets]);

  function toggleSort(key: string) {
    setView((v) => ({
      ...v,
      name: "",
      sort: v.sort?.key === key ? { key, dir: v.sort.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" },
    }));
  }

  function saveView() {
    const name = window.prompt("Name this view")?.trim();
    if (!name) return;
    const next = [...saved.filter((v) => v.name !== name), { ...view, name }];
    setSaved(next);
    storeSaved(id, role, next);
    setView((v) => ({ ...v, name }));
  }

  function removeView(name: string) {
    const next = saved.filter((v) => v.name !== name);
    setSaved(next);
    storeSaved(id, role, next);
  }

  function exportCsv() {
    const lines = [
      columns.map((c) => csvCell(c.header)).join(","),
      ...shown.map((row) => columns.map((c) => csvCell(c.value(row))).join(",")),
    ];
    const blob = new Blob([`${lines.join("\r\n")}\r\n`], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${id}-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  const control = "field";
  const action = "btn btn-secondary";

  return (
    <section aria-label={caption} className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs font-semibold text-muted">
          View
          <select
            className={control}
            value={views.some((v) => v.name === view.name) ? view.name : ""}
            onChange={(e) => {
              const chosen = views.find((v) => v.name === e.target.value);
              if (chosen) setView(chosen);
            }}
          >
            {views.some((v) => v.name === view.name) ? null : <option value="">Custom</option>}
            {views.map((v) => (
              <option key={v.name} value={v.name}>
                {v.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-48 flex-1 flex-col text-xs font-medium text-muted">
          Filter
          <input
            type="search"
            className={control}
            value={view.query}
            placeholder="Text in any column"
            onChange={(e) => setView((v) => ({ ...v, name: "", query: e.target.value }))}
          />
        </label>
        {facets.map((facet) => (
          <label key={facet.key} className="flex flex-col gap-1 text-xs font-semibold text-muted">
            {facet.label}
            <select
              className={control}
              value={view.facets[facet.key] ?? ""}
              onChange={(e) =>
                setView((v) => ({ ...v, name: "", facets: { ...v.facets, [facet.key]: e.target.value } }))
              }
            >
              <option value="">Any</option>
              {(options[facet.key] ?? []).map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          </label>
        ))}
        <div className="flex gap-2">
          <button type="button" className={action} onClick={saveView}>
            Save view
          </button>
          {saved.some((v) => v.name === view.name) ? (
            <button type="button" className={action} onClick={() => removeView(view.name)}>
              Delete view
            </button>
          ) : null}
          <button type="button" className={action} onClick={onExport ?? exportCsv} disabled={shown.length === 0}>
            Export CSV
          </button>
        </div>
      </div>
      <p className="text-xs text-muted" aria-live="polite">
        Showing {shown.length} of {rows.length}
      </p>
      {shown.length === 0 ? (
        <EmptyState
          title={rows.length === 0 ? empty.title : "Nothing matches this view"}
          next={rows.length === 0 ? empty.next : "Clear the filter or choose another view."}
          action={rows.length === 0 ? undefined : { label: "Show all", onClick: () => setView(BLANK) }}
        />
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full border-collapse text-left text-sm">
            <caption className="sr-only">{caption}</caption>
            <thead className="bg-surface-raised">
              <tr>
                {columns.map((c) => {
                  const sorted = view.sort?.key === c.key ? view.sort.dir : null;
                  return (
                    <th
                      key={c.key}
                      scope="col"
                      aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}
                      className={`border-b border-line px-3 py-2.5 text-[0.8125rem] font-bold text-ink ${c.className ?? ""}`}
                    >
                      {c.sortable === false ? (
                        c.header
                      ) : (
                        <button type="button" onClick={() => toggleSort(c.key)} className="inline-flex items-center gap-1">
                          {c.header}
                          <span aria-hidden="true" className="text-muted">
                            {sorted === "asc" ? "▲" : sorted === "desc" ? "▼" : "↕"}
                          </span>
                        </button>
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                <tr key={rowKey(row)} className="border-b border-line-subtle last:border-b-0 hover:bg-surface-raised">
                  {columns.map((c) => (
                    <td key={c.key} className={`px-3 py-2 align-top text-ink ${c.className ?? ""}`}>
                      {c.render ? c.render(row) : (c.value(row) ?? "—")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
