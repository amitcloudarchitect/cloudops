terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
  }

  backend "azurerm" {
    key = "04b-rbac.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id     = local.subscription["net"]
  storage_use_azuread = true
}

provider "azurerm" {
  alias = "sec"
  features {}
  subscription_id = local.subscription["sec"]
}

provider "azurerm" {
  alias = "data"
  features {}
  subscription_id = local.subscription["data"]
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
