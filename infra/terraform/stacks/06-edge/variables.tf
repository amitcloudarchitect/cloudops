# Customer portal edge (step 18). Rate limits are per client IP: Front Door counts
# requests from one socket address per POP over the window, so the effective global
# ceiling is a little higher than the threshold. Front Door's minimum threshold is 10
# and its windows are 1 or 5 minutes.

variable "portal_rate_limit" {
  type = object({
    threshold        = number
    duration_minutes = number
  })
  default = {
    threshold        = 1000
    duration_minutes = 1
  }
  description = <<-EOT
    Requests per client IP on every portal path. Generous: a page load pulls a dozen
    or so assets, and customers behind one corporate NAT share an IP.
  EOT
  validation {
    condition     = var.portal_rate_limit.threshold >= 10 && contains([1, 5], var.portal_rate_limit.duration_minutes)
    error_message = "threshold must be at least 10 and duration_minutes 1 or 5."
  }
}

variable "portal_api_rate_limit" {
  type = object({
    threshold        = number
    duration_minutes = number
  })
  default = {
    threshold        = 300
    duration_minutes = 1
  }
  description = <<-EOT
    Requests per client IP on the portal's /v1 API paths: tighter, because these hit
    the database and the AI gateway. Must stay below portal_rate_limit to mean anything.
  EOT
  validation {
    condition     = var.portal_api_rate_limit.threshold >= 10 && contains([1, 5], var.portal_api_rate_limit.duration_minutes)
    error_message = "threshold must be at least 10 and duration_minutes 1 or 5."
  }
}
