terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
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
