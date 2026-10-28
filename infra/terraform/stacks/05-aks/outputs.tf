output "oidc_issuer_url" {
  description = "For federating the workload identities in stack 02 to service accounts."
  value       = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "cluster_id" {
  value = azurerm_kubernetes_cluster.this.id
}

output "cluster_fqdn" {
  description = "The API server's address: public FQDN in public mode, private FQDN otherwise."
  value       = var.network_mode == "public" ? azurerm_kubernetes_cluster.this.fqdn : azurerm_kubernetes_cluster.this.private_fqdn
}

output "node_resource_group" {
  value = azurerm_kubernetes_cluster.this.node_resource_group
}

output "get_credentials_command" {
  description = "Entra ID only (local accounts are disabled): convert with kubelogin after fetching."
  value       = "az aks get-credentials -g ${azurerm_resource_group.aks.name} -n ${azurerm_kubernetes_cluster.this.name} --overwrite-existing && kubelogin convert-kubeconfig -l azurecli"
}

output "kubelet_object_id" {
  description = "The node pools' identity; stack 04b grants it AcrPull on the registry."
  value       = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
}
