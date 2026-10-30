output "endpoint" {
  description = "The account's Entra-authenticated endpoint, reachable only over the private endpoint."
  value       = azurerm_cognitive_account.openai.endpoint
}

output "account_id" {
  value = azurerm_cognitive_account.openai.id
}

output "deployments" {
  description = "Tier to deployment name: what application code asks for."
  value       = { for tier, d in azurerm_cognitive_deployment.tier : tier => d.name }
}
