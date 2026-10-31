# Operations console

Next.js (App Router), TypeScript strict, Tailwind. Decisions: ADR 0014; deployment,
runtime configuration and the CSP: ADR 0017; the Getronics brand: ADR 0024.

## Run it locally

```bash
make up && make seed        # once: the stack, migrated and seeded
make api                    # the API on http://127.0.0.1:8000, in another terminal
cp apps/web/.env.example apps/web/.env.local
make web                    # the console on http://localhost:3000
```

Sign in at `/signin` with a dev token for a seeded principal:

```bash
make token PRINCIPAL=engineer-nwl-crl
```

(from the repository root; `make token OID=…` for any other principal.) Principals: `platform-admin`, `finops-all`, `engineer-nwl-crl`,
`customer-nwl`, `customer-fab-one-sub`, `customer-master`.

## Tests

```bash
make e2e                    # Playwright: every role's journeys, desktop and tablet, axe
npm run typecheck -w @cloudops/web
```

Playwright starts the API and the console unless they are running. First run:
`npx playwright install chromium` in `apps/web` (Linux also needs
`sudo npx playwright install-deps chromium`).

## Where things are

| Path | What |
|---|---|
| `src/styles/tokens.css` | every colour and band, light and dark: the one file a rebrand changes |
| `src/fonts/`, `src/components/BrandLogo.tsx` | the open stand-in fonts and the logo slot (ADR 0024) |
| `src/lib/nav.ts` | modules and the permission each needs |
| `src/lib/health.ts` | the one health scale and its thresholds |
| `src/components/` | metric card, provenance, health badge, table, empty states, shell |
| `src/screens/` | work queue, ticket and triage panel, resource context, customers |
| `e2e/` | journeys per role |

API types come from `@cloudops/contracts`; never write one by hand. After an API
change: `uv run python -m cloudops_api.schema > packages/contracts/openapi.json`
then `npm run generate -w @cloudops/contracts`.
