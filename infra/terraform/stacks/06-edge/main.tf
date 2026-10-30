# Stack 06 — edge (step 10; LLD, Egress and edge). Front Door Premium with a WAF in
# Prevention mode in front of the AKS ingress. Requires stacks 01 and 05.
#
# network_mode = "public" (dev): the origin is a static public IP on the ingress load
# balancer. Two locks keep anyone but our Front Door off it, and both are needed:
#   1. stack 01's NSG admits only the AzureFrontDoor.Backend service tag; but that tag
#      is every Front Door profile in Azure, so anyone with their own profile could
#      point it at our IP;
#   2. the ingress therefore also rejects requests whose X-Azure-FDID header is not
#      our profile's id (frontdoor_id below; the API's HTTPRoute, ADR 0011).
# The Front Door -> origin hop is plain HTTP in public mode: Front Door accepts only a
# trusted certificate on an HTTPS origin, and a bare IP cannot have one. Browsers still
# get HTTPS, and HTTP redirects to it. Recorded as a departure in the README.
#
# network_mode = "private" (the LLD) is not wired here yet: Private Link to an internal
# load balancer needs a manual approval no Terraform can give.

data "azurerm_kubernetes_cluster" "this" {
  provider            = azurerm.aks
  name                = module.names.aks
  resource_group_name = module.names.resource_group["aks"]
}

data "azurerm_log_analytics_workspace" "this" {
  provider            = azurerm.obs
  name                = module.names.log_analytics
  resource_group_name = module.names.resource_group["obs"]
}

module "tags" {
  source      = "../../modules/tags"
  owner       = var.owner
  environment = var.env
  costcentre  = var.costcentre
  application = var.application
  dataclass   = var.dataclass["network"]
}

locals {
  public          = var.network_mode == "public"
  origin_protocol = local.public ? "HttpOnly" : "HttpsOnly"
}

# The Gateway API ingress's IP (ADR 0011). In the AKS node resource group, so the
# cluster's identity can attach it to the gateway's load balancer without a role
# assignment. Recreated if the cluster is.
resource "azurerm_public_ip" "gateway" {
  count               = local.public ? 1 : 0
  provider            = azurerm.aks
  name                = module.names.gateway_public_ip
  resource_group_name = data.azurerm_kubernetes_cluster.this.node_resource_group
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"]
  tags                = module.tags.tags
}

resource "azurerm_cdn_frontdoor_profile" "this" {
  name                     = module.names.frontdoor.profile
  resource_group_name      = module.names.resource_group["net"]
  sku_name                 = "Premium_AzureFrontDoor" # managed WAF rule sets need Premium
  response_timeout_seconds = 60
  tags                     = module.tags.tags
}

resource "azurerm_cdn_frontdoor_endpoint" "this" {
  name                     = module.names.frontdoor.endpoint
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this.id
  tags                     = module.tags.tags
}

resource "azurerm_cdn_frontdoor_origin_group" "this" {
  name                     = "og-${module.names.frontdoor.origin}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this.id
  session_affinity_enabled = false

  health_probe {
    path                = "/healthz"
    protocol            = local.public ? "Http" : "Https"
    request_type        = "GET"
    interval_in_seconds = 60
  }

  load_balancing {
    sample_size                 = 4
    successful_samples_required = 3
  }
}

# The Gateway API ingress (ADR 0011): Front Door's only origin since ingress-nginx
# was removed (phase C).
resource "azurerm_cdn_frontdoor_origin" "gateway" {
  count                          = local.public ? 1 : 0
  name                           = module.names.frontdoor.gateway_origin
  cdn_frontdoor_origin_group_id  = azurerm_cdn_frontdoor_origin_group.this.id
  enabled                        = true
  host_name                      = azurerm_public_ip.gateway[0].ip_address
  http_port                      = 80
  https_port                     = 443
  certificate_name_check_enabled = true
  priority                       = 1
  weight                         = 1000
}

resource "azurerm_cdn_frontdoor_firewall_policy" "this" {
  name                              = module.names.frontdoor.waf_policy
  resource_group_name               = module.names.resource_group["net"]
  sku_name                          = azurerm_cdn_frontdoor_profile.this.sku_name
  enabled                           = true
  mode                              = "Prevention" # Detection logs and allows; never here
  request_body_check_enabled        = true
  custom_block_response_status_code = 403
  tags                              = module.tags.tags

  # OWASP core rules (Microsoft's managed set) and bot protection (LLD).
  managed_rule {
    type    = "Microsoft_DefaultRuleSet"
    version = "2.1"
    action  = "Block"
  }

  managed_rule {
    type    = "Microsoft_BotManagerRuleSet"
    version = "1.1"
    action  = "Block"
  }
}

resource "azurerm_cdn_frontdoor_security_policy" "this" {
  name                     = "sp-${module.names.frontdoor.waf_policy}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this.id

  security_policies {
    firewall {
      cdn_frontdoor_firewall_policy_id = azurerm_cdn_frontdoor_firewall_policy.this.id
      association {
        patterns_to_match = ["/*"]
        domain {
          cdn_frontdoor_domain_id = azurerm_cdn_frontdoor_endpoint.this.id
        }
      }
    }
  }
}

resource "azurerm_cdn_frontdoor_route" "this" {
  count                         = local.public ? 1 : 0
  name                          = "route-all"
  cdn_frontdoor_endpoint_id     = azurerm_cdn_frontdoor_endpoint.this.id
  cdn_frontdoor_origin_group_id = azurerm_cdn_frontdoor_origin_group.this.id
  cdn_frontdoor_origin_ids      = [azurerm_cdn_frontdoor_origin.gateway[0].id]
  enabled                       = true
  patterns_to_match             = ["/*"]
  supported_protocols           = ["Http", "Https"]
  https_redirect_enabled        = true # HTTP in, redirected to HTTPS
  forwarding_protocol           = local.origin_protocol
  link_to_default_domain        = true # the managed *.azurefd.net certificate
}

resource "azurerm_monitor_diagnostic_setting" "frontdoor" {
  name                       = "diag-${module.names.log_analytics}"
  target_resource_id         = azurerm_cdn_frontdoor_profile.this.id
  log_analytics_workspace_id = data.azurerm_log_analytics_workspace.this.id

  enabled_log {
    category = "FrontDoorAccessLog"
  }

  enabled_log {
    category = "FrontDoorHealthProbeLog"
  }

  enabled_log {
    category = "FrontDoorWebApplicationFirewallLog"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}
