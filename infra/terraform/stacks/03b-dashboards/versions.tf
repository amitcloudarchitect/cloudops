terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.30"
    }
    # No committed lock file for this stack (.gitignore says why): the patch-level
    # constraint is the pin.
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.49.0"
    }
  }

  backend "azurerm" {
    key = "03b-dashboards.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id     = local.subscription["obs"]
  storage_use_azuread = true
}

# Azure Managed Grafana accepts an Entra access token for its API; API keys are off
# (stack 03). The token is short-lived and never stored; the resource is Azure
# Managed Grafana's well-known application id (docs/observability.md has the command):
#   export TF_VAR_grafana_token=$(az account get-access-token \
#     --resource "$AZURE_MANAGED_GRAFANA_APP_ID" --query accessToken -o tsv)
# Whoever applies needs Grafana Admin (stack 04b, grafana_admin_principal_ids).
provider "grafana" {
  url  = data.azurerm_dashboard_grafana.this.endpoint
  auth = var.grafana_token
}
