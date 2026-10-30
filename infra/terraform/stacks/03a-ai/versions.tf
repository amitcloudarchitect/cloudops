terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
  }

  backend "azurerm" {
    key = "03a-ai.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id     = local.subscription["data"]
  storage_use_azuread = true
}

provider "azurerm" {
  alias = "net"
  features {}
  subscription_id = local.subscription["net"]
}

provider "azurerm" {
  alias = "obs"
  features {}
  subscription_id = local.subscription["obs"]
}
