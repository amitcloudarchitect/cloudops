# The platform's own cost (ADR 0023): the roles the live feed into the internal tenant
# needs. The export and both containers are stack 04-data's.
#
#   each export's own identity  Storage Blob Data Contributor on cost-exports-platform:
#                               Cost Management writes the export as itself
#   id-cloudops-ingest          Storage Blob Data Reader on cost-exports-platform
#                               Storage Blob Data Contributor on azure (its raw pages)
#                               Cost Management Reader on each platform subscription:
#                               the month-to-date query reconciliation checks against.
#                               The query API's scope is the subscription; the role
#                               reads cost, never a resource's configuration or data.
#                               Subscription scope is a recorded exception to the LLD
#                               (ADR 0023), admitted by name in policy/terraform.

locals {
  bronze_containers = "${data.azurerm_storage_account.bronze.id}/blobServices/default/containers"
  platform_subscriptions = toset(distinct([
    for d in ["net", "sec", "obs", "data", "aks"] : local.subscription[d]
  ]))
}

resource "azurerm_role_assignment" "platform_cost_export_writer" {
  for_each = data.terraform_remote_state.data.outputs.platform_cost_export_principal_ids

  scope                            = "${local.bronze_containers}/cost-exports-platform"
  role_definition_name             = "Storage Blob Data Contributor"
  principal_id                     = each.value
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "ingest_platform_cost_reader" {
  scope                            = "${local.bronze_containers}/cost-exports-platform"
  role_definition_name             = "Storage Blob Data Reader"
  principal_id                     = data.azurerm_user_assigned_identity.workload["ingest"].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "ingest_raw_azure" {
  scope                            = "${local.bronze_containers}/azure"
  role_definition_name             = "Storage Blob Data Contributor"
  principal_id                     = data.azurerm_user_assigned_identity.workload["ingest"].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "ingest_cost_management_reader" {
  for_each = local.platform_subscriptions

  scope                            = "/subscriptions/${each.value}"
  role_definition_name             = "Cost Management Reader"
  principal_id                     = data.azurerm_user_assigned_identity.workload["ingest"].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# CI plans this stack with refresh, and refreshing the subscription-scope assignment
# above reads role assignments at subscription scope, where the pipeline identity has
# nothing (Reader on the platform resource groups only, never subscription-wide). This
# role reads role assignments and definitions and nothing else: no resource, no cost,
# no data.
resource "azurerm_role_definition" "role_assignment_reader" {
  name        = "CloudOps role assignment reader (${var.env})"
  scope       = "/subscriptions/${local.subscription["net"]}"
  description = "Reads role assignments and role definitions only, so CI can refresh stack 04b (ADR 0023)."

  permissions {
    actions = [
      "Microsoft.Authorization/roleAssignments/read",
      "Microsoft.Authorization/roleDefinitions/read",
    ]
    not_actions = []
  }

  assignable_scopes = [for s in local.platform_subscriptions : "/subscriptions/${s}"]
}

resource "azurerm_role_assignment" "cicd_role_assignment_reader" {
  for_each = local.platform_subscriptions

  scope = "/subscriptions/${each.value}"
  # By name, so the plan shows it to conftest's exception (ADR 0023); the definition
  # exists before the assignment looks it up.
  role_definition_name             = azurerm_role_definition.role_assignment_reader.name
  principal_id                     = data.azurerm_user_assigned_identity.cicd.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}
