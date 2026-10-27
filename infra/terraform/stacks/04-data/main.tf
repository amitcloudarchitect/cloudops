# Stack 04 — data tier (LLD, Data tier). PostgreSQL Flexible Server, Redis,
# storage accounts and Event Hubs. Every service: public network access
# disabled at the resource, reached by private endpoint, Entra ID
# authentication only, diagnostics to the platform workspace.

data "azurerm_client_config" "current" {}

data "azurerm_subnet" "this" {
  provider             = azurerm.net
  for_each             = toset(["data", "msg"])
  name                 = module.names.subnet[each.key]
  virtual_network_name = module.names.vnet
  resource_group_name  = module.names.resource_group["net"]
}

data "azurerm_private_dns_zone" "this" {
  provider = azurerm.net
  for_each = {
    postgres = "privatelink.postgres.database.azure.com"
    redis    = "privatelink.redis.cache.windows.net"
    blob     = "privatelink.blob.core.windows.net"
    eventhub = "privatelink.servicebus.windows.net"
  }
  name                = each.value
  resource_group_name = module.names.resource_group["net"]
}

data "azurerm_log_analytics_workspace" "this" {
  provider            = azurerm.obs
  name                = module.names.log_analytics
  resource_group_name = module.names.resource_group["obs"]
}

module "tags" {
  source      = "../../modules/tags"
  owner       = var.owner
  environment = var.env
  costcentre  = var.costcentre
  application = var.application
  dataclass   = var.dataclass["data"]
}

resource "azurerm_resource_group" "this" {
  for_each = toset(["data", "msg"])
  name     = module.names.resource_group[each.key]
  location = var.location
  tags     = module.tags.tags
}

resource "azurerm_management_lock" "data" {
  count      = var.env == "prd" ? 1 : 0
  name       = "lock-cannotdelete"
  scope      = azurerm_resource_group.this["data"].id
  lock_level = "CanNotDelete"
}

locals {
  postgres_location = coalesce(var.postgres_location, var.location)
  pe_suffix         = module.names.private_endpoint_suffix
  data_rg           = azurerm_resource_group.this["data"].name
  msg_rg            = azurerm_resource_group.this["msg"].name
}

# --- PostgreSQL Flexible Server --------------------------------------------------

resource "azurerm_postgresql_flexible_server" "this" {
  name                = module.names.postgres_by_location[local.postgres_location]
  resource_group_name = local.data_rg
  location            = local.postgres_location
  version             = "16"
  sku_name            = var.postgres_sizing.sku_name
  storage_mb          = var.postgres_sizing.storage_mb
  storage_tier        = var.postgres_sizing.storage_tier
  auto_grow_enabled   = true
  zone                = "1"

  backup_retention_days        = var.postgres_sizing.backup_retention_days
  geo_redundant_backup_enabled = var.postgres_sizing.geo_redundant_backup

  public_network_access_enabled = false

  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = data.azurerm_client_config.current.tenant_id
  }

  dynamic "high_availability" {
    for_each = var.postgres_sizing.zone_redundant_ha ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = "2"
    }
  }

  tags = module.tags.tags

  lifecycle {
    # Azure may move the primary between zones on failover.
    ignore_changes = [zone, high_availability[0].standby_availability_zone]
  }
}

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "this" {
  server_name         = azurerm_postgresql_flexible_server.this.name
  resource_group_name = local.data_rg
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = var.postgres_entra_admin.object_id
  principal_name      = var.postgres_entra_admin.principal_name
  principal_type      = var.postgres_entra_admin.principal_type
}

# LLD, PostgreSQL Flexible Server parameter table, in server units.
#
# One resource per parameter, each waiting for the previous one: the server
# applies configuration changes one at a time and rejects any that arrive
# while it is busy (ServerIsBusy). A for_each would send them in parallel.

resource "azurerm_postgresql_flexible_server_configuration" "pg_azure_extensions" {
  # LLD extensions: pgvector, pg_stat_statements, pgcrypto, pg_partman
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.this.id
  value     = "VECTOR,PG_STAT_STATEMENTS,PGCRYPTO,PG_PARTMAN"
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_shared_preload_libraries" {
  # pg_partman's background worker runs partition maintenance (restart)
  name       = "shared_preload_libraries"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "pg_stat_statements,pg_partman_bgw"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_azure_extensions]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_max_connections" {
  # restart
  name       = "max_connections"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "400"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_shared_preload_libraries]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_row_security" {
  # non-negotiable
  name       = "row_security"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "on"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_max_connections]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_require_secure_transport" {
  name       = "require_secure_transport"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "on"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_row_security]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_ssl_min_protocol_version" {
  name       = "ssl_min_protocol_version"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "TLSv1.2"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_require_secure_transport]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_work_mem" {
  # kB = 16 MB
  name       = "work_mem"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "16384"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_ssl_min_protocol_version]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_maintenance_work_mem" {
  # kB = 1 GB
  name       = "maintenance_work_mem"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "1048576"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_work_mem]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_random_page_cost" {
  name       = "random_page_cost"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "1.1"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_maintenance_work_mem]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_log_min_duration_statement" {
  # ms
  name       = "log_min_duration_statement"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "500"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_random_page_cost]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_statement_timeout" {
  # ms; the reporting role gets 300 s at role level
  name       = "statement_timeout"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "30000"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_log_min_duration_statement]
}

