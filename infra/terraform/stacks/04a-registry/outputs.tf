output "login_server" {
  description = "Push and pull address, e.g. crcloudopsdevweu.azurecr.io."
  value       = azurerm_container_registry.this.login_server
}

output "registry_id" {
  value = azurerm_container_registry.this.id
}
