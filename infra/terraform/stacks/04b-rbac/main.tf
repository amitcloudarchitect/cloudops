# Stack 04b — role assignments. Every Azure role assignment for stacks 01-05
# lives here and nowhere else, so they can be reviewed as one diff and applied
# by someone holding User Access Administrator (AZURE-ACCESS.md). Apply after
# 03a, 04 and 04a, before 05, and once more after 05 on a first build (the kubelet's
# AcrPull needs the cluster). Each assignment is the narrowest scope that works.

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

# --- Container registry (stack 04a) -------------------------------------------------

data "azurerm_container_registry" "this" {
  provider            = azurerm.aks
  name                = module.names.container_registry
  resource_group_name = module.names.resource_group["acr"]
}

# The kubelet identity is created by AKS with the cluster (stack 05), which applies
# after this stack. On a first build its output does not exist yet: the default
# below skips the grant, and applying this stack once more after 05 adds it. Read
# from 05's state rather than by name, so a replaced cluster's new identity is
# picked up rather than a stale one.
data "terraform_remote_state" "aks" {
  backend = "azurerm"
  config = merge(module.names.tfstate, {
    key              = "05-aks.tfstate"
    use_azuread_auth = true
  })
  defaults = {
    kubelet_object_id = null
  }
}

resource "azurerm_role_assignment" "kubelet_acr_pull" {
  count = data.terraform_remote_state.aks.outputs.kubelet_object_id == null ? 0 : 1

  scope                            = data.azurerm_container_registry.this.id
  role_definition_name             = "AcrPull"
  principal_id                     = data.terraform_remote_state.aks.outputs.kubelet_object_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# Dev: people who push from their own az login (variables.tf). Registry scope only.
resource "azurerm_role_assignment" "acr_push" {
  for_each = toset(var.acr_push_principal_ids)

  scope                = data.azurerm_container_registry.this.id
  role_definition_name = "AcrPush"
  principal_id         = each.value
}

# --- App Key Vault: workloads read their mounted secrets (CSI driver) -----------

data "azurerm_key_vault" "app" {
  provider            = azurerm.sec
  name                = module.names.key_vault["app"]
  resource_group_name = module.names.resource_group["sec"]
}

data "azurerm_user_assigned_identity" "workload" {
  provider            = azurerm.sec
  for_each            = toset(["api", "ingest", "ai", "workflow"])
  name                = module.names.workload_identity[each.key]
  resource_group_name = module.names.resource_group["sec"]
}

# Vault scope: the vault holds only platform secrets, and per-secret scopes would
# need the secrets to exist first. Read only; nothing in a pod can write a secret.
resource "azurerm_role_assignment" "workload_secrets" {
  for_each = data.azurerm_user_assigned_identity.workload

  scope                            = data.azurerm_key_vault.app.id
  role_definition_name             = "Key Vault Secrets User"
  principal_id                     = each.value.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# Dev: people who write the generated database connection strings
# (scripts/db-dev-logins.sh). Production has nobody here.
resource "azurerm_role_assignment" "dev_secret_writers" {
  for_each = toset(var.dev_secret_writer_ids)

  scope                = data.azurerm_key_vault.app.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = each.value
}

# The ServiceNow connector reads and writes its own bronze container: write for new
# pages, read for replay. Container scope, never the account.
resource "azurerm_role_assignment" "ingest_raw_servicenow" {
  scope                            = "${data.azurerm_storage_account.bronze.id}/blobServices/default/containers/servicenow"
  role_definition_name             = "Storage Blob Data Contributor"
  principal_id                     = data.azurerm_user_assigned_identity.workload["ingest"].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# The AI worker's gateway calls Azure OpenAI as itself (ADR 0012): Cognitive Services
# OpenAI User can call the deployments, not manage them, and there are no keys to
# read (local auth is off). Account scope: it is the only AI resource.
data "azurerm_cognitive_account" "openai" {
  provider            = azurerm.data
  name                = module.names.openai
  resource_group_name = module.names.resource_group["ai"]
}

resource "azurerm_role_assignment" "ai_openai_user" {
  scope                            = data.azurerm_cognitive_account.openai.id
  role_definition_name             = "Cognitive Services OpenAI User"
  principal_id                     = data.azurerm_user_assigned_identity.workload["ai"].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# --- The pipeline (ADR 0013): push images, read for plans, never write ----------
#
# Its own lookup, not part of the workload set above: that set reads the app
# vault's secrets, and the pipeline must never.
data "azurerm_user_assigned_identity" "cicd" {
  provider            = azurerm.sec
  name                = module.names.workload_identity["cicd"]
  resource_group_name = module.names.resource_group["sec"]
}

resource "azurerm_role_assignment" "cicd_acr_push" {
  scope                            = data.azurerm_container_registry.this.id
  role_definition_name             = "AcrPush"
  principal_id                     = data.azurerm_user_assigned_identity.cicd.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# Reader on each platform resource group (LLD: never subscription-wide), so
# `terraform plan` can refresh; Reader cannot read secrets or keys.
locals {
  cicd_read_groups = {
    for k in ["net", "sec", "obs", "data", "msg", "aks", "ai", "acr"] :
    k => module.names.resource_group[k]
  }
}

data "azurerm_resource_group" "cicd_read" {
  for_each = local.cicd_read_groups
  name     = each.value
}

resource "azurerm_role_assignment" "cicd_reader" {
  for_each                         = data.azurerm_resource_group.cicd_read
  scope                            = each.value.id
  role_definition_name             = "Reader"
  principal_id                     = data.azurerm_user_assigned_identity.cicd.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

data "azurerm_resource_group" "aks_nodes" {
  name = module.names.aks_node_resource_group
}

resource "azurerm_role_assignment" "cicd_reader_aks_nodes" {
  scope                            = data.azurerm_resource_group.aks_nodes.id
  role_definition_name             = "Reader"
  principal_id                     = data.azurerm_user_assigned_identity.cicd.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# The state, read only: CI plans with -lock=false and never writes state.
data "azurerm_storage_account" "tfstate" {
  name                = module.names.tfstate.storage_account_name
  resource_group_name = module.names.tfstate.resource_group_name
}

resource "azurerm_role_assignment" "cicd_state_reader" {
  scope                            = "${data.azurerm_storage_account.tfstate.id}/blobServices/default/containers/${module.names.tfstate.container_name}"
  role_definition_name             = "Storage Blob Data Reader"
  principal_id                     = data.azurerm_user_assigned_identity.cicd.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}
