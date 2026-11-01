// Ids that go into a URL. An id is only ever put in a path once it is known to be
// a UUID, and is encoded even then, so nothing from a response, a route parameter
// or page state can turn a link into markup or another path.

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

/** `${base}/${id}` for a UUID id; null for anything else, so the caller shows no link. */
export function idPath(base: string, id: unknown): string | null {
  return isUuid(id) ? `${base}/${encodeURIComponent(id)}` : null;
}
