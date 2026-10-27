# A private endpoint with its DNS zone group. Every PaaS service is reached this
# way, with public network access disabled at the resource itself (LLD, Private
# endpoints and NSGs).

resource "azurerm_private_endpoint" "this" {
  name                          = "pe-${var.service}-${var.name_suffix}"
  custom_network_interface_name = "nic-pe-${var.service}-${var.name_suffix}"
  resource_group_name           = var.resource_group_name
  location                      = var.location
  subnet_id                     = var.subnet_id
  tags                          = var.tags

  private_service_connection {
    name                           = "psc-${var.service}-${var.name_suffix}"
    private_connection_resource_id = var.resource_id
    subresource_names              = [var.subresource]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }
}
