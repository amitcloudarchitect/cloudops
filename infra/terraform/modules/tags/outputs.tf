output "tags" {
  description = "Mandatory plus any optional tags. Assign to every resource's tags argument."
  value       = merge(local.optional, local.mandatory)
}
