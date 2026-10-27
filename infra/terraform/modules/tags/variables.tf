variable "owner" {
  type        = string
  description = "Accountable owner (person or team mailbox)."
  validation {
    condition     = length(trimspace(var.owner)) > 0
    error_message = "owner is mandatory."
  }
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "tst", "uat", "prd", "dr"], var.environment)
    error_message = "environment must be one of dev, tst, uat, prd, dr."
  }
}

variable "costcentre" {
  type = string
  validation {
    condition     = length(trimspace(var.costcentre)) > 0
    error_message = "costcentre is mandatory."
  }
}

variable "application" {
  type = string
  validation {
    condition     = length(trimspace(var.application)) > 0
    error_message = "application is mandatory."
  }
}

variable "dataclass" {
  type        = string
  description = "Sensitivity class c1-c5 (HLD sensitivity classes)."
  validation {
    condition     = can(regex("^c[1-5]$", var.dataclass))
    error_message = "dataclass must be c1, c2, c3, c4 or c5."
  }
}

variable "schedule" {
  type    = string
  default = null
  validation {
    condition     = var.schedule == null || contains(["business-hours", "always-on"], coalesce(var.schedule, "always-on"))
    error_message = "schedule must be business-hours or always-on."
  }
}

variable "reviewdate" {
  type    = string
  default = null
  validation {
    condition     = var.reviewdate == null || can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", coalesce(var.reviewdate, "2000-01-01")))
    error_message = "reviewdate must be YYYY-MM-DD."
  }
}

variable "changeref" {
  type    = string
  default = null
}
