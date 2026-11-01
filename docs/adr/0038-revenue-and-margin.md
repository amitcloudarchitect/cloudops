# ADR 0038: Revenue & margin, the monthly report's sheet per customer

- **Status:** Accepted, 2026-11-01
- **Decided by:** Amit Kumar (the monthly report workbook's Revenue & margin sheet; "project
  prices from ServiceNow project tasks"; "spend plus resource count")
- **Amends:** ADR 0035 (one more SOW field)
- **Implements:** HLD M11–M12 (revenue and margin reporting); migration
  `0036_revenue_margin` (expand), `cloudops_commercial.revenue`, `cloudops_api.revenue`
  (`/v1/commercial/revenue`, `/v1/commercial/revenue.csv`), the console's Commercial →
  Revenue & margin (`/revenue`) and the CSP field in Settings → Commercial setup.

## Context

The user's monthly report workbook has a Revenue & margin sheet with one row per customer
and these columns: Sl No, customer name, CSP, average monthly cloud consumption, monthly
manage revenue, manage cost (actual), gross margin on manage revenue (%), professional
services (one-time), hosting ACV and manage ACV, with totals. The platform is to replace
it. The workbook's names and figures are real and never enter this public repository.

Almost everything is already held. The SOW has the monthly manage revenue, hosting and
manage ACV and professional services (0001, entered through Settings since ADR 0035).
`margin_snapshot` has revenue, actual and modelled cost per month. `cost_fact` has
consumption. Only whether Getronics resells the customer's cloud was missing.

## Decisions

1. **One new field: `sow.csp`**, a nullable boolean on the SOW version. Null means *not
   recorded*, never "no". It is part of the version: `sow_history` refuses to edit it, and
   a change is an amendment. `access.record_sow` is replaced with the same signature to
   take it. There is no new table. The column inherits the SOW's FORCE RLS, tenant isolation,
   the restrictive `commercial_visibility` policy, and the missing grant to
   `cloudops_customer`.

2. **Where each column comes from** (`cloudops_commercial.revenue`):
   - **Average monthly cloud consumption:** amortized `consumption` charge-class cost (HLD
     D8) over the range, divided by the months in it.
   - **Monthly manage revenue, manage cost, gross margin:** each month's latest margin
     snapshot, averaged over the months that have one.
     - A month the engine marked *not captured* is left out, and the gap is named
       (`partial_range`).
     - Cost is the actual cost. A month whose band fell back to modelled cost (effort
       incomplete) contributes its modelled cost, and the figure is marked modelled.
     - The percentage is the range's margin over its revenue.
   - **Professional services, hosting ACV, manage ACV:** from the SOW version in force at
     the end of the range, else the latest that overlaps it.

3. **ACVs are entered or derived, and a derived one is modelled.**
   - An ACV entered on the SOW is used as entered.
   - If not entered:
     - **Manage ACV** = 12 × the SOW's monthly manage revenue.
     - **Hosting ACV** = 12 × average monthly consumption × (1 + the SOW's consumption
       uplift), for a CSP customer only.
   - A non-CSP customer, one whose CSP flag is not recorded, or one without an uplift gets
     no hosting ACV, with the reason named. The value is never guessed.

4. **Professional services is the SOW's figure for now.** The user decided that project
   prices come from ServiceNow project tasks. When the `project-tasks` ingest lands, this
   column reads it instead, and the screen's note changes. Until then the column shows what
   commercial entered on the SOW.

5. **Currency** (CLAUDE.md rule 5).
   - A row is in its contract currency. Each figure is converted at the rate in force on its
     transaction date:
     - consumption at the daily average rate in force on each usage date;
     - revenue, cost and margin at their month's average rate (as margin converts),
       after margin was computed in the contract currency;
     - SOW figures at the rate in force when the version took effect.
   - The same rules convert into a reporting currency.
   - The margin percentage is the contract currency's, in every view.
   - A missing rate leaves the figure absent with `no_fx_rate`, never a 1 or a 0.
   - Nothing is rounded in the API. The console rounds once. The CSV is presentation, so it
     rounds money to two places and percentages to one.

6. **Ranges and comparison.**
   - `from`/`to` (at most 24 months; the default is the latest closed month).
   - Optionally `compare_from`/`compare_to`.
   - The API returns each block, and the change (current minus compare; the percentage in
     ratio points), so the console never does arithmetic on money.
   - Totals are per contract currency and, given a reporting currency, in it. Each says how
     many customers each figure leaves out.

7. **The endpoint is shared.** The Home dashboard's revenue section and top-10 revenue
   (`dashboards-v2`) read `/v1/commercial/revenue` and sort rows by
   `current.reported.manage_revenue`. They do not build a second revenue query.

8. **Who sees it:** `commercial.read`, staff only. The matrix refuses customer roles and
   engineers. Were it wrong, `commercial_visibility` returns no SOW or margin row, and a
   customer scope is refused the tables outright (ADR 0002 D10, ADR 0018). A
   subscription-limited scope reads no consumption here, as it reads no margin (D16).

## Consequences

- The workbook's sheet can be produced from the platform for any range, in any of the
  reporting currencies, with every gap in words.
- Seeded customers have no CSP flag, so their hosting ACV reads "not recorded" until
  commercial records an amendment. The dev example for Azure Sponsorship is in
  `docs/commercial-setup.md` and is entered through the page.
- Size bands (spend plus resource count) are the dashboards' concern and are not computed
  here.
- Follow-up: read professional services from ServiceNow project tasks once they are
  ingested.
