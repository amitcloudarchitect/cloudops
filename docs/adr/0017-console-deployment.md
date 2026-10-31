# ADR 0017: Deploying the operations console

- **Status:** Accepted, 2026-10-31
- **Decided by:** Amit Kumar
- **Implements:** ADR 0014 "Not decided here" (deployment, redirect URIs, CSP).
  `infra/docker/web.Dockerfile`, `docker-bake.hcl` target `web`, `infra/gitops/base/web`,
  `infra/gitops/base/edge`, stack `06-edge` (`route-console-static`),
  `apps/web/src/{proxy.ts,lib/csp.ts,lib/config.ts,app/healthz}`.

## Context

Step 14 built and tested the console but left its hosting open. The console is an
MSAL browser application (ADR 0014). It holds no secret and calls the API with the
user's token. Dev already has a signed image pipeline, Argo CD, a Gateway API ingress
(ADR 0011) and one Front Door endpoint.

## Decisions

1. **A container behind the existing gateway** (chosen over a static export behind
   Front Door). Next.js `standalone` runs on distroless Node as UID 65532, with a
   read-only root filesystem and every capability dropped. Its only writable path is
   Next's cache, an `emptyDir`. CI builds, scans and writes an SBOM for it; `main`
   pushes and signs it with the others; the release bot bumps its dev tag. It is an
   Argo Rollouts canary like the API (10% for 15 minutes, judged on the gateway's
   5xx rate and p95 for `web-canary`), with an HPA (2 to 4) and a PDB. It runs in
   `platform-web` on the app pool. A static export was rejected: it needs a second
   publishing path outside the signed images and GitOps, and Next's response
   headers, the CSP included, would have to move into Front Door rules.

2. **One host for the console and the API.** Both are served from
   `https://cloudops-dev-dne7dpdpfrgfccej.z02.azurefd.net`. The API's HTTPRoute keeps
   `/v1` and `/healthz`; the console's route (`platform-web/web`) takes `/` as a
   prefix. Gateway API gives the longer match precedence, so API paths cannot be
   shadowed, and a test checks the split. The console's route has no open path:
   every rule needs our Front Door's `X-Azure-FDID` (lock 2, ADR 0011). The gateway
   now admits routes from `platform-api` and `platform-web` only.
   Front Door's existing `route-all` carries the pages. A new route,
   `route-console-static`, caches `/_next/static/*` (content-hashed and immutable) at
   the edge. Nothing else is cached. The console's calls to the API are same-origin.
   The API's `CLOUDOPS_WEB_ORIGINS` still names the origin, so CORS stays correct if
   the console ever moves to its own host.

3. **Sign-in through `app-cloudops-web-dev`** (`019f8f28-8c02-4eb7-83ef-f8c80a870ff8`),
   the registration step 10 created, and not the API's own registration. It is a
   public client with no secret, and assignment is required. It already holds
   delegated permissions to the API's scopes, and the API pre-authorises it, so users
   see no consent prompt. The console asks for
   `api://f037f7b8-38cc-4add-8aa7-254b4a117e5d/access_as_user`. The API's registration
   remains a resource only. The redirect URI is the Entra work below.

4. **One image, configured at runtime.** The build fixes only the sign-in mode
   (`NEXT_PUBLIC_AUTH_MODE=entra`; the Dockerfile refuses anything else, so the
   local dev-token path is not in any image). The client id, directory, API scope and
   API base come from the pod's environment (`CLOUDOPS_WEB_*`, a generated ConfigMap).
   The server reads them per request and puts them in the page as a non-executable
   JSON element. So `promote.yml` moves the dev-built, signed image to UAT and
   production unchanged. A pod whose configuration is incomplete fails `/healthz`
   and never takes traffic.

5. **A strict CSP with a per-request nonce** (`src/proxy.ts`, `src/lib/csp.ts`). Every
   page is rendered per request, so each response gets a fresh nonce:
   ```
   default-src 'none'; script-src 'self' 'nonce-…' 'strict-dynamic';
   style-src 'self' 'nonce-…'; img-src 'self' data:; font-src 'self';
   connect-src 'self' <API origin> https://login.microsoftonline.com;
   manifest-src 'self'; frame-src 'none'; worker-src 'none'; object-src 'none';
   base-uri 'none'; form-action 'self'; frame-ancestors 'none'; upgrade-insecure-requests
   ```
   - `connect-src` is the API origin and Entra only, plus `'self'` for the App
     Router's own navigation requests. In dev the API origin is `'self'`.
   - No frames at all: MSAL renews tokens with the refresh token over fetch
     (`CacheLookupPolicy.AccessTokenAndRefreshToken`) and otherwise redirects. It
     never uses its hidden iframe, which the CSP would refuse.
   - There is no `'unsafe-inline'` or `'unsafe-eval'` outside `next dev`.
   - Also set: HSTS, `Cross-Origin-Opener-Policy: same-origin`, `X-Frame-Options: DENY`,
     `nosniff`, `no-referrer`, and a closed `Permissions-Policy`.
   - Playwright loads every module under the CSP and fails on any violation.

