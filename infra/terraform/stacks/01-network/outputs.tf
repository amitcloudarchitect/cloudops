output "vnet_id" {
  value = azurerm_virtual_network.this.id
}

output "subnet_ids" {
  value = { for k, s in azurerm_subnet.this : k => s.id }
}

output "private_dns_zone_ids" {
  value = { for k, z in azurerm_private_dns_zone.this : k => z.id }
}

output "nat_public_ip" {
  description = "Static egress address partners allowlist (LLD, Egress and edge)."
  value       = azurerm_public_ip.nat.ip_address
}
