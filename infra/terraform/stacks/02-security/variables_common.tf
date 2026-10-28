# Common to every stack and identical in each (tools/ci/tests checks this), so a
# single env/<env>/common.tfvars drives them all.

variable "subscription_id" {
  type        = string
  description = "Subscription for every domain not overridden in subscription_ids."
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.subscription_id))
    error_message = "subscription_id must be a subscription GUID."
  }
}

variable "subscription_ids" {
  type        = map(string)
  default     = {}
  description = "Per-domain overrides (net, sec, obs, data, aks) for the five-subscription model."
  validation {
    condition     = alltrue([for k in keys(var.subscription_ids) : contains(["net", "sec", "obs", "data", "aks"], k)])
    error_message = "subscription_ids keys must be among net, sec, obs, data, aks."
  }
}

variable "env" {
  type = string
}

variable "location" {
  type = string
  validation {
    condition     = contains(["westeurope", "northeurope"], var.location)
    error_message = "Allowed locations are westeurope and northeurope (LLD, Azure Policy)."
  }
}

variable "region_short" {
  type = string
}

variable "owner" {
  type = string
}

variable "costcentre" {
  type = string
}

variable "application" {
  type = string
}

variable "dataclass" {
  type        = map(string)
  description = "Sensitivity class per stack: network, security, observability, data, aks."
}

variable "network_mode" {
  type        = string
  default     = "private"
  description = <<-EOT
    "private" is the LLD: private AKS API server, internal ingress, Front Door to the
    origin over Private Link. "public" is dev only (README, Recorded departures): a
    public API server limited to authorised IPs, a public ingress locked to Front Door
    by service tag and the X-Azure-FDID header. Data services stay private in both.
  EOT
  validation {
    condition     = contains(["public", "private"], var.network_mode)
    error_message = "network_mode must be public or private."
  }
  validation {
    condition     = var.network_mode == "private" || var.env == "dev"
    error_message = "network_mode = public is for dev only."
  }
}

locals {
  subscription = {
    for d in ["net", "sec", "obs", "data", "aks"] :
    d => lookup(var.subscription_ids, d, var.subscription_id)
  }
}

module "names" {
  source       = "../../modules/naming"
  env          = var.env
  region_short = var.region_short
}
