variable "env" {
  type = string
  validation {
    condition     = contains(["dev", "tst", "uat", "prd", "dr"], var.env)
    error_message = "env must be one of dev, tst, uat, prd, dr."
  }
}

variable "region_short" {
  type = string
  validation {
    condition     = contains(["weu", "neu"], var.region_short)
    error_message = "region_short must be weu or neu."
  }
}
