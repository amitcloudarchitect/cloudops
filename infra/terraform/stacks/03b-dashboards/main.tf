# Stack 03b — the dashboards (step 19; LLD, Observability; ADR 0022).
#
# Six dashboards, as code: platform health, ingest health, execution, AI operations,
# data quality, and the platform's own cost. Each panel is a PromQL query on the
# Azure Monitor workspace (managed Prometheus), so Grafana holds no data of its own.
# No panel shows a tenant: no metric carries one. Per-customer views are the
# console's, under row-level security.
#
# Apply after stacks 03 and 04b (Grafana exists; its identity reads the workspace;
# you hold Grafana Admin). Not in CI's plan matrix: planning needs a Grafana token.
#
# Metric names are the collector's (UnderscoreEscapingWithoutSuffixes): no _total,
# no unit suffix. Service metrics carry job="cloudops/<service>". Money is counted in
# millionths (*_micros) and divided here, for display only.

data "azurerm_dashboard_grafana" "this" {
  name                = module.names.grafana
  resource_group_name = module.names.resource_group["obs"]
}

data "azurerm_monitor_workspace" "this" {
  name                = module.names.monitor_workspace
  resource_group_name = module.names.resource_group["obs"]
}

# Prometheus on the Azure Monitor workspace, read as Grafana's managed identity
# (Monitoring Data Reader, stack 04b).
resource "grafana_data_source" "prometheus" {
  type = "prometheus"
  name = "CloudOps Prometheus"
  uid  = "cloudops-prometheus"
  url  = data.azurerm_monitor_workspace.this.query_endpoint
  json_data_encoded = jsonencode({
    httpMethod       = "POST"
    timeInterval     = "30s"
    azureCredentials = { authType = "msi" }
  })
}

resource "grafana_folder" "cloudops" {
  title = "CloudOps ${var.env}"
  uid   = "cloudops-${var.env}"
}

