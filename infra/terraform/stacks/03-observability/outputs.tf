output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.this.id
}

output "app_insights_connection_string" {
  description = "Not a secret with local authentication disabled; ingestion requires Entra ID."
  value       = azurerm_application_insights.this.connection_string
  sensitive   = true
}

# Links for the console's Observability page (CLOUDOPS_WEB_GRAFANA_URL and
# CLOUDOPS_WEB_APP_INSIGHTS_URL in the web overlay). Addresses, not credentials.
output "grafana_endpoint" {
  value = azurerm_dashboard_grafana.this.endpoint
}

output "app_insights_portal_url" {
  value = "https://portal.azure.com/#resource${azurerm_application_insights.this.id}/overview"
}
