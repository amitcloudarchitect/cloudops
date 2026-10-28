variable "aks_admin_group_object_ids" {
  description = "Entra groups granted cluster admin through Azure RBAC. Local accounts are disabled."
  type        = list(string)
  validation {
    condition     = length(var.aks_admin_group_object_ids) > 0
    error_message = "At least one admin group is required; with local accounts disabled nobody could administer the cluster otherwise."
  }
}

variable "pod_cidr" {
  description = <<-EOT
    Azure CNI overlay pod range. Must sit outside every platform VNet
    (10.60-10.65/16), so the LLD's 10.x.16.0/20 cannot be used as is, and a
    /20 gives only 16 nodes at /24 per node against a ceiling of 29.
  EOT
  type        = string
  default     = "100.64.0.0/16"
}

# Kubernetes service range. Must avoid the cluster VNet, every VNet the
# cluster may ever be peered with, and the ranges AKS reserves:
# 169.254.0.0/16, 172.30.0.0/16, 172.31.0.0/16, 192.0.2.0/24, 224.0.0.0/4.
# 172.16.0.0/16 is taken by two VNets in the dev subscription.
variable "service_cidr" {
  type    = string
  default = "10.200.0.0/16"
  validation {
    condition = !anytrue([
      for reserved in ["169.254.", "172.30.", "172.31.", "192.0.2.", "10.60.", "10.61.", "10.62.", "10.63.", "10.64.", "10.65."] :
      startswith(var.service_cidr, reserved)
    ])
    error_message = "service_cidr overlaps a range AKS reserves or a platform VNet (10.60-10.65)."
  }
}

variable "dns_service_ip" {
  type    = string
  default = "10.200.0.10"
}

variable "maintenance_window" {
  description = "Weekly window for cluster and node image upgrades (LLD: auto-upgrade with a maintenance window)."
  type = object({
    day_of_week = string
    start_time  = string # HH:MM, UTC
    duration    = number # hours
  })
  default = {
    day_of_week = "Sunday"
    start_time  = "01:00"
    duration    = 4
  }
}

variable "run_command_enabled" {
  description = "az aks command invoke. Off: the private API server is reached from the hub (Bastion), not by remote command."
  type        = bool
  default     = false
}

# Node size and counts per pool. Defaults are the LLD pool table, which UAT and
# production take unchanged; dev overrides them. Pool names, labels, zones, OS
# and every security setting are fixed in code.
variable "node_pools" {
  type = map(object({
    vm_size = string
    min     = number
    max     = number
  }))
  default = {
    npsystem    = { vm_size = "Standard_D4s_v5", min = 3, max = 5 }
    npapp       = { vm_size = "Standard_D4s_v5", min = 3, max = 8 }
    npingest    = { vm_size = "Standard_D4s_v5", min = 2, max = 6 }
    nptelemetry = { vm_size = "Standard_D8s_v5", min = 2, max = 6 }
    npai        = { vm_size = "Standard_D4s_v5", min = 2, max = 4 }
  }
  validation {
    condition     = toset(keys(var.node_pools)) == toset(["npsystem", "npapp", "npingest", "nptelemetry", "npai"])
    error_message = "node_pools must give exactly npsystem, npapp, npingest, nptelemetry and npai."
  }
  validation {
    # Dsv5 only: general purpose, premium disk, the family the quota covers.
    # No burstable or spot sizes (LLD: no spot on any pool).
    condition     = alltrue([for p in values(var.node_pools) : can(regex("^Standard_D(2|4|8|16|32)s_v5$", p.vm_size))])
    error_message = "vm_size must be a Dsv5 size: Standard_D2s_v5 to Standard_D32s_v5."
  }
  validation {
    condition     = alltrue([for k, p in var.node_pools : p.max >= p.min && p.max >= 1 && p.min >= (k == "npsystem" ? 1 : 0)])
    error_message = "Each pool needs max >= min and max >= 1; user pools may scale to zero, npsystem may not."
  }
  validation {
    condition     = var.env == "dev" || var.node_pools["npsystem"].min >= 2
    error_message = "Outside dev, npsystem needs at least two nodes so a node upgrade never takes down the system add-ons."
  }
}

variable "api_server_authorized_ip_ranges" {
  type        = list(string)
  default     = []
  description = <<-EOT
    With network_mode = "public" (dev only): the only addresses that may reach the API
    server, as CIDRs. Empty means no IP restriction: the API server is reachable from
    the internet, and Entra ID sign-in (local accounts are off) is the only control.
    Ignored when private.
  EOT
  validation {
    # "Allow all" is the empty list, never 0.0.0.0/0.
    condition     = alltrue([for r in var.api_server_authorized_ip_ranges : can(cidrhost(r, 0)) && r != "0.0.0.0/0"])
    error_message = "api_server_authorized_ip_ranges must be CIDRs; leave it empty, not 0.0.0.0/0, for no restriction."
  }
}
