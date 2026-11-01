# ADR 0030: Home dashboards per role

- **Status:** Proposed, 2026-11-01 (accepted on merge)
- **Decided by:** Amit Kumar (decisions 2, 3 and 5); the rest follows ADR 0014 and ADR 0018
- **Implements:** HLD UI patterns and role-driven navigation (M7, M22).
  `apps/web/src/screens/Home.tsx`, `HomeWidgets.tsx`, `src/lib/dashboard.ts`,
  `src/components/Widget.tsx`, `BarTable.tsx`; `services/api/src/cloudops_api/home.py`;
  `role_class` on `/v1/me`; migration `0025_customer_report_packs`; the customer path in
  `cloudops_api.reports`.
- **Amends:** ADR 0018 D4 (where a customer role lands), ADR 0020 (customers read
  published report packs).

## Context

`/` redirected each role to its first module or its landing view, so the first screen
after sign-in answered one question. The HLD asks for an at-a-glance view per role,
built from metric cards that link to their evidence (ADR 0014). The HLD and LLD do not
say what each role's home holds. They also leave three things open: customers' access
to report packs, SLA due times (the platform holds none), and where a role lands.

## Decisions

1. **A dashboard of widgets, one per role class.** `lib/dashboard.ts` lists the widgets,
   the permission each needs, and whether it is staff work. It also holds each role
   class's default order:
   - engineer and secops: incidents and SLA risk, closest to breach, triage, changes,
     noisy CIs, posture, connectors;
   - delivery lead: SLA trend, backlog and MTTR by customer and workgroup, changes,
     connectors, CSAT, margin, mapping;
   - finops, cfo and commercial: cost, anomalies, reconciliation, platform cost, margin,
     AI spend;
   - ceo and auditor: a mix of these;
   - platform administrator: connectors, mapping queue, onboarding, AI spend, platform
     cost, triage;
   - customer: its service, tickets and SLA, price, vulnerabilities, availability,
     report pack, approvals.

   A widget whose permission the role lacks is not placed. The API refuses it anyway,
   and a refused widget says "Not available to your role", never zero. `/v1/me` gains
   `role_class`, because a role's name can be edited and its class cannot.
2. **Every role lands on its dashboard** (user). ADR 0018 D4's landing view becomes
   the dashboard's first widget, and its evidence link opens that view. "Home" is the
   first navigation entry.
3. **SLA risk is modelled** (user). The platform has ServiceNow's breached flag but no
   due time. "At risk" is an open incident that ServiceNow has not flagged, with at
   least 75% of its priority's modelled resolution target elapsed:
   - targets: P1 4 h, P2 8 h, P3 72 h, P4 120 h, P5 none;
   - measured in wall-clock time, so pauses are not known;
   - the policy is in `cloudops_api.home.SLA_TARGET_HOURS`.

   The figures carry `is_modelled` and name the platform policy as their source. The
   card is dashed and the widget states the rule. A priority with no target shows "—",
   never zero. Ingesting `task_sla` due times later replaces the model.
