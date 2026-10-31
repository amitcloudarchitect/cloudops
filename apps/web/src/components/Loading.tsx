import { ApiError } from "@/lib/api";

import { EmptyState } from "./EmptyState";

export function Loading({ what }: { what: string }) {
  return (
    <p role="status" aria-live="polite" className="flex items-center gap-2 py-6 text-sm text-muted">
      <span aria-hidden="true" className="size-2 animate-pulse rounded-full bg-accent" />
      Loading {what}…
    </p>
  );
}

// What went wrong, without echoing anything a client should not see: the API's
// error code and request id only (CLAUDE.md, Errors).
export function LoadError({ error, what }: { error: unknown; what: string }) {
  if (error instanceof ApiError && error.status === 404) {
    return <EmptyState title={`No such ${what}`} next="It may not exist, or it is outside your customers. Check the reference, or go back to the list." />;
  }
  if (error instanceof ApiError && error.status === 403) {
    return <EmptyState title="Not available to your role" next="Ask a platform administrator if your role should include this." />;
  }
  const ref = error instanceof ApiError && error.requestId ? ` Quote request ${error.requestId} when you report it.` : "";
  return <EmptyState title={`Could not load ${what}`} next={`Try again in a moment.${ref}`} />;
}
