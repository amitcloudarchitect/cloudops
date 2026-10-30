variable "github_repository" {
  type        = string
  default     = "amitcloudarchitect/cloudops"
  description = "owner/name of the repository whose workflows may sign in as the CI identity (ADR 0013)."

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "github_repository must be owner/name."
  }
}
