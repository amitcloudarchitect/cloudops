# ADR 0022: Observability instrumentation

- **Status:** Accepted, 2026-10-31
- **Decided by:** Amit Kumar
- **Implements:** step 19; LLD Observability (sampling, LLM tracing, SLO table, alerts
  that always page, P2 pages), Capacity and cost control; HLD Observability.
  `packages/observability`, `packages/tenancy` (statement spans, security events),
  migration `0014_observability`, `connectors/.../platform_metrics.py`,
  `infra/gitops/base/observability` and `base/platform-metrics`, Terraform stacks 02
  (identity), 03 (workspace, Grafana, alerts), 03b (dashboards), 04b (roles), 05
  (managed Prometheus, federation).

## Context

Phase 2 created Log Analytics (90 days) and workspace-based Application Insights with
local authentication off. The services had no telemetry: a handful of `print` calls
and two loggers. The LLD names the targets (OpenTelemetry, 10% sampling with 100% on
errors and execution, LLM tracing per request, managed Prometheus and Grafana, an SLO
table, alerts that always page) but not how tenant attribution survives in a store
outside row-level security, how telemetry leaves the cluster, or where dashboards
live. The user decided those before the build.

## Decisions

1. **Export through an in-cluster OpenTelemetry Collector** (user). Services send OTLP
   over HTTP to `otel-collector.observability`; nothing in a service talks to Azure
   Monitor. The collector (contrib 0.162.0) tail-samples, applies the attribute
   allowlist a second time, and exports traces and logs to Application Insights with
   Entra ID as its own workload identity, `id-cloudops-observability-{env}`
   (`azure_auth` extension; Monitoring Metrics Publisher on the resource). Metrics are
   exposed on `:8889` and scraped by Azure Monitor managed Prometheus (the AKS
   `ama-metrics` add-on, through the workspace's default data collection rule).
   Names are translated without suffixes (`UnderscoreEscapingWithoutSuffixes`); the
   rules and dashboards use those exact names.

2. **Sampling is the collector's, by tail** (LLD). The SDKs sample `always_on`; the
   collector keeps a trace if any span errored, if any span carries
   `cloudops.execution = true` (`mark_execution`), if any span is an `Execute*`
   workflow, or if the trace id falls in the 10%. The probabilistic decision hashes the
   trace id, so a long-lived trace gets the same decision every time. One replica:
   tail sampling needs a whole trace on one collector. At production volume a
   load-balancing tier routed by trace id goes in front; not needed in dev.

3. **No tenant attribute in Azure Monitor, at all** (user). Not a tenant id, not a
   customer name, not a pseudonymous key, not a principal, not a customer host. Azure
   Monitor is platform-wide storage outside row-level security. Enforced twice:
   - In each process, `cloudops_observability.scrub`: an attribute allowlist (semantic
     convention keys that carry no customer data, plus `cloudops.*` keys whose names
     contain no tenant-, person- or account-shaped fragment), every kept string
     scrubbed of secrets, e-mail addresses and UUIDs, `server.address` kept only for
     platform hosts (anything else becomes `external`), URLs and paths dropped
     (`http.route` says the same safely). Log records go through the same rules:
     `extra` fields off the allowlist are removed, messages and exceptions scrubbed.
   - In the collector, the `redaction` processor with the same allowlist and the UUID
     and e-mail patterns. A test keeps the two lists in step with each other and with
     every `cloudops.*` key the code sets; it caught `cloudops.agent.prompt_version`,
     which the services' rule dropped, now `cloudops.agent.version`.
   Customer names cannot be found by pattern; code must not put one in a message
   (CLAUDE.md, Errors). Per-tenant views are the console's, under RLS.

4. **LLM tracing is attributed under RLS, joined by trace id** (LLD; user, by 3). The
   per-request record the LLD asks for (prompt, retrieval set, tool calls, tokens,
   latency, cost, tenant, user) already lives in `cloudops.llm_call` and
   `cloudops.agent_run`. Migration 0014 adds `trace_id` to both. The span
   (`llm.complete`, `llm.embed`, `agent.run`, `agent.tool`) carries task, tier, model,
   tokens, cost as a decimal string, outcome, counts of retrieved items and tool calls,
   and the `llm_call` id: an engineer goes from a trace to the row, and the row's RLS
   decides whether they may read it. Metrics: `cloudops_llm_calls`, `_tokens`,
   `_cost_micros`, `_duration`, by task, tier, outcome and refusal code.

5. **One trace per ticket.** A ticket's life spans days and many processes, so no
   request context can carry it. At first sighting the ServiceNow connector starts a
   root span `ticket` and stores its W3C traceparent in
   `cloudops.work_item.trace_parent` (0014; random, never derived from the tenant or
   ticket number). Later pages open `ticket.updated` or `ticket.closed` under it; triage
   opens `ticket.triaged` (or starts the trace, for a ticket from before this). Each
   stage links to the batch or workflow trace that produced it. There is no webhook
   yet (the connector polls); a webhook joins the same way. HTTP, Temporal
   (`TracingInterceptor`: client to workflow to activities) and outbound calls
   propagate W3C trace context only, never baggage. The API is the edge: inbound
   `traceparent`, `tracestate` and `baggage` are stripped, so a browser cannot choose
   or join our traces.

6. **Database spans come from `packages/tenancy`.** The engine is private to it (and
   the data-access gate refuses SQLAlchemy engine imports elsewhere), so tenancy
   instruments its own engine with the OpenTelemetry API only: one client span per
   statement, with the statement text (bind markers, never values) and the SQLSTATE on
   failure. The scope statements are spans too.

7. **Security events are structured logs from the code that detects them.** A record on
   `cloudops.security` at CRITICAL with `security_event` and `source`, counted as
   `cloudops_security_events`. `cross_tenant_attempt` is emitted by tenancy (stdlib
   logging; no telemetry dependency): the statement guard, the commit-time scope check,
   a row-level security refusal on write (SQLSTATE 42501, seen in the engine's error
   hook even when the caller catches it), and the telemetry-store wrapper's refusals
   (a row or sample outside scope, a statement without the tenant predicate). A role
   refusal is not a cross-tenant attempt. `unsigned_runbook` and
   `credential_mint_unapproved` are defined and alerted on; the execution plane that
   emits them is not built (CLAUDE.md). Signing-key operations and break-glass
   sign-ins come from Azure's own logs.

8. **Alerts as code, three tiers** (stack 03).
   - Sev0, page: log search rules on Log Analytics every 5 minutes, one per security
     event (`AppTraces`, `Properties.security_event`); the runbook signing key
     (`AzureDiagnostics`, Key Vault audit) used by anyone but the pipeline identity to
     sign or a listed verifier to verify, or managed, or refused; a break-glass
     account's sign-in (`SigninLogs`), created once Entra sign-in logs are exported to
     the workspace and the two object ids are given. Query validation is skipped: a
     table appears with its first row.
   - Sev1 (P2), page: Prometheus rules. Connector outage (last success older than
     three intervals; scheduled but never succeeded), reconciliation drift above 0.5%,
     rollback rate (two or more aborted canaries in a day: the LLD names the alert,
     not the threshold; revisit with on-call), the AI spend breaker open, and the
     health gauges going stale (the outage alerts would be blind).
   - Sev3, notify: the SLO table where a signal exists. API p95 over 500 ms; console
     routes' p95 over 2 s (the dashboard SLO, measured at the API); availability burn
     at 14.4x over an hour against 99.5%; AI latency p95 over 3 s (the gateway does not
     stream, so the whole call stands in for first token, an upper bound); ticket ingest
     lag over 5 minutes; telemetry lag over 2 minutes; quarantine depth rising.
     Remediation verification (above 95%) has no signal until the execution plane.
   Receivers are variables (`page_*`, `notify_*`); webhook URLs only via `TF_VAR_`.

9. **Dashboards: Azure Managed Grafana, as Terraform** (user). Grafana 11, Standard,
   API keys off; its system identity reads the workspace (Monitoring Data Reader) and
   the observability resource group (Monitoring Reader). Six dashboards in stack 03b
   (platform health, ingest health, execution, AI operations, data quality, platform
   cost), each panel PromQL on a data source the stack creates. 03b plans with a
   short-lived Entra token for Grafana, so it is not in CI's plan matrix. Its lock
   file is not committed: the SCA scan matches Grafana server CVEs to the Grafana
   Terraform provider by name, and `.grype.yaml` admits only base-image exceptions;
   `~> 4.49.0` pins the provider instead.

10. **Connector health from the database, as gauges.** Last-success age, lag, runs and
    failures, rejected records, dead letters and schema drift are state in the run log,
    not events, so `cloudops_connectors.platform_metrics` reads them every minute and
    publishes gauges; quarantine depth and occurrences per kind and source;
    reconciliation (largest drift, unverified checks). It runs on the ingest login with
    a scope over every tenant from `cloudops.finops_tenants()`, like the FinOps worker:
    RLS still applies, no new path around it. Its own service account is federated to
    the ingest identity. Connector runs and reconciliation checks are also counted as
    they happen.

11. **The platform's own cost runs through the customer FinOps code** (user). Migration
    0014 adds `cloudops.tenant.internal` (at most one). The internal tenant is a tenant
    like any other: its cost arrives through the Azure cost connector, the FinOps jobs
    reconcile it and find anomalies with `cloudops_commercial`, and the metrics job
    reports month-to-date cost by charge class (integer millionths, never a float),
    whether it reconciles, its anomalies, and its unallocated share computed with the
    same `cloudops_commercial.finops` functions the console's FinOps view uses. Dev's
    seed adds "Getronics CloudOps platform" (GTX). **Deferred:** the live feed. The
    Azure connector runs on fixtures only and has no workload-identity token path, and
    a Cost Management export cannot land in the bronze account (public network access
    disabled; the export resource takes no managed identity). Both are a follow-up.

12. **Rollout in two steps.** In the Azure profile `CLOUDOPS_TELEMETRY` must be explicit
    (`otlp` or `off`), and `off` is refused outside dev. The bases say `otlp`; dev's
    overlay says `off` until `id-cloudops-observability-dev` exists, because the
    collector cannot run without it. Then: apply 02, 03, then 04b and 05 with
    `observability_enabled = true`, 03b; add the collector and platform metrics to the
    dev overlay with their client ids and the connection string; drop the `off` lines.

## Consequences

- Grafana and Azure Monitor never answer "which customer": that question goes to the
  console, which asks the database under the caller's scope. On-call pivots from a
  trace id to RLS'd rows (`llm_call`, `agent_run`, `work_item`, `audit.action_log`).
- A dropped attribute is silent by design. A new `cloudops.*` attribute must be added
  to the collector's allowlist (a test fails otherwise).
- Execution-plane signals (unsigned runbook, credential mint, verification rate,
  approval latency) have alert rules or dashboard panels waiting for emitters.
- The web console (browser) is not instrumented: its requests appear as API server
  spans. Browser telemetry would need its own allowlist and a CSP change.
