# ADR 0034: Commercial setup: the SOW register through the platform, and the FinOps worker in dev

- **Status:** Accepted, 2026-11-01
- **Decided by:** Amit Kumar ("Deploy the worker (Recommended)"; the Settings area)
- **Amends:** ADR 0019 (SOW capture was a dependency; the FinOps worker had no
  deployment), ADR 0021 (decision 9: "no API writes the uplift")
- **Implements:** HLD M1 (the SOW register), M11 and M12 (what pricing and margin read);
  migration `0032_commercial_setup` (expand), `cloudops_api.commercial_setup`,
  `cloudops_connectors.finops` (`recompute_pending`, `FinOpsRecompute`, the schedules),
  `infra/gitops/base/worker-finops` and its dev overlay, stack 05's federated credential,
  and the console's Settings → Commercial setup (`/settings/commercial`). The rate card
  half of the page is ADR 0033.

## Context

Pricing, Margin and Reports were empty in dev, for three reasons:

- There was no SOW, contract revenue or rate card for any customer, and only seed scripts
  wrote them.
- Margin reads only `margin_snapshot`. Only the FinOps worker writes it, and the worker had
  no deployment.
- The console had no way to record commercial terms.

Customer roles must never read or write any of this (ADR 0002, D10).

## Decisions

1. **The SOW register is `cloudops.sow`, extended rather than replaced.**
   - New fields: the contract term (`term_start`, `term_end`), `service_scope`, the
     `recurring_fee` and its `billing_frequency` (monthly, quarterly, annual), the version
     it `amends` and why, and who entered it.
   - The monthly manage revenue that margin and the report packs already read is derived:
     the fee divided by the months in its billing period, stored at `numeric(18,6)`. No
     reader changes.
   - Existing fields are reused: the target margin (step 16: per SOW), the consumption
     uplift (ADR 0021), the hosting and manage contract values and the professional
     services value (the report pack's revenue mix), coverage and support, and
     `document_uri` for where the agreement is recorded.
   - **No separate revenue-line table.** Margin reads only the monthly figure, and the
     packs read only the three contract values.

2. **Versioned, never overwritten.**
   - A customer's SOW rows, ordered by `effective_from`, are its versions.
   - An amendment is a new row effective from a later date. The version in force is closed
     the day before, so exactly one SOW is in force on any day. That is what margin, the
     customer price (ADR 0021: two overlapping is not captured) and the packs require.
   - The trigger `sow_history` refuses every other change, for every role including the
     owner: no figure, date or reference of a version is edited. An end date may only move
     earlier, and never back to open.
   - A correction is the next amendment, with its reason.
   - Test fixtures that need other figures on a seeded SOW turn the trigger off as the
     superuser (`support.edit_sow_fixture`). The product never can.

3. **Who records: `commercial.write`, a commercial permission.**
   - CFO, Commercial and Platform administrator hold it by default. FinOps, Delivery lead,
     CEO and Auditor read the register through `commercial.read`.
   - 0004's trigger refuses the permission to any role class outside the
     `commercial_visibility` list, and so to every customer role.
   - The only write path is `access.record_sow`, SECURITY DEFINER and owned by
     `cloudops_access_admin`. It checks the matrix and the tenant again: assigned to the
     writer, and in the request's scope. A tenant outside either is "not found".
   - Every request is in `audit.action_log`, and the row records who entered it.

4. **The uplift is written by commercial now.** ADR 0021 decision 9 relied on "no API writes
   the uplift; the database check is the validation". The check
   (`sow_consumption_uplift_positive`) is still the validation. The API also refuses 0 or
   less before it reaches the database. An empty uplift is "not captured", as before.

5. **"Recompute margin now" is a request row, not a call.**
   - `cloudops.margin_recompute` holds the request: FORCE RLS, tenant isolation, the
     restrictive commercial policy, and per customer only. The API inserts it through
     `access.request_margin_recompute`. One waiting request per customer is enough.
   - The worker claims requests under the finops role and runs that customer's daily margin
     work as of today: the month to date, the month closing, and the restatement window. It
     then marks the request succeeded or failed.
   - Status only moves forward. The API starts nothing, which is the mapping re-process's
     pattern (ADR 0015) and keeps Temporal the owner of the work (CLAUDE.md rule 3).
   - The Temporal schedule `finops-recompute` runs `FinOpsRecompute` every minute.
   - `recompute-local` answers the queue once without Temporal. It exists for the local
     stack and the console's journeys only, and is refused elsewhere.

6. **The FinOps worker runs in dev.**
   - `infra/gitops/base/worker-finops`: the connectors image, `python -m
     cloudops_connectors.finops worker`, in `platform-ingest` on the ingest pool.
   - It mounts only `postgres-ingest-dsn`, through workload identity on
     `id-cloudops-ingest-{env}` (stack 05, a new federated credential). It has no egress to
     443, because it calls no Azure API.
   - A Sync-hook Job creates both schedules.
   - The daily schedule's request is now empty, and each run resolves its own day from
     workflow time. Before, the date was fixed when the schedule was created, so every run
     would have repeated that day until the next sync.

7. **The console's Settings → Commercial setup.**
   - Shown to staff holding `commercial.read`. The top bar's customer selector preselects
     the customer.
   - The form for the first SOW or an amendment is shown with `commercial.write`. An
     amendment starts from the version in force, so only what changes is retyped.
   - The rate card list, and publishing a version from the latest one, need
     `ratecard.publish` (ADR 0033). The engine-structural parts carry over unchanged:
     catalogue, complexity, multipliers, base-pack bands and slabs.
   - Money is typed and sent as decimal strings. Percentages become fractions on their
     digits, with no float.

## Consequences

- Before the worker runs in dev, the user applies stack 05's new federated credential
  (`azurerm_federated_identity_credential.additional["ingest-worker-finops"]`). Without it
  the pod waits on its Key Vault mount. The schedule job needs no credential.
- Example figures for the dev test customer are entered through the page, following
  `docs/commercial-setup.md`. The repository is public, so it holds no real commercial
  terms.
- A request left `running` by a worker that died stays `running`, because status never
  moves back. The next daily run computes the same figures. A stale-request sweep is a
  follow-up if it is ever needed.
- Not built: a per-customer charge-out rate card for T&M SOWs (ADR 0033). Nothing reads one
  today.
