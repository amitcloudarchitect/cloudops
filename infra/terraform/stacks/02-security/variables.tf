variable "github_repository" {
  type        = string
  default     = "amitcloudarchitect/cloudops"
  description = "owner/name of the repository whose workflows may sign in as the CI identity (ADR 0013)."

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "github_repository must be owner/name."
  }
}

variable "github_repository_ids" {
  type        = object({ owner = number, repository = number })
  default     = { owner = 91311714, repository = 1437077070 }
  description = "GitHub's numeric IDs for the owner and repository: its immutable OIDC subjects name them (ADR 0013)."
}
