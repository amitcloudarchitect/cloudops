# Stack 05 — AKS (LLD, Compute). Private cluster (or, in dev with network_mode =
# "public", a public API server limited to authorised IPs), Workload Identity, Entra ID
# RBAC only, Azure CNI overlay with Cilium, every pool across zones 1-3, Azure
# Linux, no spot. npexec is excluded: it belongs to the execution plane, in its
# own subscription and VNet, after the security review.
#
# Requires stack 04b-rbac (Network Contributor on the AKS subnets) first.

data "azurerm_client_config" "current" {}

data "azurerm_subnet" "aks" {
  provider             = azurerm.net
  for_each             = toset(["aks_system", "aks_app"])
  name                 = module.names.subnet[each.key]
  virtual_network_name = module.names.vnet
  resource_group_name  = module.names.resource_group["net"]
}

data "azurerm_user_assigned_identity" "aks" {
  provider            = azurerm.sec
  name                = module.names.aks_identity
  resource_group_name = module.names.resource_group["sec"]
}

data "azurerm_log_analytics_workspace" "this" {
  provider            = azurerm.obs
  name                = module.names.log_analytics
  resource_group_name = module.names.resource_group["obs"]
}

module "tags" {
  source      = "../../modules/tags"
  owner       = var.owner
  environment = var.env
  costcentre  = var.costcentre
  application = var.application
  dataclass   = var.dataclass["aks"]
}

resource "azurerm_resource_group" "aks" {
  name     = module.names.resource_group["aks"]
  location = var.location
  tags     = module.tags.tags
}

locals {
  zones = ["1", "2", "3"]

  # LLD pool table, minus npexec. npsystem is the default pool below.
  user_pools = {
    npapp       = { purpose = "app" }
    npingest    = { purpose = "ingest" }
    nptelemetry = { purpose = "telemetry" }
    npai        = { purpose = "ai" }
  }
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = module.names.aks
  resource_group_name = azurerm_resource_group.aks.name
  location            = var.location
  node_resource_group = module.names.aks_node_resource_group
  dns_prefix          = module.names.aks

  sku_tier = "Standard" # uptime SLA
  # Changing network_mode replaces the cluster: Azure cannot convert a private
  # cluster to public (or back) in place without API server VNet integration.
  private_cluster_enabled   = var.network_mode == "private"
  private_dns_zone_id       = var.network_mode == "private" ? "System" : null
  automatic_upgrade_channel = "stable" # N-1 of the latest supported minor
  node_os_upgrade_channel   = "NodeImage"

  dynamic "api_server_access_profile" {
    # Public with an empty list: no IP restriction (dev only; see the variable).
    for_each = var.network_mode == "public" && length(var.api_server_authorized_ip_ranges) > 0 ? [1] : []
    content {
      authorized_ip_ranges = var.api_server_authorized_ip_ranges
    }
  }

  oidc_issuer_enabled          = true
  workload_identity_enabled    = true
  local_account_disabled       = true
  run_command_enabled          = var.run_command_enabled
  azure_policy_enabled         = true
  image_cleaner_enabled        = true
  image_cleaner_interval_hours = 48

  role_based_access_control_enabled = true
  azure_active_directory_role_based_access_control {
    azure_rbac_enabled     = true
    tenant_id              = data.azurerm_client_config.current.tenant_id
    admin_group_object_ids = var.aks_admin_group_object_ids
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [data.azurerm_user_assigned_identity.aks.id]
  }

  default_node_pool {
    name                         = "npsystem"
    vm_size                      = var.node_pools["npsystem"].vm_size
    auto_scaling_enabled         = true
    min_count                    = var.node_pools["npsystem"].min
    max_count                    = var.node_pools["npsystem"].max
    zones                        = local.zones
    os_sku                       = "AzureLinux"
    only_critical_addons_enabled = true # CriticalAddonsOnly taint
    vnet_subnet_id               = data.azurerm_subnet.aks["aks_system"].id
    temporary_name_for_rotation  = "npsystemtmp"
    tags                         = module.tags.tags

    upgrade_settings {
      max_surge = "33%"
    }
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "cilium"
    network_data_plane  = "cilium"
    outbound_type       = "userAssignedNATGateway"
    load_balancer_sku   = "standard"
    pod_cidr            = var.pod_cidr
    service_cidr        = var.service_cidr
    dns_service_ip      = var.dns_service_ip
  }

  key_vault_secrets_provider {
    secret_rotation_enabled = true
  }

  oms_agent {
    log_analytics_workspace_id      = data.azurerm_log_analytics_workspace.this.id
    msi_auth_for_monitoring_enabled = true
  }

  microsoft_defender {
    log_analytics_workspace_id = data.azurerm_log_analytics_workspace.this.id
  }

  maintenance_window_auto_upgrade {
    frequency   = "Weekly"
    interval    = 1
    day_of_week = var.maintenance_window.day_of_week
    start_time  = var.maintenance_window.start_time
    duration    = var.maintenance_window.duration
    utc_offset  = "+00:00"
  }

  maintenance_window_node_os {
    frequency   = "Weekly"
    interval    = 1
    day_of_week = var.maintenance_window.day_of_week
    start_time  = var.maintenance_window.start_time
    duration    = var.maintenance_window.duration
    utc_offset  = "+00:00"
  }

  tags = module.tags.tags

  lifecycle {
    ignore_changes = [
      default_node_pool[0].node_count, # owned by the autoscaler
      kubernetes_version,              # owned by the stable upgrade channel
    ]
  }
}

resource "azurerm_kubernetes_cluster_node_pool" "this" {
  for_each = local.user_pools

  name                  = each.key
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  mode                  = "User"
  vm_size               = var.node_pools[each.key].vm_size
  auto_scaling_enabled  = true
  min_count             = var.node_pools[each.key].min
  max_count             = var.node_pools[each.key].max
  zones                 = local.zones
  os_sku                = "AzureLinux"
  vnet_subnet_id        = data.azurerm_subnet.aks["aks_app"].id
  node_labels           = { "cloudops.getronics.com/pool" = each.value.purpose }
  tags                  = module.tags.tags

  upgrade_settings {
    max_surge = "33%"
  }

  lifecycle {
    ignore_changes = [node_count]
  }
}

resource "azurerm_monitor_diagnostic_setting" "aks" {
  name                       = "diag-${module.names.log_analytics}"
  target_resource_id         = azurerm_kubernetes_cluster.this.id
  log_analytics_workspace_id = data.azurerm_log_analytics_workspace.this.id

  enabled_log {
    category = "kube-audit-admin"
  }

  enabled_log {
    category = "guard"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}