locals {
  api = "job=\"cloudops/api\""

  dashboards = {
    platform-health = {
      title = "Platform health"
      panels = [
        { title = "API requests by route", unit = "reqps", type = "timeseries",
        expr = "sum by (http_route) (rate(http_server_request_duration_count{${local.api}}[5m]))" },
        { title = "API 5xx ratio (SLO: availability 99.5%)", unit = "percentunit", type = "timeseries",
        expr = "sum(rate(http_server_request_duration_count{${local.api}, http_response_status_code=~\"5..\"}[5m])) / sum(rate(http_server_request_duration_count{${local.api}}[5m]))" },
        { title = "API p95 (SLO: under 500 ms)", unit = "s", type = "timeseries",
        expr = "histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_bucket{${local.api}}[5m])))" },
        { title = "API p95 by route (dashboard SLO: under 2 s)", unit = "s", type = "timeseries",
        expr = "histogram_quantile(0.95, sum by (le, http_route) (rate(http_server_request_duration_bucket{${local.api}}[5m])))" },
        { title = "Outbound HTTP p95 by service", unit = "s", type = "timeseries",
        expr = "histogram_quantile(0.95, sum by (le, job) (rate(http_client_request_duration_bucket[5m])))" },
        { title = "Workflow failures by type", unit = "short", type = "timeseries",
        expr = "sum by (workflow_type) (rate(temporal_workflow_failed[5m]))" },
        { title = "Activity p95 by type", unit = "s", type = "timeseries",
        expr = "histogram_quantile(0.95, sum by (le, activity_type) (rate(temporal_activity_execution_latency_bucket[5m])))" },
        { title = "Activity schedule-to-start p95 (saturation)", unit = "s", type = "timeseries",
        expr = "histogram_quantile(0.95, sum by (le, task_queue) (rate(temporal_activity_schedule_to_start_latency_bucket[5m])))" },
        { title = "Security events, 24 h (each one pages)", unit = "short", type = "stat",
        expr = "sum by (event) (increase(cloudops_security_events[24h]))" },
        { title = "Aborted canaries, 7 days", unit = "short", type = "stat",
        expr = "sum(increase(rollout_events_total{reason=\"RolloutAborted\"}[7d]))" },
      ]
    }

    ingest-health = {
      title = "Ingest health"
      panels = [
        { title = "Intervals since last success (outage above 3)", unit = "short", type = "timeseries",
        expr = "cloudops_connector_last_success_age / on(source, entity) cloudops_connector_expected_interval" },
        { title = "Lag (SLO: tickets under 5 min, telemetry under 2 min)", unit = "s", type = "timeseries",
        expr = "cloudops_connector_lag" },
        { title = "Failed runs, last 24 h (share)", unit = "percentunit", type = "timeseries",
        expr = "cloudops_connector_failed_runs_24h / on(source, entity) clamp_min(cloudops_connector_runs_24h, 1)" },
        { title = "Records read per hour", unit = "short", type = "timeseries",
        expr = "sum by (source, entity) (increase(cloudops_connector_records{kind=\"read\"}[1h]))" },
        { title = "Records rejected, last 24 h", unit = "short", type = "timeseries",
        expr = "cloudops_connector_rejected_24h" },
        { title = "Dead-lettered pages waiting", unit = "short", type = "stat",
        expr = "sum by (source, entity) (cloudops_connector_dead_letters_open)" },
        { title = "Runs by outcome", unit = "short", type = "timeseries",
        expr = "sum by (source, status) (increase(cloudops_connector_runs[1h]))" },
        { title = "Health gauges age (stale above 600 s)", unit = "s", type = "stat",
        expr = "cloudops_platform_snapshot_age" },
      ]
    }

    execution = {
      title = "Execution"
      panels = [
        { title = "Execution plane", unit = "none", type = "text",
        text = "The execution plane is not built (CLAUDE.md: not before the security review). These panels read Execute* workflows and stay empty until it exists. Remediation verification rate and approval latency arrive with it (ADR 0022)." },
        { title = "Execution workflows completed", unit = "short", type = "timeseries",
        expr = "sum by (workflow_type) (increase(temporal_workflow_completed{workflow_type=~\"Execute.*\"}[1h]))" },
        { title = "Execution workflows failed", unit = "short", type = "timeseries",
        expr = "sum by (workflow_type) (increase(temporal_workflow_failed{workflow_type=~\"Execute.*\"}[1h]))" },
        { title = "Execution end-to-end p95", unit = "s", type = "timeseries",
        expr = "histogram_quantile(0.95, sum by (le, workflow_type) (rate(temporal_workflow_endtoend_latency_bucket{workflow_type=~\"Execute.*\"}[1h])))" },
        { title = "Triage outcomes (stages 1-3)", unit = "short", type = "timeseries",
        expr = "sum by (agent, status) (increase(cloudops_agent_runs[1h]))" },
        { title = "Unsigned runbook or unapproved mint attempts, 7 days", unit = "short", type = "stat",
        expr = "sum by (event) (increase(cloudops_security_events{event=~\"unsigned_runbook|credential_mint_unapproved\"}[7d]))" },
      ]
    }

    ai-operations = {
      title = "AI operations"
      panels = [
        { title = "Gateway calls by task and outcome", unit = "short", type = "timeseries",
        expr = "sum by (task, outcome) (increase(cloudops_llm_calls[1h]))" },
        { title = "Tokens per hour by task", unit = "short", type = "timeseries",
        expr = "sum by (task, direction) (increase(cloudops_llm_tokens[1h]))" },
        { title = "AI spend per hour (display only)", unit = "none", type = "timeseries",
        expr = "sum by (currency) (increase(cloudops_llm_cost_micros[1h])) / 1e6" },
        { title = "Gateway p95 by task (first-token SLO: under 3 s)", unit = "ms", type = "timeseries",
        expr = "histogram_quantile(0.95, sum by (le, task) (rate(cloudops_llm_duration_bucket[15m])))" },
        { title = "Refusals by code (spend_circuit_open pages)", unit = "short", type = "timeseries",
        expr = "sum by (refusal) (increase(cloudops_llm_calls{outcome=\"refused\"}[1h]))" },
        { title = "Semantic cache hit ratio", unit = "percentunit", type = "timeseries",
        expr = "sum(rate(cloudops_llm_calls{cache_hit=\"true\"}[1h])) / sum(rate(cloudops_llm_calls[1h]))" },
        { title = "Agent runs by status", unit = "short", type = "timeseries",
        expr = "sum by (agent, status) (increase(cloudops_agent_runs[1h]))" },
        { title = "Agent p95 by agent", unit = "ms", type = "timeseries",
        expr = "histogram_quantile(0.95, sum by (le, agent) (rate(cloudops_agent_duration_bucket[1h])))" },
      ]
    }

    data-quality = {
      title = "Data quality"
      panels = [
        { title = "Quarantine depth by kind and source", unit = "short", type = "timeseries",
        expr = "cloudops_quarantine_depth" },
        { title = "Quarantined occurrences", unit = "short", type = "timeseries",
        expr = "cloudops_quarantine_occurrences" },
        { title = "Quarantined records per hour (new)", unit = "short", type = "timeseries",
        expr = "sum by (source) (increase(cloudops_connector_records{kind=\"quarantined\"}[1h]))" },
        { title = "Schema drift waiting for a person", unit = "short", type = "stat",
        expr = "sum by (source, entity) (cloudops_connector_schema_drift_open)" },
        { title = "Reconciliation: largest drift (SLO: under 0.5%)", unit = "percentunit", type = "stat",
        expr = "cloudops_finops_reconciliation_drift_max" },
        { title = "Reconciliation: unverified checks", unit = "short", type = "timeseries",
        expr = "sum by (kind, reason) (cloudops_finops_reconciliation_unverified)" },
        { title = "Reconciliation checks by verdict", unit = "short", type = "timeseries",
        expr = "sum by (status, reason) (increase(cloudops_finops_reconciliation_checks[1d]))" },
      ]
    }

    platform-cost = {
      title = "Platform cost"
      panels = [
        { title = "How these figures are made", unit = "none", type = "text",
        text = "The platform's own Azure cost runs through the customer FinOps code unchanged. The platform runs in one tenant's subscription (the host, ADR 0031); its cost here is the lines of its own resources (rg-cloudops-*), not the whole subscription. Reconciliation and anomalies are the host tenant's. Amounts are month to date, amortized, in their own currency, rounded here for display. When the host's feed has no resource-level lines, the platform's part is not captured." },
        { title = "Month to date by charge class", unit = "none", type = "stat",
        expr = "sum by (charge_class, currency) (cloudops_platform_cost_micros) / 1e6" },
        { title = "Platform's part captured (resource-level lines)", unit = "bool_yes_no", type = "stat",
        expr = "cloudops_platform_cost_resource_lines" },
        { title = "Host tenant reconciles with the provider this month", unit = "bool_yes_no", type = "stat",
        expr = "cloudops_platform_cost_verified" },
        { title = "Host tenant anomalies, 30 days", unit = "short", type = "stat",
        expr = "cloudops_platform_cost_anomalies" },
        { title = "Unallocated share by dimension", unit = "percentunit", type = "stat",
        expr = "cloudops_platform_cost_unallocated_share" },
        { title = "AI spend, 30 days (display only)", unit = "none", type = "stat",
        expr = "sum by (currency) (increase(cloudops_llm_cost_micros[30d])) / 1e6" },
        { title = "Month to date over time", unit = "none", type = "timeseries",
        expr = "sum by (currency) (cloudops_platform_cost_micros) / 1e6" },
      ]
    }
  }
}

