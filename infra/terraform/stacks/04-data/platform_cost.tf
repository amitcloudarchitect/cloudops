# The platform's own cost (ADR 0023): a daily Cost Management export of each platform
# subscription, FOCUS 1.0, into bronze, read by the Azure connector into the internal
# tenant (ADR 0022, decision 11).
#
# Bronze has public network access and shared keys off. Cost Management is a trusted
# Azure service for exports (bronze already bypasses AzureServices, for the Event
# Hubs capture), but only when the export has its own system-assigned identity and
# is created with API version 2023-08-01 or later. azurerm's export resource cannot
# do that, hence azapi. Stack 04b grants that identity Storage Blob Data Contributor
# on this one container, and the ingest identity Storage Blob Data Reader on it.

locals {
  # Every platform subscription once (dev has one; the five-subscription model has up
  # to five). The export's name ends in the subscription's first eight characters, so
  # the connector's root (CLOUDOPS_AZURE_COST_EXPORT_ROOTS) is
  #   cost-exports-platform/focus/cloudops-platform-<env>-<first eight>
  platform_subscriptions = toset(distinct(values(local.subscription)))
  platform_cost_root     = "focus"
}

# Only Cost Management writes here, as itself; the connector reads.
resource "azurerm_storage_container" "cost_exports_platform" {
  name                  = "cost-exports-platform"
  storage_account_id    = azurerm_storage_account.this["bronze"].id
  container_access_type = "private"
}

# The Azure connector's raw pages: listings, query responses and copies of the export
# blobs, written before anything parses them (LLD, Connector contract). One container
# per source, as servicenow.
resource "azurerm_storage_container" "raw_azure" {
  name                  = "azure"
  storage_account_id    = azurerm_storage_account.this["bronze"].id
  container_access_type = "private"
}

# Exports are served by their own resource provider, which a subscription does not
# have registered until something asks for it ("RP Not Registered" on the export's
# PUT). Registered here, once per platform subscription, so uat and prd get it too.
resource "azapi_resource_action" "register_cost_exports" {
  for_each = local.platform_subscriptions

  type        = "Microsoft.Resources/subscriptions@2022-12-01"
  resource_id = "/subscriptions/${each.value}"
  action      = "providers/Microsoft.CostManagementExports/register"
  method      = "POST"
}

resource "azapi_resource" "platform_cost_export" {
  for_each   = local.platform_subscriptions
  depends_on = [azapi_resource_action.register_cost_exports]

  type      = "Microsoft.CostManagement/exports@2025-03-01"
  name      = "cloudops-platform-${var.env}-${substr(each.value, 0, 8)}"
  parent_id = "/subscriptions/${each.value}"
  location  = var.location

  identity {
    type = "SystemAssigned"
  }

  # Registration completes asynchronously: retry the PUT until the provider is ready.
  retry = {
    error_message_regex  = ["RP Not Registered"]
    interval_seconds     = 15
    max_interval_seconds = 60
  }

  # No tags: an export is not a taggable resource (ARM refuses them).

  body = {
    properties = {
      format = "Csv"
      definition = {
        type      = "FocusCost"
        timeframe = "MonthToDate"
        dataSet = {
          granularity = "Daily"
          configuration = {
            dataVersion = "1.0"
          }
        }
      }
      deliveryInfo = {
        destination = {
          type           = "AzureBlob"
          resourceId     = azurerm_storage_account.this["bronze"].id
          container      = azurerm_storage_container.cost_exports_platform.name
          rootFolderPath = local.platform_cost_root
        }
      }
      # Each run in its own folder with its manifest, so a restating run is a new
      # assembly the connector swaps in whole (azure/focus.py).
      partitionData         = true
      dataOverwriteBehavior = "CreateNewReport"
      compressionMode       = "gzip"
      schedule = {
        status     = "Active"
        recurrence = "Daily"
        recurrencePeriod = {
          from = var.platform_cost_export_from
          to   = "2099-12-31T00:00:00Z"
        }
      }
    }
  }
}
