output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.this.fqdn
}

output "storage_account_ids" {
  value = { for k, v in azurerm_storage_account.this : k => v.id }
}

output "eventhub_namespace_principal_id" {
  value = azurerm_eventhub_namespace.this.identity[0].principal_id
}

# Each platform cost export's own identity (ADR 0023): stack 04b lets it write the
# export container, nothing else.
output "platform_cost_export_principal_ids" {
  value = { for k, v in azapi_resource.platform_cost_export : k => v.identity[0].principal_id }
}

output "platform_cost_export_roots" {
  description = "CLOUDOPS_AZURE_COST_EXPORT_ROOTS for the connector's overlay."
  value = join(",", [for k, v in azapi_resource.platform_cost_export :
  "${azurerm_storage_container.cost_exports_platform.name}/${local.platform_cost_root}/${v.name}"])
}
