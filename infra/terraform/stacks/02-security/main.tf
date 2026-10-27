# Stack 02 — security (LLD, Identity, secrets and keys).
#
# Two vaults: application config and connector tokens (Standard), and the key
# vault (Premium, HSM-backed keys). The execution vault and execution identity
# belong to the execution plane, which is not built before its security
# review. No keys are created here: key-runbook-signing waits on the signing
# ceremony (L9), and creating keys needs data-plane RBAC on the vault.

data "azurerm_client_config" "current" {}

data "azurerm_subnet" "sec" {
  provider             = azurerm.net
  name                 = module.names.subnet["sec"]
  virtual_network_name = module.names.vnet
  resource_group_name  = module.names.resource_group["net"]
}

data "azurerm_private_dns_zone" "vault" {
  provider            = azurerm.net
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = module.names.resource_group["net"]
}

module "tags" {
  source      = "../../modules/tags"
  owner       = var.owner
  environment = var.env
  costcentre  = var.costcentre
  application = var.application
  dataclass   = var.dataclass["security"]
}

resource "azurerm_resource_group" "sec" {
  name     = module.names.resource_group["sec"]
  location = var.location
  tags     = module.tags.tags
}

# CanNotDelete on security groups in production (LLD, Subscriptions).
resource "azurerm_management_lock" "sec" {
  count      = var.env == "prd" ? 1 : 0
  name       = "lock-cannotdelete"
  scope      = azurerm_resource_group.sec.id
  lock_level = "CanNotDelete"
}

locals {
  vaults = {
    app  = "standard"
    keys = "premium" # HSM-backed, non-exportable (LLD, Vaults and keys)
  }
}

resource "azurerm_key_vault" "this" {
  for_each = local.vaults

  name                          = module.names.key_vault[each.key]
  resource_group_name           = azurerm_resource_group.sec.name
  location                      = var.location
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = each.value
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = false
  tags                          = module.tags.tags

  network_acls {
    default_action = "Deny"
    bypass         = "None"
  }
}

module "vault_endpoint" {
  source   = "../../modules/private_endpoint"
  for_each = azurerm_key_vault.this

  service             = "kv${each.key}"
  name_suffix         = module.names.private_endpoint_suffix
  resource_group_name = azurerm_resource_group.sec.name
  location            = var.location
  subnet_id           = data.azurerm_subnet.sec.id
  resource_id         = each.value.id
  subresource         = "vault"
  private_dns_zone_id = data.azurerm_private_dns_zone.vault.id
  tags                = module.tags.tags
}

# One identity per workload, federated to its Kubernetes service account once
# the cluster's OIDC issuer exists (LLD: workload identity, no service
# principals). Role assignments for these live in stack 04b-rbac.
resource "azurerm_user_assigned_identity" "workload" {
  for_each = module.names.workload_identity

  name                = each.value
  resource_group_name = azurerm_resource_group.sec.name
  location            = var.location
  tags                = module.tags.tags
}

# AKS control-plane identity. Needs Network Contributor on its subnets
# (stack 04b-rbac) before stack 05 can create the cluster.
resource "azurerm_user_assigned_identity" "aks" {
  name                = module.names.aks_identity
  resource_group_name = azurerm_resource_group.sec.name
  location            = var.location
  tags                = module.tags.tags
}
