output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.this.id
}

output "app_insights_connection_string" {
  description = "Not a secret with local authentication disabled; ingestion requires Entra ID."
  value       = azurerm_application_insights.this.connection_string
  sensitive   = true
}
