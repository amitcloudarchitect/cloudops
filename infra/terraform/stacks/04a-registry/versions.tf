terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
  }

  backend "azurerm" {
    key = "04a-registry.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id     = local.subscription["aks"]
  storage_use_azuread = true
}

provider "azurerm" {
  alias = "obs"
  features {}
  subscription_id = local.subscription["obs"]
}
