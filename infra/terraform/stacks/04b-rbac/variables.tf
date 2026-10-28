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
