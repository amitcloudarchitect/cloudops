# ADR 0014: The operations console

- **Status:** Accepted, 2026-10-30
- **Decided by:** Amit Kumar
- **Implements:** HLD, UI patterns and role-driven navigation; step 14.
  `apps/web`, `packages/contracts`, `services/api/src/cloudops_api/operations.py`,
  migration 0008 (`triage_result`, `triage_review`, permission `triage.review`).

## Decisions

1. **A browser application signing in with MSAL** (chosen over a server-side session
   in Next.js). The console is a public client: it holds no secret, gets the user's
   access token for `api://…/access_as_user` from Entra, and calls the API with it.
   The API keeps resolving scope from the token's object id alone (ADR 0002), so the
   console adds no trust path. Tokens are cached in session storage, so closing the
   tab signs out. Consequences:
   - server components render layout only; every screen that shows data is a client
     component, because only the browser holds the token;
   - the API allows CORS for configured origins only (`CLOUDOPS_WEB_ORIGINS`, exact
     `https` origins; `http://localhost:3000` in the local profile), with
     `Authorization` and `Content-Type`, `GET` and `POST`, and never credentials;
   - a local mode reads a dev token (`make token`) from session storage. Only the API
     on that machine trusts the dev issuer, so a local build pointed elsewhere signs
     nobody in. The build must name its mode; there is no default.

2. **Triage is recorded, and reviewed, in the database.** The stage 1 to 3 workflow's
   last activity (`record_outcome`) writes one `triage_result` per workflow run,
   idempotent on the workflow id. A person with `triage.review` (default: Engineer,
   Delivery lead, Platform administrator) accepts or overrides it; each decision is a
   new `triage_review` row, append-only for the application. An override names
   valid values (the classifier's own enums, carried in the API schema) and a reason;
   the database enforces the same shape. Both tables are tenant data with FORCE RLS
   and the account-scope restriction. Triage is staff-only: a customer's view of a
   ticket never carries it. No plan is shown, because planning (stage 4) is not built,
   and the panel says so.

3. **API types are generated, never written.** `python -m cloudops_api.schema` writes
   `packages/contracts/openapi.json`; `openapi-typescript` generates `src/api.ts`;
   `src/index.ts` holds aliases only, and reads the override choices from the JSON
   schema at runtime. A Python test fails when the JSON no longer matches the API; the
   CI console job fails when the TypeScript no longer matches the JSON.

4. **Role-driven navigation from `/v1/me` permissions.** A module is listed only when
   the role holds its permission (`apps/web/src/lib/nav.ts`). Each screen checks again,
   because a typed-in URL needs no link, and the API refuses regardless. Customer
   mapping (step 14b) has its place reserved and stays hidden until it exists.

5. **One design system, from one token file.** `src/styles/tokens.css` defines every
   colour, the health bands and the provenance styles, for light and dark; components
   use only the utilities it generates. Health banding goes through `lib/health.ts`
   and its single threshold table. Every number shows its source and as-of time; a
   modelled value is dashed and labelled. A figure the role may not see is "not
   available", never zero. Money arrives as a decimal string and is rounded once, on
   its digits, at display.

6. **Tables:** sort, filter, facets, CSV export (formula characters neutralised), and
   saved views: presets per role, plus the user's own views kept in this browser per
   role. Shared, server-side views wait until someone needs them across devices.

7. **Tested per role in a browser.** Playwright runs each role's critical journeys
   (engineer, customer, FinOps, platform administrator, signed-out) against the real
   API on a migrated, seeded database, at desktop and tablet widths, plus axe
   (WCAG 2.1 AA) on every screen in both themes, keyboard-only use and no sideways
   scroll at tablet width. CI runs them in the `console` job, which builds the
   production bundle first. CodeQL now analyses TypeScript too.

## Not decided here

- **Deployment.** The console is built and tested only. Hosting it (static export
  behind Front Door, or a container behind the gateway), its Entra app registration's
  SPA redirect URIs, and its CSP are a follow-up. Decided in ADR 0017.
- **Live metrics** on resource context wait for the telemetry store (LLD L1); the
  screen says so.
