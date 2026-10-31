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

  # KEDA, as the managed add-on (LLD: KEDA on connectors and Temporal workers).
  workload_autoscaler_profile {
    keda_enabled = true
  }

  oms_agent {
    log_analytics_workspace_id      = data.azurerm_log_analytics_workspace.this.id
    msi_auth_for_monitoring_enabled = true
  }

  # Azure Monitor managed Prometheus (step 19, ADR 0022): the ama-metrics add-on
  # scrapes the OpenTelemetry Collector into the workspace of stack 03.
  dynamic "monitor_metrics" {
    for_each = var.observability_enabled ? [1] : []
    content {}
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

# Gateway API ingress (ADR 0011): the managed Gateway API CRDs and the application
# routing add-on's Istio-based implementation (GatewayClass approuting-istio), the
# supported successor to the retired ingress-nginx. Microsoft patches and upgrades it
# with the cluster. azurerm has no fields for these, hence azapi, as Microsoft's own
# Terraform sample does.
resource "azapi_update_resource" "gateway_api" {
  type        = "Microsoft.ContainerService/managedClusters@2026-03-01"
  resource_id = azurerm_kubernetes_cluster.this.id
  body = {
    properties = {
      ingressProfile = {
        gatewayAPI = {
          installation = "Standard"
        }
        webAppRouting = {
          gatewayAPIImplementations = {
            appRoutingIstio = {
              mode = "Enabled"
            }
          }
        }
      }
    }
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

# --- Workload identity federation -------------------------------------------------
#
# Each workload's managed identity (stack 02) trusts exactly one Kubernetes service
# account on this cluster's OIDC issuer. Here rather than in 02 because the issuer
# belongs to the cluster: a replaced cluster gets a new issuer, and these follow.
# The service accounts are in infra/gitops/base (a test keeps the two lists equal).

locals {
  federated_service_accounts = {
    api      = "platform-api/api"
    migrate  = "platform-api/migrate"
    ingest   = "platform-ingest/connector-servicenow"
    ai       = "platform-ai/worker-ai"
    workflow = "temporal/temporal-secrets"
  }
  # Further service accounts (step 19, ADR 0022): the platform metrics job runs on
  # the ingest login, so it is the ingest identity too; the collector has its own,
  # federated once var.observability_enabled says stacks 02 and 03 are applied.
  additional_federated_service_accounts = {
    ingest-platform-metrics      = { identity = "ingest", account = "platform-ingest/platform-metrics" }
    observability-otel-collector = { identity = "observability", account = "observability/otel-collector" }
  }
  additional_enabled = {
    for k, v in local.additional_federated_service_accounts : k => v
    if v.identity != "observability" || var.observability_enabled
  }
}

data "azurerm_user_assigned_identity" "workload" {
  provider            = azurerm.sec
  for_each            = local.federated_service_accounts
  name                = module.names.workload_identity[each.key]
  resource_group_name = module.names.resource_group["sec"]
}

resource "azurerm_federated_identity_credential" "workload" {
  provider            = azurerm.sec
  for_each            = local.federated_service_accounts
  name                = "aks-${replace(each.value, "/", "-")}"
  resource_group_name = module.names.resource_group["sec"]
  parent_id           = data.azurerm_user_assigned_identity.workload[each.key].id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject             = "system:serviceaccount:${replace(each.value, "/", ":")}"
}

data "azurerm_user_assigned_identity" "additional" {
  provider            = azurerm.sec
  for_each            = toset([for v in values(local.additional_enabled) : v.identity])
  name                = module.names.workload_identity[each.key]
  resource_group_name = module.names.resource_group["sec"]
}

resource "azurerm_federated_identity_credential" "additional" {
  provider            = azurerm.sec
  for_each            = local.additional_enabled
  name                = "aks-${replace(each.value.account, "/", "-")}"
  resource_group_name = module.names.resource_group["sec"]
  parent_id           = data.azurerm_user_assigned_identity.additional[each.value.identity].id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject             = "system:serviceaccount:${replace(each.value.account, "/", ":")}"
}

# --- Managed Prometheus (step 19, ADR 0022) --------------------------------------
#
# The add-on's samples go to the Azure Monitor workspace through the workspace's own
# default data collection rule and endpoint, associated with this cluster.

data "azurerm_monitor_workspace" "this" {
  provider            = azurerm.obs
  count               = var.observability_enabled ? 1 : 0
  name                = module.names.monitor_workspace
  resource_group_name = module.names.resource_group["obs"]
}

resource "azurerm_monitor_data_collection_rule_association" "prometheus" {
  count                   = var.observability_enabled ? 1 : 0
  name                    = "dcra-cloudops-prometheus-${var.env}"
  target_resource_id      = azurerm_kubernetes_cluster.this.id
  data_collection_rule_id = data.azurerm_monitor_workspace.this[0].default_data_collection_rule_id
  description             = "Managed Prometheus: ama-metrics to the platform's Azure Monitor workspace."
}

resource "azurerm_monitor_data_collection_rule_association" "prometheus_endpoint" {
  count                       = var.observability_enabled ? 1 : 0
  target_resource_id          = azurerm_kubernetes_cluster.this.id
  data_collection_endpoint_id = data.azurerm_monitor_workspace.this[0].default_data_collection_endpoint_id
  description                 = "Managed Prometheus: the workspace's ingestion endpoint."
}
