import Link from "next/link";
import type { ReactNode } from "react";

// An empty state says what to do next, not only that there is nothing.
export function EmptyState({
  title,
  next,
  action,
}: {
  title: string;
  next: ReactNode;
  action?: { label: string; href?: string; onClick?: () => void };
}) {
  return (
    <div className="rounded-card border border-dashed border-line bg-surface p-6 text-center" role="status">
      <p className="font-semibold text-ink">{title}</p>
      <p className="mt-1 text-sm text-muted">{next}</p>
      {action?.href ? (
        <Link href={action.href} className="mt-3 inline-block font-medium text-accent underline">
          {action.label}
        </Link>
      ) : action?.onClick ? (
        <button
          type="button"
          onClick={action.onClick}
          className="mt-3 rounded-control bg-accent px-3 py-1.5 font-medium text-accent-ink"
        >
          {action.label}
        </button>
      ) : null}
    </div>
  );
}
