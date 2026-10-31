import type { ReactNode } from "react";

export function PageHeader({ title, subtitle, children }: { title: string; subtitle?: ReactNode; children?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4 border-b border-line-subtle pb-4">
      <div className="max-w-3xl">
        <h1 className="font-display text-[1.75rem] leading-tight font-bold text-ink">{title}</h1>
        {subtitle ? <p className="mt-1.5 text-sm text-muted">{subtitle}</p> : null}
      </div>
      {children}
    </div>
  );
}
