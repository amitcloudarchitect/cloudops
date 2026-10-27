terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
  }

  backend "azurerm" {
    key = "02-security.tfstate"
  }
}

provider "azurerm" {
  features {
    key_vault {
      # Purge protection is on; never let a destroy try to purge.
      purge_soft_delete_on_destroy = false
    }
  }
  subscription_id     = local.subscription["sec"]
  storage_use_azuread = true
}

# Reads stack 01's resources, wherever its subscription ends up.
provider "azurerm" {
  alias = "net"
  features {}
  subscription_id = local.subscription["net"]
}
