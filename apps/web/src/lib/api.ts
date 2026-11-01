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
// 201 for a POST that creates; 200 for one that changes and answers with the result, or
// that computes and stores nothing (a quote); 202 for one that queues work (a draft request).
type PostOk<P extends Post> = paths[P] extends {
  post: { responses: { 201: { content: { "application/json": infer R } } } };
}
  ? R
  : paths[P] extends { post: { responses: { 200: { content: { "application/json": infer R } } } } }
    ? R
    : paths[P] extends { post: { responses: { 202: { content: { "application/json": infer R } } } } }
      ? R
      : never;
// Endpoints that answer 204 No Content.
type PostEmpty = { [P in Post]: paths[P] extends { post: { responses: { 204: unknown } } } ? P : never }[Post];
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
  if (response.status === 204) return undefined;
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
  query?: Query,
): Promise<PostOk<P>> {
  return (await send(token, "POST", url(path, params, query), body)) as PostOk<P>;
}

// Endpoints that answer with a document rather than JSON (the report pack's PDF, the
// Revenue & margin CSV).
type DocumentContent = { "application/pdf": unknown } | { "text/csv": unknown };
type Download = { [P in Get]: paths[P] extends { get: { responses: { 200: { content: DocumentContent } } } } ? P : never }[Get];

/** Fetch a document with the user's token, as a Blob the browser can save. */
export async function apiDownload<P extends Download>(
  token: string,
  path: P,
  params: Record<string, string>,
  query?: Query,
): Promise<Blob> {
  const response = await fetch(url(path, params, query), { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
  if (!response.ok) throw new ApiError(response.status, "error", response.headers.get("x-request-id"));
  return response.blob();
}

/** A POST answered with 204 No Content: it resolves to nothing, or throws ApiError. */
export async function apiPostEmpty<P extends PostEmpty>(
  token: string,
  path: P,
  params: Record<string, string>,
  body: PostBody<P>,
): Promise<void> {
  await send(token, "POST", url(path, params), body);
}
