# Mandatory tags (LLD, Tagging). Every input is required and validated: there is
# no way to call this module and get a tag set missing one of them. Resources
# take their tags from here, and tools/ci/check_terraform_tags.py fails any plan
# in which a taggable resource lacks the mandatory set.

locals {
  mandatory = {
    owner       = var.owner
    environment = var.environment
    costcentre  = var.costcentre
    application = var.application
    dataclass   = var.dataclass
    managedby   = "terraform"
  }
  optional = {
    for k, v in {
      schedule   = var.schedule
      reviewdate = var.reviewdate
      changeref  = var.changeref
    } : k => v if v != null
  }
}
