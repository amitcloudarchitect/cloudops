# Step 19: alert rules as code (LLD, Observability; ADR 0022).
#
#   Sev0  always pages, any hour: a cross-tenant query attempt, an unsigned runbook
#         execution attempt, a credential mint without a valid approval, a signing-key
#         operation outside the expected pattern, a break-glass sign-in. Log rules on
#         this workspace: the services' security events (AppTraces, structured fields
#         security_event and source), Key Vault's audit log, Entra's sign-in log.
#   Sev1  P2, pages: connector outage beyond three intervals, reconciliation drift,
#         rollback rate breach, the AI spend circuit breaker. Prometheus rules.
#   Sev3  SLO warnings, notify only: the LLD's SLO table where a signal exists.
#
# Metric names are the collector's (UnderscoreEscapingWithoutSuffixes: no _total, no
# unit suffix); service metrics carry job="cloudops/<service>". No rule names a
# tenant: no metric carries one.

locals {
  page_webhooks = nonsensitive(keys(var.page_webhook_receivers))
}

resource "azurerm_monitor_action_group" "page" {
  name                = module.names.action_group["page"]
  resource_group_name = azurerm_resource_group.obs.name
  short_name          = "cloudopspage"
  tags                = module.tags.tags

  dynamic "email_receiver" {
    for_each = var.page_email_receivers
    content {
      name                    = email_receiver.key
      email_address           = email_receiver.value
      use_common_alert_schema = true
    }
  }

  dynamic "webhook_receiver" {
    for_each = toset(local.page_webhooks)
    content {
      name                    = webhook_receiver.key
      service_uri             = var.page_webhook_receivers[webhook_receiver.key]
      use_common_alert_schema = true
    }
  }
}

resource "azurerm_monitor_action_group" "notify" {
  name                = module.names.action_group["notify"]
  resource_group_name = azurerm_resource_group.obs.name
  short_name          = "cloudopsinfo"
  tags                = module.tags.tags

  dynamic "email_receiver" {
    for_each = var.notify_email_receivers
    content {
      name                    = email_receiver.key
      email_address           = email_receiver.value
      use_common_alert_schema = true
    }
  }
}

# --- Sev0: always pages ------------------------------------------------------------

data "azurerm_user_assigned_identity" "cicd" {
  provider            = azurerm.sec
  name                = module.names.workload_identity["cicd"]
  resource_group_name = module.names.resource_group["sec"]
}

