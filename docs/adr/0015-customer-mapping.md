# ADR 0015: Customer mapping (the customer master)

- **Status:** Accepted, 2026-10-30
- **Decided by:** Amit Kumar
- **Implements:** HLD M1 ("Tenant and customer master"), LLD Identity resolution;
  step 14b. Migration `0009_customer_mapping`,
  `services/api/src/cloudops_api/mapping.py`, the console's `/mapping` screen, and
  `ServiceNowReprocess` in `connectors/`.
  This changes ADR 0014, decision 4: customer mapping is no longer reserved.

## Context

Identity resolution matches a source's company name exactly against `tenant_alias`,
then normalised, then quarantines it (ADR 0004). Until now nothing put an alias into
`tenant_alias` except a direct insert, so every real dev ServiceNow incident waits
in quarantine. Aliases must not be hard-coded or seeded for real customers. A wrong
alias puts one customer's tickets in another customer's view, so adding one is a
privileged, reviewed act.

## Decisions

1. **Permission `mapping.manage`, role "Customer master".** The new system role has
   class `delivery_lead`, because Delivery owns this reference data. It holds
   `mapping.manage`, `tickets.read` and `inventory.read`. Platform administrator also
   holds `mapping.manage`. A trigger refuses `mapping.manage` to any customer-class
   role, the same way commercial permissions are guarded (ADR 0002, D10).

2. **Proposal, then decision, both append-only.** `cloudops.alias_proposal` records
   who proposed which spelling, from which source, for which tenant, and when.
   `cloudops.alias_decision` records who approved or rejected it, and when. Each
   proposal gets one final decision. A rejection needs a reason. The proposer may
   reject their own proposal, which withdraws it. Both tables are tenant data,
   keyed to the target tenant, with FORCE RLS and `tenant_isolation`. A restrictive
   `staff_only` policy keeps them from any customer scope, even in the customer's
   own tenant. The application role may only read them. Triggers stop the owner from
   updating or deleting either table.

3. **The two-person rule is in the database, switched by configuration.**
   `access.mapping_policy.approvers` defaults to 2: the approver must be a different
   person from the proposer. A trigger on `alias_decision` enforces it, so no path
   can skip it. The migration job sets the value from `CLOUDOPS_MAPPING_APPROVERS`
   after migrating, and writes the change to `access.change_log`. The job runs on
   every sync, so the database converges on the overlay. Only the dev overlay sets
   it, to `"1"`; a GitOps test checks that UAT and production never do. Locally,
   `make migrate` sets 1, as dev does. The setting is a table row, not a server
   setting, because any session can set a custom server setting.

4. **Writes go through SECURITY DEFINER functions only.** The functions are
   `access.onboard_tenant`, `propose_alias` and `decide_alias`. They are owned by
   `cloudops_access_admin`, which already owns the functions that change access, so
   no new server role is needed. Only the application may call them. Each one
   checks that the actor:
   - holds `mapping.manage` as staff;
   - is assigned the target tenant in the access tables;
   - has that tenant in this transaction's scope.

   A tenant outside scope reads exactly like one that does not exist. Approving
   inserts the `tenant_alias` row (`confirmed_by` is the approver) and deletes the
   quarantine row. The quarantine row's count and first and last seen are kept on
   the decision. Every change is written to `access.change_log`, and the API also
   writes `audit.action_log` under the target tenant. The application loses INSERT
   and UPDATE on `cloudops.tenant` and `cloudops.tenant_alias`, which no application
   code used. Ingest keeps them for the local seed and connector tests.

5. **Onboarding is open to `mapping.manage`, and grants nothing.** A new tenant is
   visible at once to every all-tenant principal (ADR 0002, D9). A principal with
   named tenants needs a platform administrator to grant the new tenant before they
   can map to it. Inserting a tenant that no scope holds yet needs one more
   permissive policy, `tenant_onboarding`. It covers INSERT only, for the
   function-owner role only, and the catalog test lists it.

