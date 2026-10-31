variable "postgres_entra_admin" {
  description = <<-EOT
    Entra principal administering the server. Password authentication is
    disabled, so this is the only way in until database roles exist. Prefer a
    group. Object ids are identifiers, not secrets.
  EOT
  type = object({
    object_id      = string
    principal_name = string
    principal_type = string # Group | User | ServicePrincipal
  })
  validation {
    condition     = contains(["Group", "User", "ServicePrincipal"], var.postgres_entra_admin.principal_type)
    error_message = "principal_type must be Group, User or ServicePrincipal."
  }
}

variable "enable_metrics_capture" {
  description = <<-EOT
    Event Hubs capture of evh-metrics to bronze (LLD). Capture authenticates
    with the namespace's managed identity, which needs Storage Blob Data
    Contributor on the bronze account (stack 04b-rbac). Apply 04 with this
    false, apply 04b, then set it true and apply 04 again.
  EOT
  type        = bool
  default     = false
}

# Capacity and resilience only. Defaults are the LLD's values, which UAT and
# production take unchanged; dev overrides them in its tfvars. Every security
# setting is fixed in code and identical in every environment.
variable "postgres_sizing" {
  type = object({
    sku_name              = string
    storage_mb            = number
    storage_tier          = string
    backup_retention_days = number
    geo_redundant_backup  = bool
    zone_redundant_ha     = bool
  })
  default = {
    sku_name              = "GP_Standard_D4ds_v5"
    storage_mb            = 524288 # 512 GB
    storage_tier          = "P30"
    backup_retention_days = 35
    geo_redundant_backup  = true
    zone_redundant_ha     = true
  }
  validation {
    condition     = startswith(var.postgres_sizing.sku_name, "GP_") || startswith(var.postgres_sizing.sku_name, "MO_")
    error_message = "General Purpose or Memory Optimized only: burstable tiers do not support HA or the LLD's parameters."
  }
}

variable "redis_zone_redundant" {
  description = "Premium P1 across zones 1-3 with two replicas (LLD). Off: a single non-zonal node."
  type        = bool
  default     = true
}

variable "postgres_location" {
  description = <<-EOT
    Region for the PostgreSQL server only. Null means var.location (the LLD's
    westeurope). Dev uses northeurope because the dev subscription is
    restricted from creating Flexible Server in westeurope; the private
    endpoint stays in the westeurope VNet either way.
  EOT
  type        = string
  default     = null
  validation {
    condition     = var.postgres_location == null || contains(["westeurope", "northeurope"], coalesce(var.postgres_location, "westeurope"))
    error_message = "Allowed locations are westeurope and northeurope (LLD, Azure Policy)."
  }
}

variable "postgres_breakglass_password_wo" {
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true
  description = <<-EOT
    Dev only: the password for the administrator login that password authentication
    requires. Write-only, never stored in state. Generate it at apply time and do not
    keep it (docs/kubernetes.md); the Entra administrator administers the server.
  EOT
  validation {
    condition     = var.env != "dev" || (var.postgres_breakglass_password_wo != null && length(coalesce(var.postgres_breakglass_password_wo, "")) >= 32)
    error_message = "In dev, pass a generated password of at least 32 characters via TF_VAR_postgres_breakglass_password_wo."
  }
}

variable "platform_cost_export_from" {
  type        = string
  default     = "2026-11-01T00:00:00Z"
  description = <<-EOT
    First day of the platform cost export's daily schedule (ADR 0023). Cost Management
    refuses a start in the past, so a re-creation after this date needs a later one.
  EOT
  validation {
    condition     = can(regex("^\\d{4}-\\d{2}-\\d{2}T00:00:00Z$", var.platform_cost_export_from))
    error_message = "platform_cost_export_from is a UTC midnight, YYYY-MM-DDT00:00:00Z."
  }
}

variable "platform_cost_export_enabled" {
  type        = bool
  default     = true
  description = <<-EOT
    Create the platform cost exports (ADR 0023). False where the subscriptions' agreement
    allows no export (pay-as-you-go, Sponsorship: dev); the connector then reads the
    query API (CLOUDOPS_AZURE_COST_LINES=query).
  EOT
}
