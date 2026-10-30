output "frontdoor_hostname" {
  description = "The platform's address, for the Entra app registration's redirect URI."
  value       = azurerm_cdn_frontdoor_endpoint.this.host_name
}

output "frontdoor_id" {
  description = "Front Door sends this as X-Azure-FDID; the ingress rejects anything else."
  value       = azurerm_cdn_frontdoor_profile.this.resource_guid
}

output "ingress_public_ip" {
  value = local.public ? azurerm_public_ip.ingress[0].ip_address : null
}

output "ingress_public_ip_resource_group" {
  value = local.public ? azurerm_public_ip.ingress[0].resource_group_name : null
}

output "gateway_public_ip" {
  description = "The Gateway API ingress's address (ADR 0011); Front Door's second origin."
  value       = local.public ? azurerm_public_ip.gateway[0].ip_address : null
}
