terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
    # Only for the platform's Cost Management export (platform_cost.tf): azurerm's
    # export resource cannot give an export the managed identity a firewalled
    # storage account needs.
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
  }

  backend "azurerm" {
    key = "04-data.tfstate"
  }
}

provider "azurerm" {
  features {
    storage {
      # Every account has public network access disabled, so the blob/queue
      # data plane is unreachable from wherever Terraform runs. Manage storage
      # through ARM only; nothing here needs the data plane.
      data_plane_available = false
    }
  }
  subscription_id     = local.subscription["data"]
  storage_use_azuread = true
}

provider "azapi" {
  subscription_id = local.subscription["data"]
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
