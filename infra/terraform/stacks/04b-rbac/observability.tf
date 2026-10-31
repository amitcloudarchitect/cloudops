# Step 19 (ADR 0021): the roles observability needs, behind var.observability_enabled
# because each names an identity or resource stacks 02 and 03 create.
#
#   id-cloudops-observability   Monitoring Metrics Publisher on Application Insights:
#                               the collector exports traces and logs with Entra ID
#                               (local authentication is off on the resource).
#   Grafana (system identity)   Monitoring Data Reader on the Azure Monitor workspace
#                               (Prometheus queries); Monitoring Reader on the
#                               observability resource group (Log Analytics, App Insights).
#   people                      Grafana Admin / Grafana Viewer on the Grafana instance.

data "azurerm_user_assigned_identity" "observability" {
  provider            = azurerm.sec
  count               = var.observability_enabled ? 1 : 0
  name                = module.names.workload_identity["observability"]
  resource_group_name = module.names.resource_group["sec"]
}

data "azurerm_application_insights" "this" {
  provider            = azurerm.obs
  count               = var.observability_enabled ? 1 : 0
  name                = module.names.app_insights
  resource_group_name = module.names.resource_group["obs"]
}

data "azurerm_monitor_workspace" "this" {
  provider            = azurerm.obs
  count               = var.observability_enabled ? 1 : 0
  name                = module.names.monitor_workspace
  resource_group_name = module.names.resource_group["obs"]
}

data "azurerm_dashboard_grafana" "this" {
  provider            = azurerm.obs
  count               = var.observability_enabled ? 1 : 0
  name                = module.names.grafana
  resource_group_name = module.names.resource_group["obs"]
}

data "azurerm_resource_group" "obs" {
  provider = azurerm.obs
  count    = var.observability_enabled ? 1 : 0
  name     = module.names.resource_group["obs"]
}

resource "azurerm_role_assignment" "collector_publisher" {
  count                            = var.observability_enabled ? 1 : 0
  scope                            = data.azurerm_application_insights.this[0].id
  role_definition_name             = "Monitoring Metrics Publisher"
  principal_id                     = data.azurerm_user_assigned_identity.observability[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "grafana_prometheus" {
  count                            = var.observability_enabled ? 1 : 0
  scope                            = data.azurerm_monitor_workspace.this[0].id
  role_definition_name             = "Monitoring Data Reader"
  principal_id                     = data.azurerm_dashboard_grafana.this[0].identity[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "grafana_monitoring" {
  count                            = var.observability_enabled ? 1 : 0
  scope                            = data.azurerm_resource_group.obs[0].id
  role_definition_name             = "Monitoring Reader"
  principal_id                     = data.azurerm_dashboard_grafana.this[0].identity[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "grafana_admin" {
  for_each             = var.observability_enabled ? toset(var.grafana_admin_principal_ids) : toset([])
  scope                = data.azurerm_dashboard_grafana.this[0].id
  role_definition_name = "Grafana Admin"
  principal_id         = each.value
}

resource "azurerm_role_assignment" "grafana_viewer" {
  for_each             = var.observability_enabled ? toset(var.grafana_viewer_principal_ids) : toset([])
  scope                = data.azurerm_dashboard_grafana.this[0].id
  role_definition_name = "Grafana Viewer"
  principal_id         = each.value
}
