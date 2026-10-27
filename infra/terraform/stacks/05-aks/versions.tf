terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
  }

  backend "azurerm" {
    key = "05-aks.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id     = local.subscription["aks"]
  storage_use_azuread = true
}

provider "azurerm" {
  alias = "net"
  features {}
  subscription_id = local.subscription["net"]
}

provider "azurerm" {
  alias = "sec"
  features {}
  subscription_id = local.subscription["sec"]
}

provider "azurerm" {
  alias = "obs"
  features {}
  subscription_id = local.subscription["obs"]
}