6. **The quarantine is exposed through one function, to `mapping.manage` holders
   only.** `access.alias_quarantine(actor)` returns the source, name, sighting count,
   first and last seen, a suggested tenant and any pending proposal. It never
   returns `raw_ref` or any payload. The suggestion is the stored one if it is in
   the caller's scope. Otherwise it is the one in-scope tenant whose canonical name
   or alias, normalised, contains or is contained in the name's. A suggestion is a
   hint for a person and is never applied by itself. The application still cannot
   read the staging table. The console labels counts as *sightings*: overlapping
   fetches and replays count again (connector state, step 05).

7. **The API is a thin door.** Every endpoint in `mapping.py` requires
   `mapping.manage` and refuses a customer before touching the database. The
   functions' refusals map to structured codes: `forbidden`, `not_found`,
   `conflict`, `second_approver_required` and `invalid_request`. None of them
   carries a tenant or a customer name. Names are stored exactly as the source sends
   them, never trimmed, because resolution's first step is an exact match.

8. **Re-processing held records is a request the connector worker runs (option A).**
   Approving a mapping changes only what is ingested from then on. Records held
   before the approval move only when a person asks, after approval. When 14b was
   built, the bronze replay could not be scoped to one name: it replayed every
   record in a date range, the quarantine row kept only its last page reference,
   and the API has no Temporal client and no access to blob storage. Three options
   were considered:
   - A: narrow the existing replay and let the worker run it;
   - B: have the API start the workflow itself;
   - C: an operator runs an unscoped replay.

   A was chosen. B would add a Temporal client and certificate to the API, a new
   trust path (ADR 0010). C would count every other quarantined name again and can
   dead-letter pages.
   - **The request.** The person sees a preview: the sightings held at approval,
     labelled as an upper bound, and the bronze days to replay. Then they ask.
     `access.request_reprocess` refuses unless:
     - the proposal was approved;
     - something was held;
     - the name still maps to that tenant;
     - no request for that name is waiting or running.

     It writes a `cloudops.alias_reprocess` row and logs the change. The API starts
     nothing.
   - **The worker.** A Temporal schedule (`servicenow-reprocess`, every minute,
     created by the existing schedule job) runs `ServiceNowReprocess`. It works as
     follows:
     1. It claims the oldest waiting request with `cloudops.claim_reprocess`,
        before it knows the tenant. Two policies, confined to the function owner,
        let it see and claim waiting requests only.
     2. It re-checks the request at run time, under the tenant's scope. The
        mapping must still be approved, and the name must still resolve exactly
        to that tenant.
     3. It replays the bronze days through `reprocess_page`. This is the same
        normalisation, linking and upsert as `process_page`, narrowed to records
        under that name. It checks resolution again on every page, writes no
        quarantine for any other name, moves no watermark and never re-fetches
        from the source.
     4. It closes the request with `cloudops.finish_reprocess` and the exact
        number of records that moved: distinct records that were not already in
        the tenant.

     A name that stops resolving to the tenant ends the request **refused**. A
     page that keeps failing is dead-lettered as in any replay and ends it
     **failed**. The status only moves forward; the request itself never changes.
   - **The window** is the held sightings' days, from the day before the first
     (a page fetched before midnight may be processed after it) to the last.
     Bronze is dated by fetch time.
   - **`make reprocess`** runs the queue once locally.

## Not decided here (follow-ups)

- **An exact preview count.** Counting held records exactly before a re-process
  would need a per-record ledger, written by the connector at quarantine time. It
  was not built because it covers only records ingested after it exists, and the
  quarantine already holding real dev incidents would get no benefit. The preview
  stays an upper bound, and the run reports the exact count.
- **Other connectors' re-process.** The request table and functions take any
  source system. Only the ServiceNow worker claims requests today, so requests for
  another source wait until its connector claims them too.

- **Removing or correcting a wrong mapping.** Deleting an alias, moving a tenant's
  tickets that were ingested under it, and recording why. Out of scope for 14b. A
  re-process re-checks the mapping at run time, so it never acts on a mapping
  removed after the request.
- **Other sources.** The screen and functions take any source system name. The
  quarantine fills only from connectors that exist (ServiceNow today).
- **Display names.** The console shows "you" or "another person" for the proposer.
  Showing names needs the application to read `access.principal`, which it cannot
  today.
