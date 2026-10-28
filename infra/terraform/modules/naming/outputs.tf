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
  }
}

output "ingress_public_ip" {
  value = "pip-cloudops-ingress-${local.e}-${local.r}"
}

# Container registry (step 11). Alphanumeric only, globally unique: crcloudops{env}{region}
# (LLD, Compute: images only from crcloudopsprdweu).
output "container_registry" {
  value = "crcloudops${local.e}${local.r}"
}
