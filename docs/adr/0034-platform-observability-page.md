# ADR 0034: The platform observability page

- **Status:** Proposed, 2026-11-01 (accepted on merge)
- **Implements:** HLD Observability (platform usage, LLM tracing, connector health);
  the console's `/observability` (nav entry reserved by ADR 0032).
  `services/api/src/cloudops_api/observability.py`, migration `0032_platform_observe`,
  `apps/web/src/screens/Observability.tsx`.
- **Follows:** ADR 0005 (gateway call records), ADR 0022 (no tenant attribute in Azure
  Monitor), ADR 0023/0031 (platform cost in its host tenant), ADR 0029 (assistant
  sub-budget), ADR 0032 (customer selector, `narrow`).

## Context

Platform administrators asked for one page showing how the platform itself is used and
behaving. The HLD permission matrix has no permission for "usage across tenants", and
the existing ones do not fit: `audit.read` is the auditor's (the whole action log),
`connectors.read` is engineering's, and `cost.read` is Getronics' cost.

## Decisions

1. **A new staff matrix permission, `platform.observe`.** Granted by default to the
   Platform administrator only; administrators may grant it to other staff roles in
   the matrix. A trigger refuses it to any customer-class role (as 0011 does for
   `access.manage`), and every endpoint refuses a customer as well.
2. **Five endpoints, one per section** (`/v1/observability/usage`, `/ai`, `/triage`,
   `/connectors`, `/platform-cost`), so one slow or refused section never blanks the page.
   Each runs under the caller's resolved scope, narrowed by the selector's `tenant_id`
   through `narrow` (ADR 0032). Connector health is per source and is not narrowed.
3. **Usage from `audit.action_log`.** The log records API requests, not screens or
   sign-ins. Top screens are therefore shown as top API endpoints, and "sessions" are
   `GET /v1/me` calls (one per console page load), marked **modelled**. A request in a
   multi-tenant scope is logged once per tenant; it is counted once, by
   (actor, action, time). Roles come from `access.observed_roles`, a definer function
   that returns roles only for the actors the caller already read under RLS.
   `audit.platform_action_log` (requests with no tenant in scope) is not counted.
4. **AI from `llm_call`**, by agent: `classify_ticket` is the classifier,
   `enrich_ticket` the enricher, `draft_*` the drafter, `assistant.*` AMARA, and
   everything else (embeddings, indexing) "other". A `cache:` prefix counts toward its
   task. Each customer's calendar month is shown against the tenant quota and the
   assistant's 40% sub-budget. Spend is Getronics' cost: it is shown only to callers who
   also hold `cost.read`, and is otherwise absent, never zero.
5. **Connectors and platform cost reuse the existing sources** (`access.connector_health`;
   the host's platform resource groups, as on the home dashboard). Each needs its own
   permission (`connectors.read`, `cost.read`) as well; without it, the section says
   so. The Connectors page keeps the detail.
6. **Grafana and Application Insights links come from runtime config**
   (`CLOUDOPS_WEB_GRAFANA_URL`, `CLOUDOPS_WEB_APP_INSIGHTS_URL`), filled from stack
   03-observability's new `grafana_endpoint` and `app_insights_portal_url` outputs. While
   they are empty, as in dev today, the page says "Not yet applied".
7. **Nothing new leaves for Azure Monitor.** The figures go out only in API responses
   under the audited pipeline; no tenant id or customer name is added to telemetry.

## Consequences

- To show the links in dev: apply stack 03-observability, then set the two outputs in
  the dev overlay's `web` ConfigMap.
- A later screen-view event could replace endpoints as the "top screens" proxy. That
  would be a new tenant table with its RLS policy and isolation test.