4. **Four summary endpoints, reuse elsewhere.** Each widget makes one call. Widgets
   that share an endpoint share one request per page load. These existing endpoints
   are reused: `/v1/performance`, `/v1/posture`, `/v1/customers`, `/v1/cost/summary`
   (the customer's price), `/v1/approvals`, `/v1/connectors/health`,
   `/v1/mapping/quarantine` and `/proposals`, `/v1/onboarding`,
   `/v1/commercial/margin` and `/v1/reports/packs`. The new endpoints are below. All
   use aggregate queries under the caller's scope, with no query per row.

   | Endpoint | Permission | Holds |
   |---|---|---|
   | `/v1/home/operations` | `tickets.read` | open incidents by priority with breached and modelled at-risk counts; the five closest to breach; changes in 7 days; noisy CIs (30 days); backlog and MTTR (30 days) by customer, and by workgroup for staff only |
   | `/v1/home/triage` | `triage.review`, staff | the latest unreviewed triage of each open ticket, routed-to-person first, then least confident (the classifier's 0.7 threshold) |
   | `/v1/home/cost` | `cost.read`, staff | Getronics' amortised cost per customer, currency and invoice month (6 months, current month to date); anomalies in 30 days; reconciliation per month: final (invoice), provisional (provider total only), unverified, not checked, no cost. The internal tenant is flagged, and its cost is the platform cost (ADR 0023) |
   | `/v1/home/ai-usage` | `cost.read`, staff | each tenant's tokens this month against the gateway's tenant quota (5 M), calls, refusals, spend per currency |

   A customer calling `/v1/home/operations` runs as `cloudops_customer` and names only
   granted columns. It gets no workgroup breakdown (`by_workgroup` is null). AI spend is
   Getronics' cost, so it follows `cost.read` and is staff only. No new permission is
   added.
5. **Customers read their published report packs** (user). Migration 0025 grants
   `reports.read` to CXO and VP or director only. It also grants column `SELECT` to
   `cloudops_customer`:
   - `report_pack`: snapshot, dates, status;
   - `report_pack_price`: the customer's price;
   - `report_narrative`: section, text and `edited_at`.

   It grants nothing about who generated, approved, published or edited a pack, no AI
   draft or its state, and nothing on `report_pack_commercial`. The existing policies
   already limit rows to published packs of the tenant, and none for a
   subscription-limited scope. The API's customer queries name only those columns. The
   portfolio and every write refuse customers. 0025 is numbered after 0021–0024 (open
   pull requests); whichever merges later rebases `down_revision`.
6. **Same design system.**
   - Metric cards carry value, label, band, source and as-of, and link to their evidence.
   - Charts are bar tables: a real `<table>` whose rows name label and value in text,
     with an `aria-hidden` SVG bar. That gives a table alternative by construction,
     needs no inline style under the CSP, and uses token colours only. Bands go through
     `lib/health.ts`; its one new threshold, AI quota used, is the gateway's own (warn
     at 80%, refuse at 100%).
   - Money stays a decimal string in its own currency, rounded once at display, and is
     never summed across currencies.
   - Gaps say why: CSAT (no survey source), availability (no telemetry store, LLD L1),
     no internal tenant.
7. **Saved layouts in the browser.** "Arrange widgets" moves and hides widgets with
   keyboard-reachable buttons. The layout is kept in `localStorage` under
   `cloudops.home.<role>`, as table saved views are (ADR 0014 D6). A layout is
   reconciled with today's permissions on load: lost widgets drop out and new ones are
   appended. "Reset to default" clears it.

## Consequences

- Tests:
  - `db/tests/test_api_home.py` (own database): every figure, cross-tenant scope, the
    customer's view without workgroups, and refusals of triage, cost and AI spend to
    customers and roles without the permission.
  - `test_reports.py` covers the customer's packs and the refused columns.
  - `apps/web/e2e/home.spec.ts`: each role's widgets in order, the modelled mark,
    evidence links, arranging, keyboard, axe in both themes, and tablet width.
  - Journeys that expected `/` to redirect now expect the dashboard.
- AMARA (the assistant session) adds a panel to `AppShell`. This change does not touch
  `AppShell`.
- Anomalies count what the FinOps job writes. PR 64 restricts it to consumption.

## Not decided here

- **Simulated data is not marked.** The platform holds no flag for a simulated tenant
  or row; ADR 0025's data arrives through the real connectors. Proposed follow-up: a
  `simulated` tenant attribute set at onboarding for test customers, shown as a badge
  on every figure from that tenant.
- **SLA due times from ServiceNow** (`task_sla`), which would replace decision 3.
- **Server-side shared layouts**, until someone needs one across devices.
- **An AI usage screen.** The widget has no evidence link because no screen holds more.
