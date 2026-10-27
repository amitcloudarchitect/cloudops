terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
  }

  backend "azurerm" {
    key = "03-observability.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id     = local.subscription["obs"]
  storage_use_azuread = true
}

provider "azurerm" {
  alias = "sec"
  features {}
  subscription_id = local.subscription["sec"]
}
