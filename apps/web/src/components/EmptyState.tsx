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
    <div className="card mx-auto max-w-xl px-6 py-10 text-center" role="status">
      {/* The brand's circle, quiet: an empty state is calm, not an error. */}
      <span aria-hidden="true" className="mx-auto mb-4 flex size-10 items-center justify-center rounded-full bg-surface-raised">
        <span className="size-2.5 rounded-full bg-brand" />
      </span>
      <p className="section-title">{title}</p>
      <p className="mx-auto mt-1.5 max-w-md text-sm text-muted">{next}</p>
      {action?.href ? (
        <Link href={action.href} className="link mt-4 inline-block font-semibold">
          {action.label}
        </Link>
      ) : action?.onClick ? (
        <button type="button" onClick={action.onClick} className="btn btn-primary mt-5">
          {action.label}
        </button>
      ) : null}
    </div>
  );
}
