terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
  }

  # Account and container come from env/<env>/backend.hcl.
  backend "azurerm" {
    key = "01-network.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id     = local.subscription["net"]
  storage_use_azuread = true
}
