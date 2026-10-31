# The customer portal's host (step 18; LLD, Egress and edge: "two origin groups behind
# separate hostnames for staff and customers so policy, rate limits and certificates
# are tuned independently"). Same profile, its own endpoint, origin group, WAF policy
# and security policy. The staff host above is untouched.
#
# The origin is the same Gateway API ingress: one app serves both audiences, and the
# gateway's routes match on path and X-Azure-FDID, not on hostname, so the profile id
# lock (main.tf) covers this host too. What a caller may see is decided by the API
# from the authenticated principal (CLAUDE.md rule 1), never by which host they used.
#
# Dev uses the managed *.azurefd.net certificate; a custom domain arrives with UAT.

resource "azurerm_cdn_frontdoor_endpoint" "portal" {
  name                     = module.names.frontdoor.portal_endpoint
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this.id
  tags                     = module.tags.tags
}

resource "azurerm_cdn_frontdoor_origin_group" "portal" {
  name                     = module.names.frontdoor.portal_origin_group
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

resource "azurerm_cdn_frontdoor_origin" "portal_gateway" {
  count                          = local.public ? 1 : 0
  name                           = module.names.frontdoor.gateway_origin
  cdn_frontdoor_origin_group_id  = azurerm_cdn_frontdoor_origin_group.portal.id
  enabled                        = true
  host_name                      = azurerm_public_ip.gateway[0].ip_address
  http_port                      = 80
  https_port                     = 443
  certificate_name_check_enabled = true
  priority                       = 1
  weight                         = 1000
}

resource "azurerm_cdn_frontdoor_firewall_policy" "portal" {
  name                              = module.names.frontdoor.portal_waf_policy
  resource_group_name               = module.names.resource_group["net"]
  sku_name                          = azurerm_cdn_frontdoor_profile.this.sku_name
  enabled                           = true
  mode                              = "Prevention"
  request_body_check_enabled        = true
  custom_block_response_status_code = 403 # applies to managed and rate-limit blocks alike
  tags                              = module.tags.tags

  # Tighter rule first: a /v1 request over its own limit is stopped there.
  custom_rule {
    name                           = "RateLimitApi"
    enabled                        = true
    priority                       = 10
    type                           = "RateLimitRule"
    action                         = "Block"
    rate_limit_threshold           = var.portal_api_rate_limit.threshold
    rate_limit_duration_in_minutes = var.portal_api_rate_limit.duration_minutes

    # RequestUri is the full URL, scheme and host included.
    match_condition {
      match_variable = "RequestUri"
      operator       = "RegEx"
      match_values   = ["^https?://[^/]+/v1(/|\\?|$)"]
      transforms     = ["Lowercase"]
    }
  }

  custom_rule {
    name                           = "RateLimitAll"
    enabled                        = true
    priority                       = 20
    type                           = "RateLimitRule"
    action                         = "Block"
    rate_limit_threshold           = var.portal_rate_limit.threshold
    rate_limit_duration_in_minutes = var.portal_rate_limit.duration_minutes

    # Every client address: the rule applies to all traffic, counted per client IP.
    match_condition {
      match_variable = "RemoteAddr"
      operator       = "IPMatch"
      match_values   = ["0.0.0.0/0", "::/0"]
    }
  }

  # The staff host's managed sets: OWASP core rules and bot protection (LLD).
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

resource "azurerm_cdn_frontdoor_security_policy" "portal" {
  name                     = "sp-${module.names.frontdoor.portal_waf_policy}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this.id

  security_policies {
    firewall {
      cdn_frontdoor_firewall_policy_id = azurerm_cdn_frontdoor_firewall_policy.portal.id
      association {
        patterns_to_match = ["/*"]
        domain {
          cdn_frontdoor_domain_id = azurerm_cdn_frontdoor_endpoint.portal.id
        }
      }
    }
  }
}

resource "azurerm_cdn_frontdoor_route" "portal" {
  count                         = local.public ? 1 : 0
  name                          = "route-all"
  cdn_frontdoor_endpoint_id     = azurerm_cdn_frontdoor_endpoint.portal.id
  cdn_frontdoor_origin_group_id = azurerm_cdn_frontdoor_origin_group.portal.id
  cdn_frontdoor_origin_ids      = [azurerm_cdn_frontdoor_origin.portal_gateway[0].id]
  enabled                       = true
  patterns_to_match             = ["/*"]
  supported_protocols           = ["Http", "Https"]
  https_redirect_enabled        = true
  forwarding_protocol           = local.origin_protocol
  link_to_default_domain        = true
}

# No cached route for /_next/static/*: the staff host's hung every gzip request
# (ADR 0017, decision 11), and this one was the same. route "portal" carries them.
