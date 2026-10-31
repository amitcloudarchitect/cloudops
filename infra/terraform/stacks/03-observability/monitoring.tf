# Step 19 (LLD, Observability; ADR 0022): Azure Monitor managed Prometheus and Azure
# Managed Grafana. The workspace's default data collection rule receives the AKS
# ama-metrics add-on's scrape (associated in stack 05); Grafana reads the workspace
# and Log Analytics as its own managed identity (roles in stack 04b).
#
# Public network access follows network_mode: dev ("public") reaches both over the
# internet with Entra ID; the LLD's private model ("private") puts them behind
# private endpoints, which arrive with the private build (ADR 0022, deferred).

resource "azurerm_monitor_workspace" "this" {
  name                          = module.names.monitor_workspace
  resource_group_name           = azurerm_resource_group.obs.name
  location                      = var.location
  public_network_access_enabled = var.network_mode == "public"
  tags                          = module.tags.tags
}

resource "azurerm_dashboard_grafana" "this" {
  name                          = module.names.grafana
  resource_group_name           = azurerm_resource_group.obs.name
  location                      = var.location
  grafana_major_version         = "11"
  sku                           = "Standard"
  api_key_enabled               = false # Entra ID only
  public_network_access_enabled = var.network_mode == "public"
  zone_redundancy_enabled       = var.env == "prd"
  tags                          = module.tags.tags

  identity {
    type = "SystemAssigned"
  }

  azure_monitor_workspace_integrations {
    resource_id = azurerm_monitor_workspace.this.id
  }
}
