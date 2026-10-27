output "key_vault_ids" {
  value = { for k, v in azurerm_key_vault.this : k => v.id }
}

output "workload_identity_client_ids" {
  description = "Client ids for Kubernetes service account annotations. Not secrets."
  value       = { for k, v in azurerm_user_assigned_identity.workload : k => v.client_id }
}

output "aks_identity_id" {
  value = azurerm_user_assigned_identity.aks.id
}
