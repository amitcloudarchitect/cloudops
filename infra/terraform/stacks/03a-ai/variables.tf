variable "openai_capacity" {
  type        = map(number)
  default     = { small = 50, large = 30, embed = 50 }
  description = "Tokens per minute per tier, in thousands (Azure's capacity units). The LLD sets none; these are dev's (ADR 0012)."

  validation {
    condition     = toset(keys(var.openai_capacity)) == toset(["small", "large", "embed"])
    error_message = "openai_capacity must give exactly small, large and embed."
  }
}
