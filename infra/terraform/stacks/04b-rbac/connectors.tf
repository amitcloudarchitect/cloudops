# The read-only sources of ADR 0028: the ingest identity reads and writes each one's
# own bronze container (write for new pages, read for replay), as servicenow and
# azure. Container scope, never the account. The containers are 04-data's
# (connectors.tf). Nothing else is granted: each source is read with its own
# credential (a federated app registration for SharePoint, a Key Vault secret the
# workload identity can already read for Qualys and CoreStack), never an Azure role.
resource "azurerm_role_assignment" "ingest_raw_connector" {
  for_each = toset(["sharepoint", "qualys", "corestack"])

  scope                            = "${data.azurerm_storage_account.bronze.id}/blobServices/default/containers/${each.key}"
  role_definition_name             = "Storage Blob Data Contributor"
  principal_id                     = data.azurerm_user_assigned_identity.workload["ingest"].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}