locals {
  # The services' security events (cloudops_observability.security). Each record
  # names what happened and where, never whom: the trace id leads to the audit trail.
  security_events = {
    cross-tenant-attempt = {
      event       = "cross_tenant_attempt"
      description = "A query tried to reach another tenant's data (tenancy guard, row-level security, telemetry store wrapper)."
    }
    unsigned-runbook = {
      event       = "unsigned_runbook"
      description = "An attempt to execute a runbook without a valid signature (execution plane)."
    }
    credential-mint-unapproved = {
      event       = "credential_mint_unapproved"
      description = "An attempt to mint a customer credential without a valid approval (execution plane)."
    }
  }

  # LLD: the vault grants sign to the pipeline identity alone, so the expected
  # pattern is KeySign by id-cloudops-cicd, and verification by the execution plane
  # once it exists. Anything else on the runbook signing key pages: another caller,
  # a management operation (rotate, import, backup, delete), or a refusal.
  signing_key_query = <<-KQL
    let pipeline = "${data.azurerm_user_assigned_identity.cicd.principal_id}";
    let verifiers = dynamic([${join(", ", [for id in var.signing_key_verifier_object_ids : "\"${id}\""])}]);
    AzureDiagnostics
    | where ResourceProvider == "MICROSOFT.KEYVAULT" and Resource == "${upper(module.names.key_vault["keys"])}"
    | where Category == "AuditEvent" and id_s has "/keys/key-runbook-signing"
    | where not(OperationName == "KeySign" and identity_claim_oid_g == pipeline and ResultSignature == "OK")
    | where not(OperationName in ("KeyVerify", "KeyGet") and identity_claim_oid_g in (verifiers) and ResultSignature == "OK")
    | project TimeGenerated, OperationName, ResultSignature, identity_claim_oid_g
  KQL

  break_glass_query = <<-KQL
    SigninLogs
    | where UserId in (dynamic([${join(", ", [for id in var.break_glass_object_ids : "\"${id}\""])}]))
    | project TimeGenerated, UserId, ResultType, AppDisplayName
  KQL
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "security_event" {
  for_each = local.security_events

  name                 = "alert-cloudops-${each.key}-${var.env}"
  display_name         = "Sev0 ${each.value.event}"
  description          = each.value.description
  resource_group_name  = azurerm_resource_group.obs.name
  location             = var.location
  scopes               = [azurerm_log_analytics_workspace.this.id]
  severity             = 0
  evaluation_frequency = "PT5M"
  window_duration      = "PT5M"
  # A table appears with its first row: validating against an empty workspace fails.
  skip_query_validation   = true
  auto_mitigation_enabled = false
  tags                    = module.tags.tags

  criteria {
    query                   = "AppTraces | where tostring(Properties.security_event) == \"${each.value.event}\""
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0
  }

  action {
    action_groups = [azurerm_monitor_action_group.page.id]
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "signing_key" {
  name                    = "alert-cloudops-signing-key-${var.env}"
  display_name            = "Sev0 signing key operation outside the expected pattern"
  description             = "key-runbook-signing used by anyone but the pipeline (sign) or the execution plane (verify), managed, or refused."
  resource_group_name     = azurerm_resource_group.obs.name
  location                = var.location
  scopes                  = [azurerm_log_analytics_workspace.this.id]
  severity                = 0
  evaluation_frequency    = "PT5M"
  window_duration         = "PT5M"
  skip_query_validation   = true
  auto_mitigation_enabled = false
  tags                    = module.tags.tags

  criteria {
    query                   = local.signing_key_query
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0
  }

  action {
    action_groups = [azurerm_monitor_action_group.page.id]
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "break_glass" {
  count = var.entra_signin_logs_exported && length(var.break_glass_object_ids) > 0 ? 1 : 0

  name                    = "alert-cloudops-break-glass-${var.env}"
  display_name            = "Sev0 break-glass sign-in"
  description             = "A break-glass account signed in (or tried to)."
  resource_group_name     = azurerm_resource_group.obs.name
  location                = var.location
  scopes                  = [azurerm_log_analytics_workspace.this.id]
  severity                = 0
  evaluation_frequency    = "PT5M"
  window_duration         = "PT5M"
  skip_query_validation   = true
  auto_mitigation_enabled = false
  tags                    = module.tags.tags

  criteria {
    query                   = local.break_glass_query
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0
  }

  action {
    action_groups = [azurerm_monitor_action_group.page.id]
  }
}

# --- Sev1 (P2) and Sev3 (SLO): Prometheus rules ----------------------------------------

locals {
  p2_rules = {
    ConnectorOutage = {
      # LLD: connector outage beyond three intervals.
      expr = "cloudops_connector_last_success_age > on(source, entity) (3 * cloudops_connector_expected_interval)"
      for  = "PT5M"
      text = "{{ $labels.source }}/{{ $labels.entity }} has not succeeded for more than three intervals."
    }
    ConnectorNeverSucceeded = {
      expr = "cloudops_connector_expected_interval unless on(source, entity) cloudops_connector_last_success_age"
      for  = "PT1H"
      text = "{{ $labels.source }}/{{ $labels.entity }} is scheduled but has never succeeded."
    }
    ReconciliationDrift = {
      # LLD SLO: cost reconciliation drift under 0.5% monthly.
      expr = "cloudops_finops_reconciliation_drift_max > 0.005"
      for  = "PT15M"
      text = "A cost reconciliation check drifted more than 0.5% from the provider's figure."
    }
    RollbackRateBreach = {
      # ADR 0022: two or more aborted canaries in a day breaches the rollback rate.
      expr = "sum(increase(rollout_events_total{reason=\"RolloutAborted\"}[1d])) >= 2"
      # Azure's minimum hold: Prometheus rule groups refuse "for" under one minute.
      for  = "PT1M"
      text = "Two or more canaries were aborted and rolled back in the last day."
    }
    AiSpendCircuitBreaker = {
      expr = "sum(increase(cloudops_llm_calls{refusal=\"spend_circuit_open\"}[10m])) > 0"
      for  = "PT1M" # the minimum Azure accepts
      text = "The AI gateway's spend circuit breaker is open: calls are being refused."
    }
    PlatformMetricsStale = {
      expr = "cloudops_platform_snapshot_age > 600 or absent(cloudops_platform_snapshot_age)"
      for  = "PT10M"
      text = "Connector health and FinOps gauges have not refreshed for ten minutes: the outage alerts above are blind."
    }
  }

  slo_rules = {
    ApiLatencyP95 = {
      # LLD SLO: API p95 under 500 ms.
      expr = "histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_bucket{job=\"cloudops/api\"}[5m]))) > 0.5"
      text = "API p95 latency above 500 ms."
    }
    DashboardLatencyP95 = {
      # LLD SLO: dashboard p95 under 2 s, measured at the API routes the console reads.
      expr = "histogram_quantile(0.95, sum by (le, http_route) (rate(http_server_request_duration_bucket{job=\"cloudops/api\", http_route=~\"/v1/.*\"}[15m]))) > 2"
      text = "A console route's p95 is above 2 s: {{ $labels.http_route }}."
    }
    AvailabilityFastBurn = {
      # LLD SLO: 99.5% availability; 14.4x burn over an hour spends 2% of a month's budget.
      expr = "(sum(rate(http_server_request_duration_count{job=\"cloudops/api\", http_response_status_code=~\"5..\"}[1h])) / sum(rate(http_server_request_duration_count{job=\"cloudops/api\"}[1h]))) > 14.4 * 0.005"
      text = "The API is spending its availability error budget fast."
    }
    AiLatencyP95 = {
      # LLD SLO: AI first token under 3 s. The gateway does not stream yet, so the
      # whole call stands in for first token: an upper bound.
      expr = "histogram_quantile(0.95, sum by (le) (rate(cloudops_llm_duration_bucket{outcome=\"succeeded\"}[15m]))) > 3000"
      text = "AI gateway p95 latency above 3 s."
    }
    TicketIngestLag = {
      # LLD SLO: ticket ingest lag under 5 minutes.
      expr = "max(cloudops_connector_lag{source=\"servicenow\"}) > 300"
      text = "ServiceNow ingest lag above 5 minutes."
    }
    TelemetryIngestLag = {
      # LLD SLO: telemetry ingest lag under 2 minutes.
      expr = "max(cloudops_connector_lag{source=\"telemetry\"}) > 120"
      text = "Telemetry ingest lag above 2 minutes."
    }
    QuarantineRising = {
      # LLD: quarantine depth is an alerting metric (an unannounced upstream rename).
      expr = "sum by (kind, source) (delta(cloudops_quarantine_depth[6h])) > 0"
      text = "{{ $labels.kind }} quarantine for {{ $labels.source }} grew in the last 6 hours."
    }
  }
}

resource "azurerm_monitor_alert_prometheus_rule_group" "p2" {
  name                = "prg-cloudops-p2-${var.env}"
  resource_group_name = azurerm_resource_group.obs.name
  location            = var.location
  scopes              = [azurerm_monitor_workspace.this.id]
  description         = "P2: pages (LLD, Observability)."
  interval            = "PT1M"
  rule_group_enabled  = true
  tags                = module.tags.tags

  dynamic "rule" {
    for_each = local.p2_rules
    content {
      alert       = rule.key
      expression  = rule.value.expr
      for         = rule.value.for
      severity    = 1
      enabled     = true
      annotations = { summary = rule.value.text }
      labels      = { priority = "P2" }
      action {
        action_group_id = azurerm_monitor_action_group.page.id
      }
      alert_resolution {
        auto_resolved   = true
        time_to_resolve = "PT10M"
      }
    }
  }
}

resource "azurerm_monitor_alert_prometheus_rule_group" "slo" {
  name                = "prg-cloudops-slo-${var.env}"
  resource_group_name = azurerm_resource_group.obs.name
  location            = var.location
  scopes              = [azurerm_monitor_workspace.this.id]
  description         = "SLO warnings: notify, never page (LLD, Observability)."
  interval            = "PT1M"
  rule_group_enabled  = true
  tags                = module.tags.tags

  dynamic "rule" {
    for_each = local.slo_rules
    content {
      alert       = rule.key
      expression  = rule.value.expr
      for         = "PT10M"
      severity    = 3
      enabled     = true
      annotations = { summary = rule.value.text }
      labels      = { priority = "SLO" }
      action {
        action_group_id = azurerm_monitor_action_group.notify.id
      }
      alert_resolution {
        auto_resolved   = true
        time_to_resolve = "PT10M"
      }
    }
  }
}
