# ADR 0037: Connector sync every six hours, and once when a customer is onboarded

- **Status:** Accepted, 2026-11-01
- **Decided by:** Amit Kumar
- **Implements:** the user's request (2026-11-01): "connectors should sync once as
  soon customer is on-boarded, post that it should sync every 6 hours".
  `connectors/src/cloudops_connectors/core/sync.py`, the worker command in
  `core/cli.py`, migration `0034_connector_sync`.
- **Amends:** ADR 0028 (SharePoint, Qualys and CoreStack ran daily). Follows ADR
  0015's request pattern: the API records, a worker acts.

## Context

SharePoint, Qualys and CoreStack synced once a day, so a customer onboarded in the
morning showed nothing from them until the next day. Mapping a source's name to a
tenant changed nothing until that source's next run either. The cloud credential path
already syncs at once: a passed check creates and triggers the credential's own
schedules (customer worker, ADR 0027).

## Decisions

1. **Every six hours** for SharePoint (`drive_item`, `folder_item`), Qualys
   (`host_detection`, the dev simulation too) and CoreStack (`finding`):
   `sync.CUSTOMER_SYNC` in each connector's schedule, and the same expected interval
   in `staging.connector_schedule`, which connector health reads. The schedule Job
   is an Argo CD sync hook, so the next sync applies the new interval to the existing
   schedules (`upsert_schedule` replaces the spec; checked against a Temporal
   server). An update never triggers a run. Temporal aligns the interval to the
   epoch: runs fall at 00:00, 06:00, 12:00 and 18:00 UTC.

2. **Left as they are:** ServiceNow (five minutes), Azure and AWS inventory (15
   minutes), and the daily feeds whose source publishes daily: the providers' cost
   exports, totals and invoices (the platform's own feed and each customer
   credential's), ECB rates, the FinOps recompute. Entra runs on fixtures only.

3. **Once, now, when a customer is onboarded.** The database queues a request in
   `staging.sync_request` when:

   | Event | Row inserted | Sources asked |
   | --- | --- | --- |
   | A tenant is onboarded | `cloudops.tenant` | SharePoint, Qualys, CoreStack |
   | An alias is approved | `cloudops.tenant_alias` | that alias's source, if one of the three |
   | An account is added (a passed credential check, or inventory) | `cloudops.cloud_account` | CoreStack, Qualys (their findings link to accounts and resources) |

   An `AFTER INSERT` trigger does it, so every path that onboards (the definer
   functions of 0009, 0025 and 0029) queues without changing, and nothing the client
   sends decides it. At most one request waits per source; more coalesce. A row is
   a source and a reason code: no tenant, no name, nothing a source sent.

4. **The worker claims, the API starts nothing.** Each of the three sources' workers
   polls every minute, claims its own source's waiting request, and triggers each of
   its schedules once with `BUFFER_ONE`, so a run that started before the change is
   followed by one more rather than skipped. The claim and the triggers share a
   transaction: a trigger that fails leaves the request waiting. A schedule not
   created yet is passed over, because its creation runs it anyway. Claimed rows are
   kept 30 days.

5. **Grants.** The trigger function is SECURITY DEFINER, owned like the table, so the
   roles that onboard need no grant to queue. The ingest role can read, claim and
   clear requests, but not insert one; the application has no grant at all.
   `staging.sync_request` is ingest state like `connector_schedule`: no tenant
   column, protected by grants (`test_schema_rls.NON_TENANT`).

## Consequences

- An approved mapping for SharePoint or CoreStack shows its data within a minute or
  two. Both read their source whole each run, so the sync files everything the
  quarantined name held.
- **Incremental sources need a re-process, not a sync.** Qualys reads from its
  watermark, so detections held before the mapping are not fetched again. They move
  only by the ADR 0015 re-process, which only ServiceNow implements today. The dev
  simulation resolves its tag without a mapping, so this does not bite yet.
- An account added queues Qualys before the Azure inventory has read the account's
  VMs (15 minutes later). The dev simulation builds its estate from that inventory,
  so the first such run may hold fewer hosts; the next six-hourly run completes it.
- **CoreStack detail refusals.** CoreStack's detail endpoint answers 400 for every
  Defender for Cloud recommendation. Since this change, such a recommendation is filed
  from its list fields as one finding (`raw_extra.detail_status`), without per-resource
  rows, and its earlier per-resource findings are held rather than resolved.
