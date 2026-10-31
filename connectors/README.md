# Connector contract

Every source connector follows this contract (LLD, Connectors and execution plane).
ServiceNow (`src/cloudops_connectors/servicenow`) is the reference implementation:
copy its shape, not just its intent.

```
authenticate → watermark → fetch (5-min overlap) → raw to bronze → normalise
  → resolve identity → quarantine unmatched → commit with the watermark → report
```

## The rules

| Step | Rule | Where it lives in ServiceNow |
| --- | --- | --- |
| Authenticate | Credentials come from the secret store (`cloudops_config`): Key Vault in Azure, `.env.local` locally. Never from code or config. Customer-environment credentials are covered by ADR 0003; a source's own read account is not. | `__main__.py` (`_client`) |
| Watermark | One per source, entity and instance (`staging.ingest_watermark`). Read at the start of a run. | `core/state.py` |
| Fetch | From the watermark minus **five minutes**, ordered by update time then id, so offsets are stable. Honour the source's rate-limit headers (`core/ratelimit.py`). Retries belong to Temporal: exponential backoff, **five attempts**. | `client.py`, `workflows.py` (`RETRY`) |
| Raw to bronze | The response body is stored **before anything parses it**, at `{source}/{entity}/yyyy/mm/dd/{run}-{page}.json`, write-once, with a SHA-256 hash. | `workflows.py` (`fetch_page`), `core/raw.py` |
| Normalise | Map source fields 1:1 to the canonical columns. **Every unmapped field goes to `raw_extra`.** A record without its keys is rejected and counted, never guessed; it stays in bronze. | `mapping.py` |
| Resolve identity | Customer: `cloudops.resolve_tenant_alias(source, name)`. It tries an exact match on source plus alias, then a normalised match, and otherwise returns nothing. The connector never reads aliases itself (ADR 0004). Resource: external key, then `name_norm` if unique in the tenant, otherwise unlinked. | `pipeline.py`, migration 0005 |
| Quarantine | Unmatched customer names go to `staging.tenant_alias_quarantine`, and unlinked resources to `staging.resource_quarantine`, with occurrence counts. Nothing is dropped and nothing is guessed. | `core/state.py` |
| Commit | **One transaction per page**: the upserts, the quarantine counts and the watermark advance commit together or not at all. The scope is exactly the tenants the page resolved to. Upserts key on the source's natural key, so replays are idempotent. | `pipeline.py` |
| Report | One `staging.ingest_run` row per run, recording pages, records, written, quarantined, rejected, CIs unlinked and lag. Quarantine depth is an alerting signal. | `core/state.py`, `workflows.py` |
| Dead letter | After five failed attempts a page goes to `staging.ingest_dead_letter` with its bronze reference. The run stops there, so the watermark never skips past it. | `workflows.py` |
| Replay | Reprocess bronze for a date range through the same `process_page`, without fetching and without moving the watermark. | `ServiceNowReplay` |

Orchestration is Temporal (CLAUDE.md rule 3). The connector is a workflow started by a
Temporal schedule (every 5 minutes for ServiceNow), not a cron job. Workflow code only
orchestrates; every side effect is an activity.

## The step 15 connectors (ADR 0016)

