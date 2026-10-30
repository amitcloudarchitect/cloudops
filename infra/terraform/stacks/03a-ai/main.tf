# Stack 03a — Azure OpenAI (LLD, AI platform configuration; ADR 0012). One resource,
# three deployments named by tier, never by model: application code asks for
# cloudops-small, cloudops-large or cloudops-embed (LLD).
#
# Before 04b-rbac, which grants the AI workload identity Cognitive Services OpenAI User
# on this account; nothing here grants access.
#
# - Private endpoint only (snet-ai, privatelink.openai.azure.com from stack 01);
#   public network access off.
# - Entra ID only: local (key) authentication off, so no API key exists to leak.
# - EU Data Zone deployments: prompts are processed only in EU regions (ADR 0012).
# - Each deployment pinned to an exact model version with automatic upgrades off;
#   a model changes only through the evaluation gate (LLD).
# - Abuse-monitoring opt-out is an application to Microsoft, not a setting: decided
#   before UAT (ADR 0012).

module "tags" {
  source      = "../../modules/tags"
  owner       = var.owner
  environment = var.env
  costcentre  = var.costcentre
  application = var.application
  dataclass   = var.dataclass["data"] # customer ticket text passes through it
}

data "azurerm_subnet" "ai" {
  provider             = azurerm.net
  name                 = module.names.subnet["ai"]
  virtual_network_name = module.names.vnet
  resource_group_name  = module.names.resource_group["net"]
}

data "azurerm_private_dns_zone" "openai" {
  provider            = azurerm.net
  name                = "privatelink.openai.azure.com"
  resource_group_name = module.names.resource_group["net"]
}

data "azurerm_log_analytics_workspace" "this" {
  provider            = azurerm.obs
  name                = module.names.log_analytics
  resource_group_name = module.names.resource_group["obs"]
}

resource "azurerm_resource_group" "ai" {
  name     = module.names.resource_group["ai"]
  location = var.location
  tags     = module.tags.tags
}

resource "azurerm_cognitive_account" "openai" {
  name                          = module.names.openai
  resource_group_name           = azurerm_resource_group.ai.name
  location                      = var.location
  kind                          = "OpenAI"
  sku_name                      = "S0"
  custom_subdomain_name         = module.names.openai
  local_auth_enabled            = false
  public_network_access_enabled = false
  tags                          = module.tags.tags

  network_acls {
    default_action = "Deny"
  }
}

locals {
  # Chosen 2026-10-30 from what West Europe offers as Data Zone (ADR 0012).
  models = {
    small = { name = "gpt-5.4-mini", version = "2026-03-17" }
    large = { name = "gpt-5.5", version = "2026-04-24" }
    embed = { name = "text-embedding-3-small", version = "1" } # 1536 dims: vector(1536)
  }
}

resource "azurerm_cognitive_deployment" "tier" {
  for_each               = local.models
  name                   = "cloudops-${each.key}"
  cognitive_account_id   = azurerm_cognitive_account.openai.id
  version_upgrade_option = "NoAutoUpgrade"

  model {
    format  = "OpenAI"
    name    = each.value.name
    version = each.value.version
  }

  sku {
    name     = "DataZoneStandard"
    capacity = var.openai_capacity[each.key]
  }
}

module "openai_endpoint" {
  source              = "../../modules/private_endpoint"
  service             = "oai"
  name_suffix         = module.names.private_endpoint_suffix
  resource_group_name = azurerm_resource_group.ai.name
  location            = var.location
  subnet_id           = data.azurerm_subnet.ai.id
  resource_id         = azurerm_cognitive_account.openai.id
  subresource         = "account"
  private_dns_zone_id = data.azurerm_private_dns_zone.openai.id
  tags                = module.tags.tags
}

# Who called what, when, and how many tokens: operation metadata, not prompt content.
resource "azurerm_monitor_diagnostic_setting" "openai" {
  name                       = "diag-to-law"
  target_resource_id         = azurerm_cognitive_account.openai.id
  log_analytics_workspace_id = data.azurerm_log_analytics_workspace.this.id

  enabled_log {
    category = "Audit"
  }
  enabled_log {
    category = "RequestResponse"
  }
  enabled_metric {
    category = "AllMetrics"
  }
}
