# The raw pages of the read-only sources connected in ADR 0028, written before
# anything parses them (LLD, Connector contract). One container per source, as
# servicenow and azure, so each connector's write role is scoped to its own
# container (04b-rbac, connectors.tf).
#
#   sharepoint  folder snapshots and file content (Microsoft Graph)
#   qualys      host detection pages (in dev, the simulated estate's; ADR 0025)
#   corestack   recommendation snapshots
resource "azurerm_storage_container" "raw_connector" {
  for_each = toset(["sharepoint", "qualys", "corestack"])

  name                  = each.key
  storage_account_id    = azurerm_storage_account.this["bronze"].id
  container_access_type = "private"
}
