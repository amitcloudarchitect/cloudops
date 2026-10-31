import { runtimeFromEnv } from "@/lib/config";

// The pod's probe (base/web), reached by the kubelet only: Front Door's /healthz goes
// to the API (base/api, httproute.yaml). Fails while the configuration is incomplete,
// so a misconfigured pod never takes traffic.
export const dynamic = "force-dynamic";

export function GET(): Response {
  try {
    runtimeFromEnv();
  } catch {
    return new Response("misconfigured\n", { status: 503, headers: { "Cache-Control": "no-store" } });
  }
  return new Response("ok\n", { headers: { "Cache-Control": "no-store" } });
}
