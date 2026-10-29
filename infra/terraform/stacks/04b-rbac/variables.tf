variable "acr_push_principal_ids" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Entra object IDs (people or groups) allowed to push images to the registry
    from their own az login. Dev only until the CI pipeline (step 13) pushes with
    its federated identity; production lists nobody here.
  EOT
  validation {
    condition     = alltrue([for id in var.acr_push_principal_ids : can(regex("^[0-9a-f-]{36}$", id))])
    error_message = "acr_push_principal_ids must be Entra object IDs."
  }
  validation {
    condition     = var.env == "dev" || length(var.acr_push_principal_ids) == 0
    error_message = "Only the pipeline identity pushes outside dev."
  }
}

variable "dev_secret_writer_ids" {
  type        = list(string)
  default     = []
  description = "Dev only: Entra object IDs that may write secrets to the app Key Vault."
  validation {
    condition     = alltrue([for id in var.dev_secret_writer_ids : can(regex("^[0-9a-f-]{36}$", id))])
    error_message = "dev_secret_writer_ids must be Entra object IDs."
  }
  validation {
    condition     = var.env == "dev" || length(var.dev_secret_writer_ids) == 0
    error_message = "Nobody writes secrets by hand outside dev."
  }
}
