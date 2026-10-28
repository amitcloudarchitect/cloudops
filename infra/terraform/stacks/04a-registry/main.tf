# Stack 04a — container registry (LLD, Compute: images only from the platform
# registry). Its own stack and resource group, so replacing the cluster never takes
# the images with it. Pull and push rights are role assignments, so they live in
# 04b-rbac; nothing here grants access.
#
# Dev departure (README): Basic SKU with public network access, chosen for cost.
# The LLD's registry is private (Premium, private endpoint, privatelink.azurecr.io
# zone in stack 01). Authentication is Entra ID only either way: the admin user is
# off and anonymous pull is off, so being reachable is not being readable.

module "tags" {
  source      = "../../modules/tags"
  owner       = var.owner
  environment = var.env
  costcentre  = var.costcentre
  application = var.application
  dataclass   = var.dataclass["aks"]
}

data "azurerm_log_analytics_workspace" "this" {
  provider            = azurerm.obs
  name                = module.names.log_analytics
  resource_group_name = module.names.resource_group["obs"]
}

resource "azurerm_resource_group" "acr" {
  name     = module.names.resource_group["acr"]
  location = var.location
  tags     = module.tags.tags
}

resource "azurerm_container_registry" "this" {
  name                          = module.names.container_registry
  resource_group_name           = azurerm_resource_group.acr.name
  location                      = var.location
  sku                           = "Basic"
  admin_enabled                 = false # no shared username and password, ever
  anonymous_pull_enabled        = false
  public_network_access_enabled = true # dev only, see header
  tags                          = module.tags.tags
}

resource "azurerm_monitor_diagnostic_setting" "acr" {
  name                       = "diag-to-law"
  target_resource_id         = azurerm_container_registry.this.id
  log_analytics_workspace_id = data.azurerm_log_analytics_workspace.this.id

  enabled_log {
    category = "ContainerRegistryLoginEvents"
  }
  enabled_log {
    category = "ContainerRegistryRepositoryEvents"
  }
  enabled_metric {
    category = "AllMetrics"
  }
}