Qualys, CoreStack, Azure management, AWS management, SharePoint and Entra ID run
on one shared orchestration, `core/ingest.py` (`ConnectorIngest`,
`ConnectorReplay`). Each source supplies a `Source` with `fetch` (one page as
sent), `inspect` (after bronze: record count, next cursor, companion payloads),
and `process` (the contract's steps 5 to 8 for one page). `core/identity.py`,
`core/drift.py` and `core/cli.py` are shared by all of them.

| Source | Entities | Cadence | Notes |
| --- | --- | --- | --- |
| `qualys` | `host_detection` | daily | FQDN naming: resources match on asset id, then `name_norm` plus IP, then `name_norm`. KnowledgeBase entries are stored in bronze beside each page. Customer from the `Customer:` host tag. |
| `corestack` | `finding` | daily | Writes `cost_finding` only, never `cost_fact`: cost comes from the providers' raw exports (HLD). API shape assumed until CoreStack docs and a sandbox exist (`corestack/mapping.py`). |
| `azure` | `resource`, `cost_export`, `cost_total`, `invoice` | 15 min, daily | Resource Graph in subscription batches, honouring `x-ms-user-quota-remaining`. The snapshot is authoritative per subscription: resources not seen in a complete run are closed. Cost exports, totals and invoices: see step 16 below. |
| `aws` | `config`, `cur`, `cost_total`, `invoice` | 15 min, daily | Config aggregator queries; CUR from S3, each assembly staged and swapped into `cost_fact` atomically, so a restatement replaces the period. Totals and invoices: see step 16 below. |
| `ecb` | `fx` | daily | ECB euro reference rates into `fx_rate` (step 16 below). |
| `sharepoint` | `drive_item` | nightly | Per-site selected consent, delta sync per site. Deletion, or a site's consent withdrawn, removes its chunks. |
| `entra` | `user`, `role_assignment` | daily | Users by delta; role assignments as a snapshot, removals closed. |

### Step 16: cost exports, provider totals and FX rates

| Source / entity | Reads | Writes | Notes |
| --- | --- | --- | --- |
| `azure` / `cost_export` | Cost Management scheduled export of the FOCUS 1.0 dataset to blob storage: per run a `manifest.json` (`runInfo`, `blobs[]`) naming gzip CSV parts. One scope per export root (`<container>/<path>`). | `cost_fact` (`source_system` `azure`) | The CUR's staged swap, shared in `core/assembly.py`: a run replaces its subscriptions' period atomically, a superseded run swaps nothing. `charge_class` from the fixed rule in `azure/focus.py` (licence meters separate from consumption, HLD D8). `Tags` to `tags_snapshot`. |
| `azure` / `cost_total` | Cost Management query per subscription: `ActualCost`, `MonthToDate`, sum of cost with currency. | `billing_total` (`provider_total`) | `as_of` is the run's day. Subscriptions come from the ARM listing, as for `resource`. |
| `azure` / `invoice` | Billing invoices per subscription of the configured billing account, last three months of invoice periods. | `billing_total` (`invoice`) | `billedAmount` (else `amountDue`), `name` as the document reference. |
| `aws` / `cost_total` | Cost Explorer `GetCostAndUsage`, MONTHLY `UnblendedCost` by `LINKED_ACCOUNT`, month start to today. One scope per payer. | `billing_total` (`provider_total`) | `as_of` is the period's end minus one day. |
| `aws` / `invoice` | Invoicing `ListInvoiceSummaries` for the payer, invoices issued in the last 92 days. | `billing_total` (`invoice`) | `BaseCurrencyAmount`, `InvoiceId`; figures for one key in one currency are summed (an invoice and its credit memo). |
| `ecb` / `fx` | The ECB history file (`eurofxref-hist.xml`). | `fx_rate` (`source` `ecb`), `fx_restatement` | Every directed pair of the configured currencies (EUR, GBP, USD by default), quantised to 8 places half-even: `average` per ECB day, `closing` on the last ECB day of each completed month. A revised ECB rate is updated with an explicit `fx_restatement`; a `finance` rate is never overwritten. |

Shared rules: raw to bronze before parsing; `Decimal` from the payload to the
column, rounded once; an account resolves only by an exact alias of its
provider, unknown ones go to the alias quarantine with counts; billing totals
upsert on (account, kind, period, as_of), so a re-read or a replay rewrites the
same row; watermarks move in the committing transaction (the newest `as_of`, the
last manifest swapped in, the newest ECB day). Endpoint shapes of the billing
APIs and the FOCUS export are assumptions until they are checked against the
real services (ADR 0016).

Cloud account and directory ids resolve only through an exact, approved alias
(`azure`, `aws`, `entra`): an unknown one shows up in the customer mapping
console. None of these has a credential path yet: without `--fixtures` they
refuse to start (CLAUDE.md rule 2).

```bash
make connectors        # every step 15 connector once over its fixtures
uv run python -m cloudops_connectors.ecb run --fixtures              # step 16: ECB rates
uv run python -m cloudops_connectors.azure run --fixtures --entity cost_export
```

## A customer's own credential (ADR 0027, under ADR 0003)

Non-production only. `cloudops_connectors.customer` reads a customer's Azure
subscriptions with an app registration's secret, or an AWS account with an access
key, read-only. The secret is a Key Vault secret read through the `SecretStore` by
name (`customer-<code>-azure-secret`), never stored anywhere else.

- **Check first.** An onboarding (`/v1/cloud-onboarding`) queues a check; the worker
  claims it, confirms the credential reaches exactly its accounts with read roles only
  (`customer/check.py`), binds the accounts to the tenant and starts the first sync.
- **Bound sources.** The ordinary sources, built with an `identity.Binding`: the
  accounts resolve to the credential's tenant (never through the alias map), the Azure
  client refuses any other subscription before calling (`allowed`), and the source's
  `instance` names the credential (own watermarks; replays skip other instances' pages).
- **One worker per credential**, on `connectors-<provider>-customer-<code>`, with
  schedules `customer-<provider>-<code>-<entity>`.
- `stored_key` is refused in `prd` (`cloudops_config`, `CUSTOMER_ACCESS`).

Instructions: `docs/cloud-onboarding.md`.

## Telemetry (not the batch medallion)

```
Event Hubs (evh-metrics) ──> consumer ──> normalise ──> resolve ──> allowlist
        │                                                              │
        └── Capture: bronze copy, in parallel           cloudops_tenancy.telemetry
                                                        (tenant predicate injected)
                                                                       │
                                                              column store (ClickHouse)
```

- `telemetry/normalise.py`: Azure Monitor diagnostic records and CloudWatch
  metric streams map to the canonical metric model.
- `telemetry/resolve.py`: an account resolves only by an exact alias, and a
  resource only within its tenant. Answers, negative ones included, are cached for
  five minutes. Only `staging.metric_allowlist` pairs are stored.
- `telemetry/consumer.py`: the checkpoint moves only after the batch is stored.
  A batch is retried five times, then the consumer halts rather than skip it.
  Writes carry the partition and sequence range as a deduplication token, so
  redelivery stores nothing twice.
- `telemetry/eventhub.py`: the SDK binding (managed identity, blob
  checkpoints). It is not wired to a command until LLD open item L1 is decided.
- Locally, `make telemetry` applies `db/telemetry` and streams the recorded
  events through the in-memory hub (`telemetry/hub.py`), whose `Capture`
  writes each event to Azurite as it is published.

## Writing the next connector

1. Add `src/cloudops_connectors/<source>/` with `mapping.py`, `client.py`, `fake.py`,
   `pipeline.py`, `workflows.py` and `__main__.py`, shaped like ServiceNow's. Reuse
   `core/` as it is.
2. Keep the canonical table's natural key as the upsert key, and add `raw_extra` to the
   table if it lacks one, in an expand migration.
3. Record realistic fixtures, anonymised. If the synthetic dataset can generate them,
   build them from it (`cloudops_devdata`), so the ground truth comes for free.
4. Write a fake instance (an httpx transport) that answers the API as the client uses
   it, including rate-limit headers. The client must not know whether it is talking
   to a fake.
5. Tests, all required:
   - contract tests against the fixtures: mapping, `raw_extra`, malformed records,
     rate limits, raw before parse;
   - an integration test on the migrated database covering ground truth, overlap
     with no duplicates, updates, the atomic commit, dead letters and replay;
   - isolation tests for any new function or policy.

## Running ServiceNow locally

```bash
make ingest
```

That runs both entities against the recorded fixtures, through Temporal, Azurite and
the local database, and prints the run reports. To replay today's bronze pages:

```bash
make replay
```

Against a real instance the connector refuses to start until the ServiceNow read account
exists (LLD readiness gate 3). The integration user's timezone must be GMT, because
encoded-query date-times are read in the user's timezone.

## Known limits

- **A ticket moving between tenants fails its page.** If a ticket's company is
  re-pointed to a different tenant, the upsert cannot reach the old row under the new
  tenant's scope. The page is dead-lettered for a person to decide; it is not resolved
  automatically.
- **Quarantine counts are sightings, not tickets.** A ticket seen again in the
  five-minute overlap, or replayed, counts again. The counts are for spotting a rising
  trend, not for exact figures.
- **Account and resource ids are matched as sent.** Azure ids are
  case-insensitive, but rows keep the casing Resource Graph sends. If it changes a
  resource group's casing, the result is a new row, and the old row is closed.
- **A listed subscription that returns no rows has its inventory closed.** The
  snapshot is authoritative. Partial permissions that still list a subscription
  would empty it.
- **An approved mapping for a non-ServiceNow source applies from the next run.**
  Only ServiceNow re-processes held records on request (ADR 0015).
- **Offset paging over a moving dataset.** A record updated mid-run can shift between
  pages. The overlap and idempotent upserts cover it on the next run.
