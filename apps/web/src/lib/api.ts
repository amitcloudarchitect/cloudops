// Typed calls to the API. Every path and body comes from @cloudops/contracts,
// generated from the API's OpenAPI schema; nothing here describes a shape by hand.

import type { ApiErrorBody, paths } from "@cloudops/contracts";

import { config } from "./config";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly requestId: string | null,
  ) {
    super(`${status} ${code}`);
  }
}

type Get = { [P in keyof paths]: paths[P] extends { get: unknown } ? P : never }[keyof paths];
type Post = { [P in keyof paths]: paths[P] extends { post: unknown } ? P : never }[keyof paths];

type GetOk<P extends Get> = paths[P] extends {
  get: { responses: { 200: { content: { "application/json": infer R } } } };
}
  ? R
  : never;
type PostOk<P extends Post> = paths[P] extends {
  post: { responses: { 201: { content: { "application/json": infer R } } } };
}
  ? R
  : never;
// An endpoint that takes no body takes `undefined`, and nothing is sent.
type PostBody<P extends Post> = paths[P] extends {
  post: { requestBody: { content: { "application/json": infer B } } };
}
  ? B
  : undefined;

export type Query = Record<string, string | undefined>;

function url(path: string, pathParams: Record<string, string>, query?: Query): string {
  const filled = path.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = pathParams[name];
    if (value === undefined) throw new Error(`missing path parameter ${name}`);
    return encodeURIComponent(value);
  });
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== "") search.set(key, value);
  }
  const qs = search.toString();
  return `${config().apiBase}${filled}${qs ? `?${qs}` : ""}`;
}

async function send(token: string, method: string, target: string, body?: unknown): Promise<unknown> {
  const response = await fetch(target, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    let code = response.status === 422 ? "invalid_request" : "error";
    try {
      const parsed = (await response.json()) as Partial<ApiErrorBody>;
      code = parsed.error?.code ?? code;
    } catch {
      // A body that is not the API's error shape keeps the status-derived code.
    }
    throw new ApiError(response.status, code, response.headers.get("x-request-id"));
  }
  return response.json();
}

export async function apiGet<P extends Get>(
  token: string,
  path: P,
  params: Record<string, string> = {},
  query?: Query,
): Promise<GetOk<P>> {
  return (await send(token, "GET", url(path, params, query))) as GetOk<P>;
}

export async function apiPost<P extends Post>(
  token: string,
  path: P,
  params: Record<string, string>,
  body: PostBody<P>,
): Promise<PostOk<P>> {
  return (await send(token, "POST", url(path, params), body)) as PostOk<P>;
}