resource "azurerm_postgresql_flexible_server_configuration" "pg_idle_in_transaction_session_timeout" {
  # ms
  name       = "idle_in_transaction_session_timeout"
  server_id  = azurerm_postgresql_flexible_server.this.id
  value      = "60000"
  depends_on = [azurerm_postgresql_flexible_server_configuration.pg_statement_timeout]
}

# Addresses from the earlier for_each, so applied parameters are kept, not reset.

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["azure.extensions"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_azure_extensions
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["shared_preload_libraries"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_shared_preload_libraries
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["max_connections"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_max_connections
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["row_security"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_row_security
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["require_secure_transport"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_require_secure_transport
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["ssl_min_protocol_version"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_ssl_min_protocol_version
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["work_mem"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_work_mem
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["maintenance_work_mem"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_maintenance_work_mem
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["random_page_cost"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_random_page_cost
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["log_min_duration_statement"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_log_min_duration_statement
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["statement_timeout"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_statement_timeout
}

moved {
  from = azurerm_postgresql_flexible_server_configuration.this["idle_in_transaction_session_timeout"]
  to   = azurerm_postgresql_flexible_server_configuration.pg_idle_in_transaction_session_timeout
}

module "postgres_endpoint" {
  source              = "../../modules/private_endpoint"
  service             = "psql"
  name_suffix         = local.pe_suffix
  resource_group_name = local.data_rg
  location            = var.location
  subnet_id           = data.azurerm_subnet.this["data"].id
  resource_id         = azurerm_postgresql_flexible_server.this.id
  subresource         = "postgresqlServer"
  private_dns_zone_id = data.azurerm_private_dns_zone.this["postgres"].id
  tags                = module.tags.tags
}

# --- Redis Premium -------------------------------------------------------------------

resource "azurerm_redis_cache" "this" {
  name                = module.names.redis
  resource_group_name = local.data_rg
  location            = var.location
  sku_name            = "Premium"
  family              = "P"
  capacity            = 1
  # Zone redundancy needs replicas to place in the other zones.
  zones                = var.redis_zone_redundant ? ["1", "2", "3"] : null
  replicas_per_primary = var.redis_zone_redundant ? 2 : null

  non_ssl_port_enabled               = false
  minimum_tls_version                = "1.2"
  public_network_access_enabled      = false
  access_keys_authentication_enabled = false

  redis_configuration {
    maxmemory_policy                        = "volatile-lru"
    active_directory_authentication_enabled = true
    # AOF persistence (LLD) is not enabled here: it needs a storage
    # connection string, i.e. a key, and shared key access is disabled on
    # every account. Open item: infra/terraform/README.md.
  }

  tags = module.tags.tags
}

module "redis_endpoint" {
  source              = "../../modules/private_endpoint"
  service             = "redis"
  name_suffix         = local.pe_suffix
  resource_group_name = local.data_rg
  location            = var.location
  subnet_id           = data.azurerm_subnet.this["data"].id
  resource_id         = azurerm_redis_cache.this.id
  subresource         = "redisCache"
  private_dns_zone_id = data.azurerm_private_dns_zone.this["redis"].id
  tags                = module.tags.tags
}

# --- Storage ---------------------------------------------------------------------------

locals {
  storage = {
    bronze = {
      # GRS, not the LLD's GZRS: Azure cannot archive blobs in zone-redundant
      # accounts, and archiving 7 years of raw payloads matters more here.
      replication = "GRS"
      versioning  = true
      # Event Hubs capture writes here as a trusted Azure service.
      bypass = ["AzureServices"]
    }
    reports = {
      replication = "ZRS"
      versioning  = false
      bypass      = ["None"]
    }
  }
}

resource "azurerm_storage_account" "this" {
  for_each = local.storage

  name                     = module.names.storage[each.key]
  resource_group_name      = local.data_rg
  location                 = var.location
  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = each.value.replication

  https_traffic_only_enabled        = true
  min_tls_version                   = "TLS1_2"
  shared_access_key_enabled         = false
  default_to_oauth_authentication   = true
  public_network_access_enabled     = false
  allow_nested_items_to_be_public   = false
  infrastructure_encryption_enabled = true

  blob_properties {
    versioning_enabled = each.value.versioning
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  network_rules {
    default_action = "Deny"
    bypass         = each.value.bypass
  }

  tags = module.tags.tags
}

# LLD lifecycle. Bronze: hot 30 d, then cool, archive at 13 months, delete at
# 7 years. Reports: hot 90 d, then cool.
resource "azurerm_storage_management_policy" "bronze" {
  storage_account_id = azurerm_storage_account.this["bronze"].id

  rule {
    name    = "bronze-tiering"
    enabled = true
    filters {
      blob_types = ["blockBlob"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than    = 30
        tier_to_archive_after_days_since_modification_greater_than = 395
        delete_after_days_since_modification_greater_than          = 2557
      }
      version {
        delete_after_days_since_creation = 2557
      }
    }
  }
}

resource "azurerm_storage_management_policy" "reports" {
  storage_account_id = azurerm_storage_account.this["reports"].id

  rule {
    name    = "reports-tiering"
    enabled = true
    filters {
      blob_types = ["blockBlob"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than = 90
      }
    }
  }
}

module "storage_endpoint" {
  source   = "../../modules/private_endpoint"
  for_each = azurerm_storage_account.this

  service             = "st${each.key}"
  name_suffix         = local.pe_suffix
  resource_group_name = local.data_rg
  location            = var.location
  subnet_id           = data.azurerm_subnet.this["data"].id
  resource_id         = each.value.id
  subresource         = "blob"
  private_dns_zone_id = data.azurerm_private_dns_zone.this["blob"].id
  tags                = module.tags.tags
}

# --- Event Hubs ------------------------------------------------------------------------

resource "azurerm_eventhub_namespace" "this" {
  name                          = module.names.eventhub_namespace
  resource_group_name           = local.msg_rg
  location                      = var.location
  sku                           = "Standard"
  capacity                      = 4
  auto_inflate_enabled          = true
  maximum_throughput_units      = 12
  local_authentication_enabled  = false # managed identity, no SAS keys
  public_network_access_enabled = false
  minimum_tls_version           = "1.2"

  identity {
    type = "SystemAssigned"
  }

  tags = module.tags.tags
}

# Partition counts cannot be reduced later; sized for design scale (LLD).
resource "azurerm_eventhub" "metrics" {
  name              = "evh-metrics"
  namespace_id      = azurerm_eventhub_namespace.this.id
  partition_count   = 8
  message_retention = 3

  dynamic "capture_description" {
    for_each = var.enable_metrics_capture ? [1] : []
    content {
      enabled             = true
      encoding            = "Avro"
      interval_in_seconds = 300
      skip_empty_archives = true
      destination {
        name                = "EventHubArchive.AzureBlockBlob"
        archive_name_format = "{Namespace}/{EventHub}/{PartitionId}/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}"
        blob_container_name = "evh-metrics-capture"
        storage_account_id  = azurerm_storage_account.this["bronze"].id
      }
    }
  }
}

resource "azurerm_eventhub" "config" {
  name              = "evh-config"
  namespace_id      = azurerm_eventhub_namespace.this.id
  partition_count   = 4
  message_retention = 7
}

resource "azurerm_storage_container" "capture" {
  name                  = "evh-metrics-capture"
  storage_account_id    = azurerm_storage_account.this["bronze"].id
  container_access_type = "private"
}

module "eventhub_endpoint" {
  source              = "../../modules/private_endpoint"
  service             = "evhns"
  name_suffix         = local.pe_suffix
  resource_group_name = local.msg_rg
  location            = var.location
  subnet_id           = data.azurerm_subnet.this["msg"].id
  resource_id         = azurerm_eventhub_namespace.this.id
  subresource         = "namespace"
  private_dns_zone_id = data.azurerm_private_dns_zone.this["eventhub"].id
  tags                = module.tags.tags
}

# --- Diagnostics -------------------------------------------------------------------------

locals {
  diagnosed = {
    postgres = azurerm_postgresql_flexible_server.this.id
    redis    = azurerm_redis_cache.this.id
    eventhub = azurerm_eventhub_namespace.this.id
    bronze   = "${azurerm_storage_account.this["bronze"].id}/blobServices/default"
    reports  = "${azurerm_storage_account.this["reports"].id}/blobServices/default"
  }
}

resource "azurerm_monitor_diagnostic_setting" "this" {
  for_each = local.diagnosed

  name                       = "diag-${module.names.log_analytics}"
  target_resource_id         = each.value
  log_analytics_workspace_id = data.azurerm_log_analytics_workspace.this.id

  enabled_log {
    category_group = "allLogs"
  }

  # Blob services report their metric categories individually; asking for
  # AllMetrics there is stored as Capacity + Transaction and shows as a
  # change on every plan.
  dynamic "enabled_metric" {
    for_each = contains(["bronze", "reports"], each.key) ? ["Capacity", "Transaction"] : ["AllMetrics"]
    content {
      category = enabled_metric.value
    }
  }
}
