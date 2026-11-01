"use client";

import { useEffect, useMemo, useState } from "react";

import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { FrameContext, WidgetCache } from "@/components/Widget";
import { useAssistantContext } from "@/lib/assistant";
import { defaultLayout, loadLayout, place, saveLayout, WIDGETS, type Layout, type Role, type WidgetId } from "@/lib/dashboard";
import { can, useMe } from "@/lib/session";

import { WIDGET_COMPONENTS } from "./HomeWidgets";

// Home is the role's dashboard (ADR 0030): an at-a-glance view of the estate, from
// widgets the role may see, in its role's default order, with the role's landing
// view (ADR 0018, D4) first. The user may hide and reorder widgets; the layout is
// kept in this browser, per role.
export function Home() {
  useAssistantContext({ screen: "home" });
  const me = useMe();
  const role: Role = useMemo(
    () => ({ kind: me.kind, roleClass: me.role_class, landing: me.landing, can: (p: string) => can(me, p) }),
    [me],
  );
  const fallback = useMemo(() => ({ order: defaultLayout(role), hidden: [] as WidgetId[] }), [role]);
  const [layout, setLayout] = useState<Layout>(fallback);
  const [editing, setEditing] = useState(false);
  // Storage is read after mount: the server render has none.
  useEffect(() => setLayout(loadLayout(me.role, role)), [me.role, role]);

  const update = (next: Layout) => {
    setLayout(next);
    saveLayout(me.role, next);
  };
  const reset = () => {
    setLayout(fallback);
    saveLayout(me.role, null);
  };
  const shown = layout.order.filter((id) => !layout.hidden.includes(id));
  const hidden = layout.order.filter((id) => layout.hidden.includes(id));

  if (layout.order.length === 0) {
    return <EmptyState title="Nothing to show for your role yet" next="Ask a platform administrator which role you need." />;
  }
  return (
    <>
      <PageHeader
        title="Home"
        subtitle={
          me.kind === "customer"
            ? "Your service at a glance. Every figure links to the screen behind it."
            : "Your estate at a glance, for your role. Every figure links to the screen behind it."
        }
      >
        <div className="flex flex-wrap gap-2">
          {editing ? (
            <button type="button" className="btn btn-secondary" onClick={reset}>
              Reset to default
            </button>
          ) : null}
          <button type="button" className={editing ? "btn btn-primary" : "btn btn-secondary"} aria-pressed={editing} onClick={() => setEditing((e) => !e)}>
            {editing ? "Done" : "Arrange widgets"}
          </button>
        </div>
      </PageHeader>
      {editing && hidden.length > 0 ? (
        <div className="card mb-4 p-4" role="group" aria-label="Hidden widgets">
          <p className="section-title mb-2">Hidden</p>
          <ul className="flex flex-wrap gap-2">
            {hidden.map((id) => (
              <li key={id}>
                <button
                  type="button"
                  className="btn btn-secondary px-2 py-1 text-xs"
                  onClick={() => update({ ...layout, hidden: layout.hidden.filter((h) => h !== id) })}
                >
                  Show {WIDGETS[id].title}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {shown.length === 0 ? (
        <EmptyState title="Every widget is hidden" next="Use Arrange widgets to show them again, or reset to your role's default." plain />
      ) : null}
      <WidgetCache>
        <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
          {shown.map((id, i) => {
            const Widget = WIDGET_COMPONENTS[id];
            const step = (by: -1 | 1) => {
              const beside = shown[i + by];
              if (beside) update({ ...layout, order: place(layout.order, id, beside, by) });
            };
            return (
              <FrameContext.Provider
                key={id}
                value={{
                  editing,
                  first: i === 0,
                  last: i === shown.length - 1,
                  onUp: () => step(-1),
                  onDown: () => step(1),
                  onHide: () => update({ ...layout, hidden: [...layout.hidden, id] }),
                }}
              >
                <Widget />
              </FrameContext.Provider>
            );
          })}
        </div>
      </WidgetCache>
    </>
  );
}
