# Stack 01 — network (LLD, Network). VNet, subnets, NSGs, private DNS zones, NAT.
#
# Address space per environment is fixed by the LLD so any pair can be peered
# later without renumbering. Subnet layout mirrors the production table at the
# same offsets inside each environment's /16.

locals {
  vnet_cidr = {
    prd = "10.60.0.0/16"
    dr  = "10.61.0.0/16"
    uat = "10.63.0.0/16"
    tst = "10.64.0.0/16"
    dev = "10.65.0.0/16"
  }[var.env]

  # LLD, Production subnets, at the same offsets. 10.x.16.0/20 (the LLD's pod
  # range) is left unallocated: with Azure CNI overlay, pods use a range
  # outside the VNet (stack 05), never a VNet subnet.
  subnet_cidr = {
    aks_system = cidrsubnet(local.vnet_cidr, 6, 0)    # x.0.0/22
    aks_app    = cidrsubnet(local.vnet_cidr, 6, 1)    # x.4.0/22
    data       = cidrsubnet(local.vnet_cidr, 8, 32)   # x.32.0/24
    msg        = cidrsubnet(local.vnet_cidr, 8, 33)   # x.33.0/24
    sec        = cidrsubnet(local.vnet_cidr, 8, 34)   # x.34.0/24
    ai         = cidrsubnet(local.vnet_cidr, 8, 35)   # x.35.0/24
    telemetry  = cidrsubnet(local.vnet_cidr, 8, 36)   # x.36.0/24
    egress     = cidrsubnet(local.vnet_cidr, 10, 148) # x.37.0/26
  }

  aks_subnets = ["aks_system", "aks_app"]

  # Subnets holding private endpoints: NSGs must apply to endpoint traffic.
  endpoint_subnets = ["data", "msg", "sec", "ai"]

  # Everything leaving the cluster goes through the NAT gateway's static IPs.
  nat_subnets = ["aks_system", "aks_app", "egress"]

  private_dns_zones = {
    postgres = "privatelink.postgres.database.azure.com"
    redis    = "privatelink.redis.cache.windows.net"
    blob     = "privatelink.blob.core.windows.net"
    vault    = "privatelink.vaultcore.azure.net"
    eventhub = "privatelink.servicebus.windows.net"
    acr      = "privatelink.azurecr.io"
    openai   = "privatelink.openai.azure.com"
  }
}

module "tags" {
  source      = "../../modules/tags"
  owner       = var.owner
  environment = var.env
  costcentre  = var.costcentre
  application = var.application
  dataclass   = var.dataclass["network"]
}

resource "azurerm_resource_group" "net" {
  name     = module.names.resource_group["net"]
  location = var.location
  tags     = module.tags.tags
}

resource "azurerm_virtual_network" "this" {
  name                = module.names.vnet
  resource_group_name = azurerm_resource_group.net.name
  location            = var.location
  address_space       = [local.vnet_cidr]
  tags                = module.tags.tags
  # Azure-provided DNS, no custom servers (LLD).
}

resource "azurerm_subnet" "this" {
  for_each = local.subnet_cidr

  name                 = module.names.subnet[each.key]
  resource_group_name  = azurerm_resource_group.net.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [each.value]

  # No implicit internet egress: outbound is the NAT gateway or nothing.
  default_outbound_access_enabled   = false
  private_endpoint_network_policies = contains(local.endpoint_subnets, each.key) ? "Enabled" : "Disabled"
}

# --- NAT ---------------------------------------------------------------------

resource "azurerm_public_ip" "nat" {
  name                = module.names.nat_public_ip
  resource_group_name = azurerm_resource_group.net.name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"]
  tags                = module.tags.tags
}

resource "azurerm_nat_gateway" "this" {
  name                    = module.names.nat_gateway
  resource_group_name     = azurerm_resource_group.net.name
  location                = var.location
  sku_name                = "Standard"
  idle_timeout_in_minutes = 10
  tags                    = module.tags.tags
}

resource "azurerm_nat_gateway_public_ip_association" "this" {
  nat_gateway_id       = azurerm_nat_gateway.this.id
  public_ip_address_id = azurerm_public_ip.nat.id
}

resource "azurerm_subnet_nat_gateway_association" "this" {
  for_each = toset(local.nat_subnets)

  subnet_id      = azurerm_subnet.this[each.key].id
  nat_gateway_id = azurerm_nat_gateway.this.id
}

# --- NSGs: default-deny inbound, allowed flows from the LLD ---------------------

