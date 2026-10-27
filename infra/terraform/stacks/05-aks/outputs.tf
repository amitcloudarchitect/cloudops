output "oidc_issuer_url" {
  description = "For federating the workload identities in stack 02 to service accounts."
  value       = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "cluster_id" {
  value = azurerm_kubernetes_cluster.this.id
}
