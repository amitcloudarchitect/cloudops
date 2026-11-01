# ADR 0038: Home is five sections, over a date range, compared side by side

- **Status:** Proposed, 2026-11-01 (accepted on merge)
- **Decided by:** Amit Kumar (the sections, the size band rule, project prices from
  ServiceNow project tasks); the rest follows ADR 0030, ADR 0032 and ADR 0021
- **Implements:** HLD UI patterns and role-driven navigation (M7, M22); the user's monthly
  report workbook, replaced. `services/api/src/cloudops_api/home_sections.py`; migration
  `0036_size_bands`; `apps/web/src/screens/Home.tsx`, `screens/home/*`,
  `screens/SizeBands.tsx`, `lib/dashboard.ts`, `lib/range.ts`,
  `components/CompareTable.tsx`.
- **Amends:** ADR 0030 decisions 1, 2 and 7 (what Home holds, how a role lands, saved
  layouts). Its decisions 3 to 6 stand.

## Context

ADR 0030 gave each role class a dashboard of widgets, each over a fixed window (open
now, 7 days, 30 days, 6 months). The user wants Home to replace their monthly report
workbook: the same five sections for everyone allowed them, each over a date range they
choose, and two ranges side by side (two months, for example) broken down by component.
Every section follows the customer selector (ADR 0032).

## Decisions

1. **Five sections, by permission.** `lib/dashboard.ts` lists them:

   | Section | Permission | Who |
   |---|---|---|
   | Operations | `tickets.read` | staff and customers |
   | Revenue & margin | staff: `commercial.read`; a customer: `cost.read` | staff; a customer sees "Your cloud consumption" |
   | Security & compliance | `vulnerabilities.read` | staff and customers |
   | Platform | `inventory.read` | staff |
   | Top 10 customers | `tickets.read` | staff |

   A role's landing view (ADR 0018 D4) puts its section first (`/posture`: Security;
   `/work`, `/performance`: Operations) and is linked from Home's header ("Go to …"). A
   role with no section (the Approver) sees that link and nothing else. The role-class
   presets, the widget grid and "Arrange widgets" (ADR 0030 decisions 1 and 7) are
   retired: the sections are the same for every role that may see them.
2. **A date range per section, and compare.** Each section has its own range (default:
   this month to date; presets for last month, 30 and 90 days, year to date, or custom
   dates). Both days are inclusive, in UTC; the API refuses a range backwards or longer
   than a year (`422 invalid_range`), and the console says so before asking. "Compare"
   adds a second range (default: the same days of the month before, or the same length
   just before) and calls the same endpoint again: every component then shows both
   figures and the change, signed, in its own unit. No endpoint knows about compare.
3. **Scope.** Every section endpoint takes `ScopeDep`: staff read every customer in scope
   or the one chosen in the top bar; a customer reads its own tenant; a tenant outside
   scope is 404. Choosing a customer in the Top 10 sets the selector, which narrows every
   section.
4. **Operations** (`/v1/home/sections/operations`):
   - received: work items opened in the range by kind and priority. Incidents, requests
     (`service_request`), changes (`change_task`) and tasks (`project_task`, the only
     other kind ingested); a kind added later is listed after them, never dropped;
   - auto-triaged (staff only, ADR 0014): incidents and requests received in the range
     with a triage result, by the route of their latest one (`ready_for_planning`, or
     `human`), and their share of those received;
   - auto-remediated: **0, with `available: false` and the reason
     `execution_plane_not_built`**, shown as "not yet available", never hidden;
   - SLA attainment, **modelled** on ADR 0030's targets (P1 4 h, P2 8 h, P3 72 h, P4
     120 h, P5 none): an incident resolved in the range is met when ServiceNow's resolve
     time (else wall clock) is within its priority's target. ServiceNow's own breach flag
     is shown apart.
5. **Revenue & margin** (`/v1/home/sections/revenue`) **computes no revenue of its
   own** (coordinating session, 2026-11-01). Staff figures come from two sibling
   sources, read once each is merged:
   - hosting cost, managed services revenue and cost, margin and the ACV figures from
     the revenue table, `GET /v1/commercial/revenue` (`commercial.read`, staff,
     `ScopeDep`, with its own compare range and reporting currency);
   - new project price (closed project tasks) and proposed initiatives (open ones)
     from the ServiceNow project task ingest.

   Until then the staff view lists each component, names its source and says "No data
   source yet" (`revenue` and `projects`: `available: false`,
   `reason: no_data_source`); it never shows a figure of this module's. A staff role
   without `commercial.read` is refused the section (ADR 0002 D10). A customer gets
   `customer_price` only: its consumption price per closed month, provider and currency,
   through `cloudops.customer_monthly_price` (ADR 0021), converted at each day's rate when
   a reporting currency is chosen. No cost, margin or project figure, whatever its role
   holds.