6. **No Azure identity for the console.** The pod calls nothing in Azure, and the
   browser, not the pod, talks to the API and Entra. So its ServiceAccount is
   federated to nothing, and it has no Key Vault mount and no egress (DNS aside).
   The GitOps tests name this exemption (`NO_AZURE_IDENTITY`). They check that such a
   workload has no identity at all, and that every other workload keeps its own.

7. **Two pull requests to switch it on.** The dev overlay lists the `web` image before
   it deploys `base/web`. Until `main` has pushed a `web` image there is no tag to
   run. A Rollout that cannot pull would sit in sync wave 1 and stall the whole
   platform Application, the API included. The second pull request adds `base/web`
   and its ConfigMap patch once the release bot has set the tag.

8. **Each environment bootstraps its first administrator on deployment** (added
   2026-10-31: the first sign-in to dev reached "no access yet", because nothing had
   ever created dev's administrator).
   - `bootstrap-admin` (`infra/gitops/base/api`) is a Sync hook after the migration
     job. It runs `python -m cloudops_api.bootstrap --once` as the API's identity.
   - The administrator's Entra object id is the Key Vault secret
     `platform-admin-oid`, set by a person per environment (ADR 0002 D14). It is
     mounted for this job only, not for the API.
   - `--once` treats the database's refusal "a platform administrator already
     exists" as done and exits 0, so it is safe on every sync. It still can never
     add a second administrator: the database refuses either way. Any other
     failure fails the job and the sync.
   - The API's identity already reads the whole vault (stack 04b), so this needs no
     new role assignment.
   - It lands in two pull requests, as the console did (decision 7). The job runs
     the deployed `api` image, and an image without `--once` would fail the hook on
     every sync. The job is added once the release bot has deployed an image that
     has the flag.

9. **A failed sign-in says why** (added 2026-10-31). Before this, a failed return
   from Entra was swallowed, and the user saw the same "Sign in" button again; the
   first dev sign-in took several clicks. Now the console says "Sign-in did not
   complete" with MSAL's error code and Entra's AADSTS number. Both are identifiers,
   with no token or user detail. The full error goes to the browser console. One
   MSAL client is shared by every caller in the page, so two cannot race to consume
   the same redirect response.

## Manual steps (Key Vault, before the administrator job's first run)

Create the secret holding your Entra object id. An object id is an identifier, not a
credential, but it decides who is administrator, so it lives with the secrets:

```bash
az keyvault secret set --vault-name kv-cloudops-app-dev-weu --name platform-admin-oid --value "$(az ad signed-in-user show --query id -o tsv)"
```

Until it exists, the job waits on its volume for 5 minutes, fails, and fails that
sync; Argo CD retries.

## Manual steps (Entra; yours, not the pipeline's)

On `app-cloudops-web-dev` (`019f8f28-8c02-4eb7-83ef-f8c80a870ff8`):

1. Add a **Single-page application** platform redirect URI of exactly
   `https://cloudops-dev-dne7dpdpfrgfccej.z02.azurefd.net`. It must be the origin with
   no path and no trailing slash, because the console sends `window.location.origin`.
   The same URI is the post-logout redirect.
2. Remove the stale **Web** redirect URI
   `https://cloudops-dev-dne7dpdpfrgfccej.z02.azurefd.net/api/auth/callback/azure-ad`
   (from a server-side design that was not built). Add no client secret or
   certificate: the console is a public client using PKCE.
3. Check, without changing anything, that:
   - the API permissions include `app-cloudops-api-dev` / `access_as_user`
     (delegated), with admin consent granted;
   - `app-cloudops-api-dev` → Expose an API still lists this client id under
     Authorized client applications.
4. Enterprise application `app-cloudops-web-dev` → Users and groups: assign the
   people (or a group) who should sign in. Assignment is required, so anyone
   unassigned is refused by Entra before the console loads.
5. Leave "Allow public client flows" off. That setting is for device-code and ROPC
   flows, and a SPA redirect URI needs neither.

```bash
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications(appId='019f8f28-8c02-4eb7-83ef-f8c80a870ff8')" --headers Content-Type=application/json --body '{"spa":{"redirectUris":["https://cloudops-dev-dne7dpdpfrgfccej.z02.azurefd.net"]},"web":{"redirectUris":[]}}'
```

The command does steps 1 and 2. Steps 3 to 5 are checks and assignments in the
portal.

## Consequences

- The console fails with the API: Front Door's one origin group probes the API's
  `/healthz`, so an unhealthy API takes the console's pages down too. Either is
  useless without the other.
- A pod that reaches the browser carries the tenant id, client id and API scope.
  These are identifiers, already public in any sign-in URL.
- Pages are never cached, at the edge or in the browser (`no-store`, set by Next for
  dynamic pages); only the hashed assets are.
- UAT and production overlays include `base/web`. Their values, and their own
  redirect URIs, arrive with those environments.

## Not decided here

- A custom domain. Moving to one changes the redirect URI, `CLOUDOPS_WEB_ORIGINS`,
  `CLOUDOPS_WEB_API_BASE` and the Front Door domain together.
- Private mode (Private Link to an internal load balancer), as for the API.
