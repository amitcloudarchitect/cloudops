# Stack 04b — role assignments. Every Azure role assignment for stacks 01-05
# lives here and nowhere else, so they can be reviewed as one diff and applied
# by someone holding User Access Administrator (AZURE-ACCESS.md). Apply after
# 04, before 05. Each assignment is the narrowest scope that works.

data "azurerm_user_assigned_identity" "aks" {
  provider            = azurerm.sec
  name                = module.names.aks_identity
  resource_group_name = module.names.resource_group["sec"]
}

data "azurerm_subnet" "aks" {
  for_each             = toset(["aks_system", "aks_app"])
  name                 = module.names.subnet[each.key]
  virtual_network_name = module.names.vnet
  resource_group_name  = module.names.resource_group["net"]
}

# The Event Hubs namespace's managed identity, as published by stack 04. Read
# from its state with the same Entra auth the backend uses, rather than via a
# second provider that needs its own token.
data "terraform_remote_state" "data" {
  backend = "azurerm"
  config = merge(module.names.tfstate, {
    key              = "04-data.tfstate"
    use_azuread_auth = true
  })
}

data "azurerm_storage_account" "bronze" {
  provider            = azurerm.data
  name                = module.names.storage["bronze"]
  resource_group_name = module.names.resource_group["data"]
}

# AKS control plane joins nodes and internal load balancers to its own subnets
# (bring-your-own VNet). Subnet scope, not VNet.
resource "azurerm_role_assignment" "aks_subnet" {
  for_each = data.azurerm_subnet.aks

  scope                            = each.value.id
  role_definition_name             = "Network Contributor"
  principal_id                     = data.azurerm_user_assigned_identity.aks.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# Event Hubs capture writes evh-metrics to bronze with the namespace identity.
# Scoped to the one capture container, not the account.
resource "azurerm_role_assignment" "capture" {
  scope                            = "${data.azurerm_storage_account.bronze.id}/blobServices/default/containers/evh-metrics-capture"
  role_definition_name             = "Storage Blob Data Contributor"
  principal_id                     = data.terraform_remote_state.data.outputs.eventhub_namespace_principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}
