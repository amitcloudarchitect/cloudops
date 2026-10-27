# Stack 03 — observability (LLD, Observability). Log Analytics with 90 days hot
# retention, workspace-based Application Insights, and diagnostics for the key
# vaults, which exist before this workspace does (LLD build order). Later stacks
# wire their own diagnostics to this workspace.

module "tags" {
  source      = "../../modules/tags"
  owner       = var.owner
  environment = var.env
  costcentre  = var.costcentre
  application = var.application
  dataclass   = var.dataclass["observability"]
}

resource "azurerm_resource_group" "obs" {
  name     = module.names.resource_group["obs"]
  location = var.location
  tags     = module.tags.tags
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = module.names.log_analytics
  resource_group_name = azurerm_resource_group.obs.name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = 90
  # Entra ID only for ingestion and query; no workspace shared keys in use.
  local_authentication_enabled = false
  tags                         = module.tags.tags
}

resource "azurerm_application_insights" "this" {
  name                         = module.names.app_insights
  resource_group_name          = azurerm_resource_group.obs.name
  location                     = var.location
  workspace_id                 = azurerm_log_analytics_workspace.this.id
  application_type             = "web"
  local_authentication_enabled = false
  tags                         = module.tags.tags
}

data "azurerm_key_vault" "this" {
  provider            = azurerm.sec
  for_each            = toset(["app", "keys"])
  name                = module.names.key_vault[each.key]
  resource_group_name = module.names.resource_group["sec"]
}

# Every key operation is recorded: signing-key use outside the expected
# pattern is one of the alerts that always pages (LLD, Observability).
resource "azurerm_monitor_diagnostic_setting" "key_vault" {
  for_each = data.azurerm_key_vault.this

  name                       = "diag-${module.names.log_analytics}"
  target_resource_id         = each.value.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log {
    category_group = "audit"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}
