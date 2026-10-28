# CloudOps Platform: local development

Getronics' multi-tenant agentic operations platform. This page gets you from a fresh
clone to a running, seeded stack with green tests, with no Azure access needed.

Read these before you write code, in this order:

1. [`CLAUDE.md`](CLAUDE.md): the rules that matter most. Tenant isolation comes first.
2. [`docs/LLD.md`](docs/LLD.md): the specification. Where anything disagrees with it, the LLD wins.
3. [`RUNBOOK.md`](RUNBOOK.md): the build sequence and where the project is in it.

## Your first thirty minutes

| Minutes | Do |
| --- | --- |
| 0–5 | Install the prerequisites and run `uv sync` |
| 5–10 | `make up`, then `make seed` |
| 10–15 | `make test`, and see the isolation suite pass against your own Postgres |
| 15–25 | Open the Temporal UI, run `make psql`, and look at the seeded mess (see "Exploring the data") |
| 25–30 | Read "The rules that matter most" in `CLAUDE.md` |

## Prerequisites

- **Docker** with Compose v2.20 or later (for `--wait`). Allow it about 4 GB of RAM.
- **[uv](https://docs.astral.sh/uv/)**: it installs the pinned Python 3.12 itself.
- **make** and **bash**. On Windows, work inside WSL2 and clone into the Linux
  filesystem, not `/mnt/c`.

## Quick start

```bash
uv sync
```

```bash
make up
```

```bash
make seed
```

```bash
make test
```

The first `make up` creates `.env.local` with fresh random passwords and builds the
Postgres image, which takes about a minute. After that it starts in seconds.

## What's running

All ports bind to `127.0.0.1` only.

| Service | Address | Stands in for |
| --- | --- | --- |
| PostgreSQL 16, pgvector, pg_partman | `127.0.0.1:5432`, database `cloudops` | Azure Database for PostgreSQL Flexible Server |
| Redis 7.4 | `127.0.0.1:6379` | Azure Cache for Redis |
| Temporal (dev server) | `127.0.0.1:7233`, UI at <http://localhost:8233> | Temporal in AKS |
| Azurite (blob) | `http://127.0.0.1:10000/devstoreaccount1`, containers `bronze` and `reports` | The bronze and reports storage accounts |
| ClickHouse 24.8 | `http://127.0.0.1:8123`, database `telemetry` | The telemetry store (LLD open item L1) |
| Mock LLM gateway | in-process | Azure OpenAI behind the AI gateway |

Postgres runs with the LLD's production parameters: `row_security=on`,
`statement_timeout=30s`, `idle_in_transaction_session_timeout=60s`, `work_mem=16MB`,
`maintenance_work_mem=1GB`, `random_page_cost=1.1`, `max_connections=400` and
`log_min_duration_statement=500ms`. It also runs pg_partman's background worker. A
query that times out or sorts to disk in production does the same here.

Database logins match what infrastructure creates in Azure. **None of them can bypass
row-level security**, and `packages/tenancy` refuses to run as one that could.

| Login | Member of | Used by |
| --- | --- | --- |
| `cloudops_migrator` | (owns the schema) | `make migrate` only |
| `cloudops_app_local` | `cloudops_app` | services, the seed's read-back check, `make psql` |
| `cloudops_ingest_local` | `cloudops_ingest` | connectors, the seed |
| `postgres` | superuser | `make test` provisioning its throwaway databases. Nothing else. |

## Make targets

| Target | Does |
| --- | --- |
| `make env` | Creates `.env.local` with random passwords. Never overwrites an existing one. |
| `make up` | Starts the stack, waits until it is healthy, applies migrations, creates the blob containers |
| `make seed` | Loads the synthetic dataset, then checks isolation. Refuses if the data is already there. |
| `make test` | Runs every test, including the isolation suite, against the local Postgres |
| `make lint` | ruff, `mypy --strict`, and the data-access gate |
| `make check` | `lint` and `test`, which is what CI runs |
| `make down` | Stops the stack. Data stays in Docker volumes. |
| `make reset` | Wipes every volume, then runs `up` and `seed` again |
| `make psql` | Opens psql as the application login |
| `make api` | Runs the API at `http://127.0.0.1:8000` |
| `make token OID=…` | Mints a dev token for a seeded principal |
| `make ingest` | Runs the ServiceNow connector over the recorded fixtures |
| `make replay` | Reprocesses today's bronze pages without re-fetching |
| `make logs` / `make ps` | Container logs and status |

`make test` never touches your seeded data. Each test session creates its own database
on the same server and drops it afterwards.

## Configuration: local or Azure, never a guess

Every process states its environment explicitly. Nothing falls back from one
environment to the other.

```python
import os
from cloudops_config import Component, DbLogin, load_settings
from cloudops_tenancy import Database

settings = load_settings(os.environ, require={Component.DATABASE_APP, Component.LLM})
db = Database.create(settings.database_dsn(DbLogin.APP))
llm = settings.llm
```

- `CLOUDOPS_ENV` must be `local` or `azure`. If it is unset, the process refuses to
  start. It does not assume local.
- A service declares the components it needs, and they are all validated at startup.
  A misconfigured pod dies at boot, not on its first request. Asking for a component
  you did not declare is an error.
- The **local** profile reads secrets from `.env.local` and uses the containers above
  plus the mock LLM. It refuses to run if `KUBERNETES_SERVICE_HOST` or
  `AZURE_FEDERATED_TOKEN_FILE` is set.
- The **azure** profile needs the workload identity the AKS webhook injects
  (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and a readable `AZURE_FEDERATED_TOKEN_FILE`) and
  the CSI secrets mount in `CLOUDOPS_SECRETS_DIR`. Each component also needs its own
  address: `CLOUDOPS_REDIS_HOST`, `CLOUDOPS_BRONZE_CONTAINER_URL`,
  `CLOUDOPS_REPORTS_CONTAINER_URL`, `CLOUDOPS_TEMPORAL_ADDRESS` and
  `CLOUDOPS_TEMPORAL_NAMESPACE`. If any of these is missing, it raises. It never reads
  `.env.local`, even when the file is present.
- TLS, ports and auth modes are fixed per profile. They are not variables.
- Telemetry and the LLM have **no Azure backend yet**. Requesting either under
  `azure` raises `NotBuiltError` instead of quietly using a local stand-in.

Secrets are named as they are in Key Vault, and the store decides where each one comes
from. In Azure, `postgres-app-dsn` is the CSI-mounted file `/…/postgres-app-dsn`. Locally
it is the line `POSTGRES_APP_DSN=…` in `.env.local`.

The mock LLM returns the canned responses in
`packages/config/src/cloudops_config/canned_responses.json`, keyed by task. It never
reads the prompt, and an unknown task raises an error. Embeddings are deterministic
1536-dimension unit vectors.

## The seed data

`make seed` loads a deterministic dataset generated by
`packages/devdata/src/cloudops_devdata/synthetic.py`:

| | |
| --- | --- |
| Tenants | 5 fictional customers (Northwind, Contoso, Fabrikam, Tailspin, Woodgrove) |
| Resources | 200 across 13 Azure subscriptions and AWS accounts |
| Tickets | 500 incidents, requests, change tasks and project tasks over 90 days |
| Cost | About 18,800 rows covering the 90 days up to yesterday, in EUR, GBP and USD, including credits, tax, reservations and savings plans |
| FX | Daily average and month-end closing rates for every currency pair. Synthetic, not a real source (LLD L4). |
| Vulnerabilities | About 150 Qualys detections, some on hosts the inventory has never seen |
| Commercial | One SOW per tenant. Tailspin's contract changed currency at renewal. |

**It is deliberately messy.** Identity resolution is only worth testing against input
that looks like what the sources really send:

- **Customer names:** each tenant has registered aliases in several source systems.
  Ticket `company_raw` values also use spellings that resolve only after normalisation,
  such as `Northwind Logistics BV.`, `Contoso  Retail Ltd` and `Tail Spin Air GmbH`.
  Typos, accents, abbreviations and ambiguous fragments such as `Contoso`, `TSA` and
  `Northwind Logístics B.V.` sit in `staging.tenant_alias_quarantine` with occurrence
  counts. Some have a suggested tenant and some do not.
- **Resources:** names follow five naming conventions plus deviations. Some are FQDNs
  and some short names, and some AWS instances carry no Name tag. `dc01` exists in two
  tenants, `fs-shared01` exists twice inside one tenant, and `10.0.1.4` is used by two
  tenants.
- **Ticket CIs:** a CI can match one resource, match several (and so stay unlinked),
  match nothing (and so go to `staging.resource_quarantine`), or be junk such as `N/A`
  or `TBD`.
- **Tags:** keys are inconsistent (`env`, `Environment`, `ENV`, `Env `), some
  duplicates conflict, and some resources have no tags at all.
- **Closure codes:** `Solved (Work Around)` escapes the `%workaround%` flag that
  `Resolved (Workaround)` matches.
- **Planted hostile content:** a few prompt-injection strings appear in ticket text and
  in a resource tag, and a few tickets mention another customer by name. Ingested
  content is data, never instruction.

`Dataset.alias_truth` and `Dataset.ci_truth` record the correct outcome for every messy
value: tenant, resolution path, and whether a CI should be linked or quarantined. Use
them to score a resolver. The same `--seed` always produces the same rows and ids. Only
the dates move, because the 90-day window ends yesterday. Pass `--anchor 2026-10-27`
for fixed dates:

```bash
uv run python -m cloudops_devdata.seed --anchor 2026-10-27 --seed 7
```

The seed writes as the ingest login, one transaction per tenant under that tenant's
scope, so row-level security checks every row on the way in. It then reads everything
back as the application login. Each tenant's scope must see exactly its own rows, an
empty scope must see nothing, and an engineer must see no SOW rows. If any of that
fails, the seed fails.

## Exploring the data

`make seed` prints each tenant's id. `make psql` connects as the application login, so
with no scope set you see nothing. That is the point:

```sql
SELECT count(*) FROM cloudops.resource;          -- 0: no scope, no rows

BEGIN;
SET LOCAL app.tenant_ids = '<a tenant id from make seed>';
SET LOCAL app.role = 'finops';
SELECT name, name_norm, ip_addresses, tags FROM cloudops.resource LIMIT 10;
SELECT company_raw, count(*) FROM cloudops.work_item GROUP BY 1;
SELECT currency, charge_class, sum(amortized_cost) FROM cloudops.cost_fact GROUP BY 1, 2;
COMMIT;
```

This is for poking around by hand only. **Application code never sets scope itself.**
It goes through `cloudops_tenancy.Database.transaction(scope)`, and CI fails on
anything else.

## Calling the API

`make seed` also creates the default platform admin (ADR 0002, D14) and four demo
principals, and prints their Entra object ids:

| Principal | What it can see |
| --- | --- |
| `finops-all` | Staff, FinOps role, every tenant, including commercial data |
| `engineer-nwl-crl` | Staff, Engineer role, Northwind and Contoso, no commercial data |
| `customer-nwl` | A Northwind customer: the customer view, its own tenant only |
| `customer-fab-one-sub` | A Fabrikam customer limited to one subscription |

```bash
make api
```

Then, in another terminal (`make seed` prints the oid):

```bash
curl -s -H "Authorization: Bearer $(make -s token OID=<oid from make seed>)" http://127.0.0.1:8000/v1/resources
```

Every request goes through the same pipeline: token validation, scope resolution from
the database, the permission matrix, the query under row-level security, provenance
on every number, and an audit row. Tenant scope comes only from the access tables,
keyed on the token's `oid`. Headers, query parameters and token claims never change
it; try sending `X-Tenant-Id`. The OpenAPI schema is at
`http://127.0.0.1:8000/openapi.json` (local only).

## Azure database bootstrap

Before the first migration on a new server, its administrator creates the server-level
roles the migrations rely on. This is the same file `make up` runs locally,
[`db/bootstrap/roles.sql`](db/bootstrap/roles.sql). It is idempotent, and it refuses
rather than repairs a role that already exists with `LOGIN`, `SUPERUSER` or
`BYPASSRLS`.

Prerequisites on Azure Database for PostgreSQL (Entra authentication only):

1. **The migration role exists.** It is the Entra identity the migration pipeline runs
   as (step 13), created by the Entra administrator with
   `SELECT * FROM pgaadauth_create_principal('<identity name>', false, false);`.
2. **A host that can reach the private endpoint,** such as a pod in AKS or the devbox
   once stack 01 admits it to the data subnet. `psql` must be installed there.
3. **An Entra access token for the server administrator,** used as the password.

```bash
export PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"
```

```bash
psql "host=psql-cloudops-dev-neu.postgres.database.azure.com dbname=postgres user=<entra admin name> sslmode=require" -v migrator=<migration identity name> -f db/bootstrap/roles.sql
```

The token expires within about an hour and is never written anywhere.

## How local differs from Azure

Each of these departures is deliberate and applies to local only.

| Local | Azure (LLD) | Why |
| --- | --- | --- |
| No TLS on Postgres, Redis or ClickHouse | TLS 1.2 everywhere | Loopback only. The Azure profile hard-codes TLS. |
| Password logins from `.env.local` | Workload identity, CSI-mounted secrets | No Entra ID on a laptop |
| No PgBouncer | PgBouncer in transaction mode (LLD L6 open) | Not yet decided. `packages/tenancy` is already written for transaction pooling. |
| Temporal dev server on SQLite | Temporal in AKS | One binary and a UI. Persistence and TLS for Azure are not yet specified. |
| One Azurite account with two containers | Separate bronze and reports storage accounts | Same container URLs, different hosts |
| ClickHouse single node, users only, no tables | 3–5 nodes, schema decided at the telemetry step | LLD L1 (ClickHouse or ADX) is still open |
| Mock LLM | Azure OpenAI through the AI gateway (step 06) | No model calls, cost or data leaving the laptop |
| Tokens from a dev issuer (`make token`) | Entra ID tokens | No Entra on a laptop. The API validates both the same way. |
| `statement_timeout` 30 s for every login | 30 s application, 300 s reporting | No reporting role exists yet. It takes a per-role override when it does. |
| `make up` runs migrations | A pipeline job runs migrations, never a human | Convenience. It still runs as the migrator, not as superuser. |

## Troubleshooting

- **"port is already allocated":** something else, often a local Postgres, holds 5432,
  6379, 7233, 8233, 8123 or 10000. Stop it.
- **"already seeded":** run `make reset`.
- **`.env.local` deleted while the volumes still exist:** the containers keep the old
  passwords, so delete the volumes too with `rm -f .env.local && make reset`.
- **"CLOUDOPS_ENV is not set":** you ran a module directly. Go through `make`, or
  export `CLOUDOPS_ENV=local` yourself.
- **One `FATAL: role "cloudops_migrator" does not exist` in the Postgres log on first
  start:** this is harmless. pg_partman's worker starts during the init phase, before
  the init script has created the role.

## Repository map

| Path | What |
| --- | --- |
| `packages/tenancy` | The only door to the database |
| `packages/config` | Backend selection, secret stores, the mock LLM |
| `packages/devdata` | `.env.local` generation, Azurite provisioning, the synthetic seed, dev tokens. Local only. |
| `services/api` | The API: token validation, scope resolution, BFF, provenance, audit |
| `connectors` | Source connectors, plus the connector contract ([`connectors/README.md`](connectors/README.md)) |
| `db/migrations` | Hand-written SQL migrations (alembic) |
| `db/tests` | Schema checks and the isolation suite |
| `infra/local` | Init scripts for the compose stack |
| `infra/terraform` | Azure infrastructure: plan freely, never apply |
| `tools/ci` | The data-access gate and the Terraform checks |
