terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
  }

  backend "azurerm" {
    key = "06-edge.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id     = local.subscription["net"]
  storage_use_azuread = true
}

provider "azurerm" {
  alias = "aks"
  features {}
  subscription_id = local.subscription["aks"]
}

provider "azurerm" {
  alias = "obs"
  features {}
  subscription_id = local.subscription["obs"]
}
