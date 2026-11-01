# ADR 0038: Project prices from ServiceNow project tasks

- **Status:** Accepted, 2026-11-01.
- **Decided by:** Amit Kumar (project prices from ServiceNow, closed vs open);
  the coordinating session (build before the dev instance had the tables, then
  confirm against it).
- **Implements:** the user's decision of 2026-11-01: "new project price" comes from
  closed project tasks and "proposed new initiatives price" from open ones, read
  from ServiceNow, read-only. Code: `connectors/src/cloudops_connectors/servicenow`
  (`mapping.py`, `pipeline.py`, `client.py`, `workflows.py`, `__main__.py`),
  migration `0036_project_task`, `services/api/src/cloudops_api/project_prices.py`,
  `packages/devdata/src/cloudops_devdata/servicenow_fixtures.py`, the simulation
  seeder (`tools/simulation/servicenow_load.py --only`) and the recorder
  (`tools/servicenow_record.py --entity`).
- **Follows:** ADR 0004 (raw before parsed), 0009 (the read-only account), 0016
  (quarantine), 0025 (the simulation seed), 0037 (six-hourly business data).

## Context

The dashboards (v2) and the revenue table need two figures per customer and
period: the price of projects delivered and the price of initiatives proposed.
The user decided both come from ServiceNow project tasks.

On 2026-11-01 dev407774 had no Project Portfolio Management:

- `pm_project_task`, `pm_project` and `planned_task` answered 400 `Invalid table`
  to the read-only client;
- `sys_db_object` listed no table whose name starts with `pm_`.

The build started against ServiceNow's standard fields. The same day the
coordinating session (with the user's leave to change dev407774):

- activated Project Management;
- granted `svc_cloudops_read` read on project task records;
- created the seeding client `svc_cloudops_sim_seed`.

Everything below was then checked against the instance.

## What the instance holds (dev407774, 2026-11-01)

- **`state`** is an integer choice: `-5` Pending, `1` Open, `2` Work in Progress,
  `3` Closed Complete, `4` Closed Incomplete, `7` Closed Skipped.
- **Currency fields:**
  - `cost` ("Total planned cost");
  - `work_cost` ("Actual cost");
  - `budget_cost`, `capex_cost` and `opex_cost`.
- **Dates:** `opened_at`, `closed_at`, `start_date`, `end_date`, `work_start`,
  `work_end`.
- **A currency field, read with `sysparm_display_value=all`, has three halves.**
  - `value` and `display_value` are converted to the instance's reference
    currency at the instance's own rate (`39437.16`, `$39,437.16`).
  - `currency_display_value` is the amount as entered, with its code
    (`EUR;35300.00`).
  - Only the last is the transaction amount.
- **PPM holds costs.** It has no customer *price* field.
- **The read account is still read-only.** A PATCH and a POST on
  `pm_project_task` both answered 403.

## Decisions

1. **A table of its own, `cloudops.project_task`, not `work_item`.** It carries a
   price, so it is commercial. Commercial tables are separate and policy-protected
   (LLD), and `work_item` is read by engineers.
   - Keyed by `sys_id`.
   - `status` is `open` or `closed`, read from the state choice's *value*: 3, 4
     and 7 are closed. The label is kept as `state`.
   - Columns: `opened_at`, `start_at`, `end_at`, `closed_at`, and the top task.
   - `price` is `numeric(18,6)`, with `price_currency` beside it: both or neither.
2. **The price is `cost`, "Total planned cost", and the field is a setting.**
   - PPM has no price, so which cost stands for it is the business's call.
     `work_cost` ("Actual cost") is the obvious alternative for delivered work.
   - The setting is `CLOUDOPS_SERVICENOW_PROJECT_PRICE_FIELD` (default `cost`),
     checked to be a field name.
   - Each row records the field that priced it (`price_field`). Every field stays
     in bronze and in `raw_extra`, so a change is a setting and a replay, not a
     migration (tested).
3. **The amount is the one entered, with its code.**
   - The connector reads `currency_display_value` (or a stored currency string in
     `value`).
   - It never reads the converted `value`, and never pairs an amount with a
     guessed currency.
   - A task with no stated currency is stored and counted as unpriced, never
     summed as zero.
4. **The connector's existing pattern, unchanged.**
   - Raw page to bronze before parsing.
   - The company is resolved through the alias map; unmatched names are
     quarantined with a count.
   - One transaction per page commits the tasks, the quarantine and the watermark.
   - Replay and the ADR 0015 re-process cover project tasks too.
   - A replay of an older page never rolls a task back (`source_updated_at`).
5. **Every six hours** (ADR 0037, business data). Tickets stay at five minutes.
   `staging.connector_schedule` gives connector health the same interval.
6. **An instance without PPM is a supported state.**
   - The client raises `SourceTableAbsentError` on 400 `Invalid table`. It is not
     retried.
   - The run ends `source_absent`, a new `staging.ingest_run` status. Nothing is
     written and the watermark does not move.
   - Connector health shows timeliness `source_absent`, band `unknown`, reason
     `source_absent`, counted on its own and not as an outage.
   - The API's `source` field says the same, so the console shows "no project data"
     rather than zeros. Its top-level `as_of` is when a sync last succeeded.
7. **Who can read it.**
   - FORCE RLS, `tenant_isolation`, and the restrictive `commercial_visibility`.
   - It is the first commercial table a connector writes. The policy also admits
     `app.role = 'ingest'`, but only on a login that is a member of
     `cloudops_ingest`. The application login claiming `ingest` sees nothing
     (tested).
   - Per customer only (`account_scope_is_all()`), like margin.
   - A customer scope has no grant on the table at all.
8. **The API: `GET /v1/commercial/project-prices?from&to[&currency][&tenant_id]`.**
   - Staff with `commercial.read`.
   - Per customer in scope: `delivered` (closed tasks whose `closed_at` falls in
     the range) and `proposed` (tasks still open whose `opened_at` falls in it).
   - Each holds the task count, the unpriced count and amounts per transaction
     currency, unrounded.
   - Given `currency`, each task is converted at the average rate in force on its
     own date. A bucket with a priced task lacking a rate has no conversion, never
     a partial sum.
9. **A SQL read for the revenue table: `cloudops.project_services(p_first, p_last)`**,
   agreed with that session.
   - Delivered price per tenant, close day (UTC) and currency, so the caller
     converts each amount at the rate on the day it was delivered.
   - Returns `as_of` (the last successful sync) and `provisional` (true while the
     source is not `available`).
   - SECURITY INVOKER, so the table's policies decide every row. EXECUTE to the
     application only.
10. **Dev records come from the simulation (ADR 0025).**
    - The simulation's project tasks carry made-up prices in EUR. The latest two
      stay open as proposed initiatives.
    - The seeder adds them with `--only pm_project_task`, reading the company and
      groups for their sys_ids and writing nothing else.
11. **The recording is scrubbed beyond ADR 0009.**
    - A project task's short description names the customer's own resource groups
      and networks, and ServiceNow echoes it in `project` and `task`.
    - The recorder redacts it and every echo of it (tested on the recording).

## Consequences

- If the business wants actual rather than planned cost: set
  `CLOUDOPS_SERVICENOW_PROJECT_PRICE_FIELD=work_cost` and replay the entity's
  bronze.
- Dev407774 is a personal developer instance. A reclaimed instance needs PPM, the
  read grant and the seeding client again, then the seeder with
  `--only pm_project_task`.
- In environments without PPM the project-task connector runs every six hours,
  reports `source_absent`, and costs one request.
- `ServiceNowReprocess` now walks three entities. One with no bronze pages costs a
  listing.