6. **Security & compliance** (`/v1/home/sections/security`): vulnerabilities open at the
   range's end, by severity and by age at that end (the posture view's bands, ADR 0036),
   found and fixed in the range. Staff with `assurance.read` also get each customer's
   latest assessment opened by the end, scored as it stands now (ADR 0020), and their
   mean. Staff get CoreStack's security events (detected in the range, open at the end,
   by severity) and compliance findings open at the end; `cloudops_customer` has no grant
   on that source, so a customer gets neither.
7. **Platform** (`/v1/home/sections/platform`, staff): connector health now (the console
   reuses `/v1/connectors/health`, with `connectors.read`); customers onboarded by the
   range's end (not a prospect or exited, internal tenants counted apart), by cloud
   (a customer on both counts in each); and each customer's **size band** (user rule):
   - the higher of a spend band and a resource band;
   - spend: the last whole calendar month the range reaches, amortised cost converted
     to the policy's currency (EUR) at each day's rate; Large above €50,000 a month,
     Medium from €10,000, Small below. Without `cost.read`, no rate, or no cost that
     month, the spend band is absent and says why; the resources decide alone;
   - resources: inventory records live at the range's end; Large above 1,000, Medium
     from 200, Small below.
8. **The thresholds are editable** (migration 0036). `cloudops.size_band_policy` holds
   versions, never edited (a trigger refuses UPDATE, DELETE and TRUNCATE); the latest is
   in force. Platform-wide, like `fx_rate`: FORCE RLS, staff scopes read, no grant to
   `cloudops_customer`. `access.set_size_bands` (SECURITY DEFINER, owned by
   `cloudops_access_admin`) is the only write, and requires the new matrix permission
   `sizebands.manage` held as staff; the Platform administrator holds it by default.
   Settings → Size bands shows the thresholds and their history to staff and the form to
   holders; every change is in `audit.action_log`.
9. **Top 10 customers** (`/v1/home/sections/top-customers`, staff): non-internal
   customers in scope, least healthy first (then by name), by a **modelled health
   score** as at the range's end, `cloudops_api.home_sections.HEALTH_POLICY`: 100, less 20
   per open P1 and 10 per open P2 incident (at most 40), up to 20 for the share of
   critical resources in an unavailable state in inventory, 2 per open severity 4 or 5
   vulnerability (at most 20) and 5 per open high-severity security event (at most 20).
   Healthy from 80, watch from 60, at risk from 40, else critical. Each part is returned
   with what was observed and its penalty; a part not observed (no critical resources,
   or not permitted) costs nothing and says so.

   Ranking by revenue (largest first) sorts by the revenue table's
   `current.reported.total_revenue` once it is served; `rank_customers` already orders
   by it. Until then a commercial role is told "no data source yet" (`revenue`:
   pending), and any other role sees nothing about revenue.
10. **Same design system** (ADR 0014, ADR 0030 decision 6). Metric cards and
    `CompareTable`: a real table, a row per component and a column per range, the change
    in words, wrapping rather than scrolling at tablet width. Modelled figures carry the
    modelled mark; a missing figure says why. Every figure keeps its source and as-of.

## Consequences

- `/v1/home/operations`, `/triage`, `/cost` and `/ai-usage` (ADR 0030) stay served and
  tested, but Home no longer calls them. Removing them, or reusing them in the
  Observability page, is a later decision.
- Tests: `services/api/tests/test_home_sections.py` (range, size band boundaries, health
  score and bands, ordering); `db/tests/test_api_home_sections.py` (every section's
  figures, spend in two currencies, range and compare, scope and selector narrowing,
  customer versus staff, the pending revenue sources, refusals, the size band table's
  policies and immutability, the setting's permission and audit);
  `apps/web/e2e/home.spec.ts` (each role's sections, compare, range errors, pending
  revenue, size band setting, top 10 drill-down, axe in both themes at tablet width);
  portal journeys follow the landing link instead of a lead widget.
- When the revenue table and the project task ingest merge, this change is rebased on
  them: the staff Revenue & margin view and the top 10's revenue ranking read them, and
  `SourcePending` becomes their figures.

## Not decided here

- **Revenue and project prices.** Their figures, currencies and conversion belong to
  the revenue table and the project task ingest. Until they are served, absent.
- **Availability from telemetry.** The health score reads inventory state; ClickHouse
  availability (LLD L1) replaces it when it is captured.
- **SLA due times from ServiceNow** (`task_sla`), which would replace the modelled
  attainment, as ADR 0030 already notes.
- **Server-side saved ranges.** A section's range lasts the visit.
