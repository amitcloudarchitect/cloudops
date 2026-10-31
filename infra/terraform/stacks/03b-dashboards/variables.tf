variable "grafana_token" {
  type        = string
  sensitive   = true
  description = "An Entra access token for Azure Managed Grafana (see versions.tf). Never in a file."
}