locals {
  aks_cidrs = [for s in local.aks_subnets : local.subnet_cidr[s]]

  # Azure CNI overlay (stack 05, pod_cidr; a test keeps the two equal). Pod-to-pod
  # traffic between nodes keeps the pod addresses as source and destination, so the
  # intra-cluster rules must name this range too, or deny-all-inbound drops it: the
  # control plane's konnectivity tunnel to webhooks, CoreDNS across pools, and every
  # service whose pods sit on another pool. Traffic leaving the cluster (private
  # endpoints included) is SNATed to the node's address, so other subnets need only
  # the node ranges.
  aks_pod_cidr      = "100.64.0.0/16"
  aks_intra_sources = concat(local.aks_cidrs, [local.aks_pod_cidr])

  # Dev only, a recorded departure (README): the devbox (stack 06, x.38.0/24,
  # the same offset it computes) may reach PostgreSQL on 5432, to run the server
  # bootstrap (db/bootstrap/roles.sql) and, until the pipeline exists, migrations.
  # PostgreSQL still requires an Entra token; no other port on the data subnet
  # is opened, and no other environment gets the rule.
  devbox_cidr = cidrsubnet(local.vnet_cidr, 8, 38)

  # The service tag admits every Front Door profile in Azure, not only ours, so the
  # ingress also checks X-Azure-FDID against our profile (infra/cluster/ingress-nginx).
  frontdoor_ports = var.network_mode == "public" ? ["80", "443"] : ["443"]
  devbox_postgres = var.env == "dev" ? [
    { name = "allow-devbox-postgres", priority = 110, sources = [local.devbox_cidr], ports = ["5432"], protocol = "Tcp" },
  ] : []

  # Inbound allows per subnet. Ports are the LLD's (Private endpoints and NSGs)
  # unless noted.
  inbound = {
    aks_system = [
      # Intra-cluster traffic between node pools; AKS does not work without it.
      { name = "allow-aks-nodes", priority = 100, sources = local.aks_intra_sources, ports = ["*"], protocol = "*", destinations = [local.subnet_cidr["aks_system"], local.aks_pod_cidr] },
    ]
    aks_app = [
      # Front Door to the ingress. In public mode the ingress load balancer keeps the
      # public frontend IP as the destination (floating IP), so the rule must not be
      # limited to the subnet's range. Port 80 only in
      # public mode, where the Front Door -> origin hop is plain HTTP (README).
      { name = "allow-frontdoor-https", priority = 100, sources = ["AzureFrontDoor.Backend"], ports = local.frontdoor_ports, protocol = "Tcp", destinations = ["*"] },
      { name = "allow-aks-nodes", priority = 110, sources = local.aks_intra_sources, ports = ["*"], protocol = "*", destinations = [local.subnet_cidr["aks_app"], local.aks_pod_cidr] },
    ]
    data = concat([
      # 443 is not in the LLD's list but the blob private endpoints live here.
      { name = "allow-aks-data", priority = 100, sources = local.aks_cidrs, ports = ["5432", "6380", "443"], protocol = "Tcp" },
    ], local.devbox_postgres)
    msg = [
      { name = "allow-aks-messaging", priority = 100, sources = local.aks_cidrs, ports = ["443", "5671"], protocol = "Tcp" },
    ]
    sec = [
      { name = "allow-aks-https", priority = 100, sources = local.aks_cidrs, ports = ["443"], protocol = "Tcp" },
    ]
    ai = [
      { name = "allow-aks-https", priority = 100, sources = local.aks_cidrs, ports = ["443"], protocol = "Tcp" },
    ]
    telemetry = [
      # Telemetry store ports depend on L1 (ClickHouse or Data Explorer); 443 until decided.
      { name = "allow-app-telemetry", priority = 100, sources = [local.subnet_cidr["aks_app"]], ports = ["443"], protocol = "Tcp" },
    ]
    egress = []
  }
}

resource "azurerm_network_security_group" "this" {
  for_each = local.subnet_cidr

  name                = module.names.nsg[each.key]
  resource_group_name = azurerm_resource_group.net.name
  location            = var.location
  tags                = module.tags.tags

  dynamic "security_rule" {
    for_each = local.inbound[each.key]
    content {
      name              = security_rule.value.name
      priority          = security_rule.value.priority
      direction         = "Inbound"
      access            = "Allow"
      protocol          = security_rule.value.protocol
      source_port_range = "*"
      # Azure rejects "*" inside a port list; any-port must be the single-value form.
      destination_port_range       = security_rule.value.ports == ["*"] ? "*" : null
      destination_port_ranges      = security_rule.value.ports == ["*"] ? null : security_rule.value.ports
      source_address_prefix        = length(security_rule.value.sources) == 1 ? security_rule.value.sources[0] : null
      source_address_prefixes      = length(security_rule.value.sources) > 1 ? security_rule.value.sources : null
      destination_address_prefix   = length(lookup(security_rule.value, "destinations", [""])) == 1 ? lookup(security_rule.value, "destinations", [local.subnet_cidr[each.key]])[0] : null
      destination_address_prefixes = length(lookup(security_rule.value, "destinations", [""])) > 1 ? security_rule.value.destinations : null
    }
  }

  # Platform health probes; without this the AKS load balancer marks every node down.
  dynamic "security_rule" {
    for_each = contains(local.aks_subnets, each.key) ? [1] : []
    content {
      name                       = "allow-azure-load-balancer"
      priority                   = 4000
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "*"
      source_port_range          = "*"
      destination_port_range     = "*"
      source_address_prefix      = "AzureLoadBalancer"
      destination_address_prefix = "*"
    }
  }

  # The final explicit deny (LLD). Overrides the default AllowVnetInBound.
  security_rule {
    name                       = "deny-all-inbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "this" {
  for_each = local.subnet_cidr

  subnet_id                 = azurerm_subnet.this[each.key].id
  network_security_group_id = azurerm_network_security_group.this[each.key].id
}

# --- Private DNS -------------------------------------------------------------------

resource "azurerm_private_dns_zone" "this" {
  for_each = local.private_dns_zones

  name                = each.value
  resource_group_name = azurerm_resource_group.net.name
  tags                = module.tags.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "this" {
  for_each = local.private_dns_zones

  name                  = "link-${each.key}-${module.names.vnet}"
  resource_group_name   = azurerm_resource_group.net.name
  private_dns_zone_name = azurerm_private_dns_zone.this[each.key].name
  virtual_network_id    = azurerm_virtual_network.this.id
  registration_enabled  = false
  tags                  = module.tags.tags
}
