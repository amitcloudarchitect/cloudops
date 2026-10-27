output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.this.fqdn
}

output "storage_account_ids" {
  value = { for k, v in azurerm_storage_account.this : k => v.id }
}

output "eventhub_namespace_principal_id" {
  value = azurerm_eventhub_namespace.this.identity[0].principal_id
}
