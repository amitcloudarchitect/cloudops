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
- **Offset paging over a moving dataset.** A record updated mid-run can shift between
  pages. The overlap and idempotent upserts cover it on the next run.
