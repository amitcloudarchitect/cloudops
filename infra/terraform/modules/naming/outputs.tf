output "resource_group" {
  value = local.resource_groups
}

output "vnet" {
  value = "vnet-cloudops-${local.e}-${local.r}"
}

output "subnet" {
  value = local.subnets
}

output "nsg" {
  value = { for k, _ in local.subnets : k => "nsg-cloudops-${replace(k, "_", "-")}-${local.e}-${local.r}" }
}

output "nat_gateway" {
  value = "ng-cloudops-${local.e}-${local.r}"
}

output "nat_public_ip" {
  value = "pip-cloudops-nat-${local.e}-${local.r}"
}

output "key_vault" {
  value = {
    app  = "kv-cloudops-app-${local.e}-${local.r}"
    keys = "kv-cloudops-keys-${local.e}-${local.r}"
  }
}

output "workload_identity" {
  value = { for w in local.workload_identities : w => "id-cloudops-${w}-${local.e}" }
}

output "aks_identity" {
  value = "id-cloudops-aks-${local.e}"
}

output "log_analytics" {
  value = "log-cloudops-${local.e}-${local.r}"
}

output "app_insights" {
  value = "appi-cloudops-${local.e}-${local.r}"
}

output "postgres" {
  value = "psql-cloudops-${local.e}-${local.r}"
}

# For a server placed outside the stack's region, so its name states where it is.
output "postgres_by_location" {
  value = {
    westeurope  = "psql-cloudops-${local.e}-weu"
    northeurope = "psql-cloudops-${local.e}-neu"
  }
}

output "redis" {
  value = "redis-cloudops-${local.e}-${local.r}"
}

output "storage" {
  value = {
    bronze  = "stcloudopsbronze${local.e}${local.r}"
    reports = "stcloudopsreports${local.e}${local.r}"
  }
}

output "eventhub_namespace" {
  value = "evhns-cloudops-${local.e}-${local.r}"
}

output "aks" {
  value = "aks-cloudops-${local.e}-${local.r}"
}

output "aks_node_resource_group" {
  value = "rg-cloudops-aksnodes-${local.e}-${local.r}"
}

output "tfstate" {
  description = "Hand-built state backend (AZURE-ACCESS.md), for stacks that read another stack's outputs."
  value = {
    resource_group_name  = "rg-cloudops-tfstate-${local.e}-${local.r}"
    storage_account_name = "stcloudopstfstate${local.e}${local.r}"
    container_name       = "tfstate"
  }
}

output "private_endpoint_suffix" {
  description = "pe-{service}-cloudops-{env}, as in the LLD example pe-psql-cloudops-prd."
  value       = "cloudops-${local.e}"
}

# Edge (step 10). Front Door is global; the profile and its WAF policy live in the
# network resource group. WAF policy names allow letters and digits only.
output "frontdoor" {
  value = {
    profile    = "afd-cloudops-${local.e}"
    endpoint   = "cloudops-${local.e}"
    waf_policy = "wafcloudops${local.e}"
    origin     = "aks-ingress"
    # The Gateway API ingress (ADR 0011), side by side with origin until cutover.
    gateway_origin = "aks-gateway"
    # The customer portal's own hostname (step 18; LLD, Egress and edge: separate
    # hostnames so WAF policy, rate limits and certificates are tuned independently).
    portal_endpoint     = "cloudops-portal-${local.e}"
    portal_origin_group = "og-portal-aks-gateway"
    portal_waf_policy   = "wafcloudopsportal${local.e}"
  }
}

output "ingress_public_ip" {
  value = "pip-cloudops-ingress-${local.e}-${local.r}"
}

# The Gateway API ingress's static IP (ADR 0011). Its own name: during the migration
# both ingresses hold an IP, and Front Door switches between them.
output "gateway_public_ip" {
  value = "pip-cloudops-gateway-${local.e}-${local.r}"
}

# Azure OpenAI (LLD, AI platform configuration; ADR 0012). Also its custom subdomain,
# which Entra authentication and the private endpoint require.
output "openai" {
  value = "oai-cloudops-${local.e}-${local.r}"
}

# Container registry (step 11). Alphanumeric only, globally unique: crcloudops{env}{region}
# (LLD, Compute: images only from crcloudopsprdweu).
output "container_registry" {
  value = "crcloudops${local.e}${local.r}"
}

# Step 19 (ADR 0021): managed Prometheus, Grafana, paging.
output "monitor_workspace" {
  value = "amw-cloudops-${local.e}-${local.r}"
}

output "grafana" {
  # Azure Managed Grafana names are 2-23 characters.
  value = "amg-cloudops-${local.e}-${local.r}"
}

output "action_group" {
  value = {
    page   = "ag-cloudops-page-${local.e}"
    notify = "ag-cloudops-notify-${local.e}"
  }
}