resource "grafana_dashboard" "this" {
  for_each  = local.dashboards
  folder    = grafana_folder.cloudops.uid
  overwrite = true
  config_json = jsonencode({
    uid           = "cloudops-${each.key}"
    title         = each.value.title
    tags          = ["cloudops", "step19"]
    timezone      = "utc"
    schemaVersion = 39
    refresh       = "1m"
    time          = { from = "now-24h", to = "now" }
    panels = [for i, p in each.value.panels : merge(
      {
        id      = i + 1
        title   = p.title
        type    = p.type
        gridPos = { h = 8, w = 12, x = (i % 2) * 12, y = floor(i / 2) * 8 }
      },
      # The two panel kinds have different shapes, which a conditional cannot return
      # directly ("inconsistent conditional result types"): encode each branch, then
      # decode the one chosen. The JSON Grafana receives is unchanged.
      jsondecode(p.type == "text" ? jsonencode({
        options = { mode = "markdown", content = p.text }
        }) : jsonencode({
        datasource  = { type = "prometheus", uid = grafana_data_source.prometheus.uid }
        fieldConfig = { defaults = { unit = p.unit }, overrides = [] }
        targets     = [{ refId = "A", expr = p.expr, range = p.type != "stat", instant = p.type == "stat" }]
      }))
    )]
  })
}
