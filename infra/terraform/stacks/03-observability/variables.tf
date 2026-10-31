# Step 19 (ADR 0021): who is paged and what the alert rules key on. Defaults keep a
# plan valid with common.tfvars alone; an action group with no receiver pages nobody,
# so dev's stack tfvars (or TF_VAR_*) name them before the alerts mean anything.

variable "page_email_receivers" {
  type        = map(string)
  default     = {}
  description = "Name => e-mail address paged for Sev0 (always pages) and Sev1 (P2) alerts."
}

variable "page_webhook_receivers" {
  type        = map(string)
  default     = {}
  sensitive   = true
  description = "Name => webhook URL (an on-call tool's integration URL carries its key: pass as TF_VAR_page_webhook_receivers, never in a file)."
}

variable "notify_email_receivers" {
  type        = map(string)
  default     = {}
  description = "Name => e-mail address told about SLO warnings, which do not page."
}

variable "break_glass_object_ids" {
  type        = list(string)
  default     = []
  description = "Entra object ids of the two break-glass accounts (LLD, Pre-deployment readiness). Their sign-in always pages."
  validation {
    condition     = alltrue([for id in var.break_glass_object_ids : can(regex("^[0-9a-f-]{36}$", id))])
    error_message = "break_glass_object_ids must be object ids."
  }
}

variable "entra_signin_logs_exported" {
  type        = bool
  default     = false
  description = "True once Entra sign-in logs flow into this workspace (a tenant-level diagnostic setting an Entra administrator creates; ADR 0021). The break-glass rule reads them."
}

variable "signing_key_verifier_object_ids" {
  type        = list(string)
  default     = []
  description = "Principals expected to verify with the runbook signing key: the execution plane's, once it exists. Empty until then, so any verification pages."
}
