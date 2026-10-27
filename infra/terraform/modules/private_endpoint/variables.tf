variable "service" {
  type        = string
  description = "Short service token used in the name, e.g. psql, redis, kv."
}

variable "name_suffix" {
  type        = string
  description = "cloudops-{env}, from the naming module."
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "resource_id" {
  type = string
}

variable "subresource" {
  type = string
}

variable "private_dns_zone_id" {
  type = string
}

variable "tags" {
  type = map(string)
  validation {
    condition     = alltrue([for k in ["owner", "environment", "costcentre", "application", "dataclass", "managedby"] : contains(keys(var.tags), k)])
    error_message = "tags must come from the tags module and carry every mandatory key."
  }
}
